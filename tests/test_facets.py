import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from helpers import make_mp4

from agentic_video_editor.facets import FACETS, facet_analyze_project
from agentic_video_editor.gemini_provider import MockProvider
from agentic_video_editor.project import load_project


def _json(result):
    return json.loads(result.stdout)


def _make_project(tmp_path, run_ave, clips=1):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for index in range(clips):
        make_mp4(source_dir / f"clip_{index}.mp4", seconds=2)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    return project_dir


def _observation_rows(project_dir):
    db_path = load_project(project_dir).db_path
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "select observation_type, source, start_sec, end_sec, value, confidence from observations"
            ).fetchall()
        ]


def test_facets_mock_creates_timed_observations_for_all_facets(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave)

    summary = _json(run_ave("facets", project_dir, "--provider", "mock", "--json"))

    assert summary["assets_completed"] == 1
    assert summary["facets_run"] == len(FACETS)
    rows = _observation_rows(project_dir)
    facet_types = {row["observation_type"] for row in rows}
    assert facet_types == set(FACETS)
    assert len(facet_types) >= 6
    for row in rows:
        assert row["start_sec"] is not None
        assert row["end_sec"] is not None
        assert row["end_sec"] > row["start_sec"]
        assert row["source"] == f"mock:{row['observation_type']}:v1"
        payload = json.loads(row["value"])
        assert payload["evidence"]

    people_values = [json.loads(row["value"]) for row in rows if row["observation_type"] == "people_appearance"]
    labels = {value["person"] for value in people_values}
    assert labels == {"P1", "P2"}


def test_facets_only_runs_selected_facets(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave)

    summary = _json(
        run_ave(
            "facets",
            project_dir,
            "--provider",
            "mock",
            "--only",
            "people_appearance",
            "--only",
            "audio_character",
            "--json",
        )
    )

    assert summary["facets_run"] == 2
    facet_types = {row["observation_type"] for row in _observation_rows(project_dir)}
    assert facet_types == {"people_appearance", "audio_character"}


def test_facets_resume_skips_done_and_force_reruns(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave)

    first = _json(run_ave("facets", project_dir, "--provider", "mock", "--json"))
    second = _json(run_ave("facets", project_dir, "--provider", "mock", "--json"))
    forced = _json(run_ave("facets", project_dir, "--provider", "mock", "--force", "--json"))

    assert first["facets_run"] == len(FACETS)
    assert second["facets_run"] == 0
    assert second["facets_skipped"] == len(FACETS)
    assert second["observations_created"] == 0
    assert forced["facets_run"] == len(FACETS)
    # force replaces rows for each facet source instead of duplicating them
    assert len(_observation_rows(project_dir)) == first["observations_created"]


def test_facets_budget_mode_stores_same_schema(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave)

    summary = _json(run_ave("facets", project_dir, "--provider", "mock", "--budget", "--json"))

    assert summary["facets_run"] == 1
    rows = _observation_rows(project_dir)
    facet_types = {row["observation_type"] for row in rows}
    assert len(facet_types) >= 6
    for row in rows:
        assert row["source"] == "mock:all_facets_budget:v1"
        assert row["start_sec"] is not None
        assert row["end_sec"] is not None
        assert json.loads(row["value"])["evidence"]

    resumed = _json(run_ave("facets", project_dir, "--provider", "mock", "--budget", "--json"))
    assert resumed["facets_run"] == 0


def test_facets_summary_reports_counts(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave)
    run_ave("facets", project_dir, "--provider", "mock", "--json")

    summary = _json(run_ave("facets-summary", project_dir, "--json"))

    assert summary["observations"] > 0
    assert set(summary["by_facet"]) == set(FACETS)


class _CountingProvider:
    """Session-per-asset probe: A2's one-upload guarantee at the facets layer."""

    def __init__(self):
        self.sessions_opened = 0
        self.prompts_run = 0
        self._mock = MockProvider()

    @contextmanager
    def video_session(self, video_path: Path):
        self.sessions_opened += 1
        provider = self

        class _Session:
            def generate_json(self, prompt: str):
                provider.prompts_run += 1
                return provider._mock.generate_video_json(video_path, prompt)

        yield _Session()


def test_facets_uses_one_session_per_asset(tmp_path, run_ave, monkeypatch):
    project_dir = _make_project(tmp_path, run_ave)
    project = load_project(project_dir)
    counting = _CountingProvider()
    monkeypatch.setattr(
        "agentic_video_editor.facets.provider_for_name",
        lambda name, **kwargs: counting,
    )

    summary = facet_analyze_project(project, provider_name="mock")

    assert counting.sessions_opened == 1
    assert counting.prompts_run == len(FACETS)
    assert summary.observations_created > 0


def test_facets_limit_makes_progress_across_repeated_resumed_runs(tmp_path, run_ave):
    """Regression: --limit smaller than the asset count must advance through
    the backlog on repeated invocations, not re-select the same
    already-complete assets forever (this stalled a real multi-asset ingest)."""
    project_dir = _make_project(tmp_path, run_ave, clips=4)

    for _ in range(4):
        run_ave("facets", project_dir, "--provider", "mock", "--limit", "1", "--json")

    project = load_project(project_dir)
    with sqlite3.connect(project.db_path) as conn:
        conn.row_factory = sqlite3.Row
        asset_count = conn.execute("select count(*) as c from assets").fetchone()["c"]
    assert asset_count == 4
    # every asset should have gotten all facets, not just the first one repeatedly
    with sqlite3.connect(project.db_path) as conn:
        conn.row_factory = sqlite3.Row
        per_asset_counts = conn.execute(
            "select asset_id, count(distinct observation_type) as n from observations group by asset_id"
        ).fetchall()
    assert len(per_asset_counts) == 4
    assert all(row["n"] == len(FACETS) for row in per_asset_counts)
