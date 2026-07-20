import json
import sqlite3
from pathlib import Path

from helpers import make_mp4

from agentic_video_editor.facets import facet_analyze_project
from agentic_video_editor.gemini_provider import MockProvider
from agentic_video_editor.media import probe_media
from agentic_video_editor.project import load_project
from agentic_video_editor.proxy import generate_proxies_for_project, generate_proxy


def _json(result):
    return json.loads(result.stdout)


def _make_project(tmp_path, run_ave, *, seconds=1, size="640x480"):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_0.mp4", seconds=seconds, size=size)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    return project_dir


def _proxy_rows(project_dir):
    db_path = load_project(project_dir).db_path
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [
            dict(row)
            for row in conn.execute(
                "select asset_id, path from media_artifacts where artifact_type = 'proxy'"
            ).fetchall()
        ]


def test_generate_proxy_downscales_and_preserves_aspect(tmp_path):
    source = tmp_path / "source.mp4"
    make_mp4(source, seconds=1, size="640x480")
    output = tmp_path / "proxy.mp4"

    result = generate_proxy(source, output, max_height=240)

    assert result.status == "ok"
    assert output.exists()
    assert output.stat().st_size < source.stat().st_size
    probe = probe_media(output)
    assert probe.metadata["height"] == 240
    assert probe.metadata["width"] == 320  # 640x480 -> 320x240, aspect preserved


def test_generate_proxy_never_upscales_smaller_source(tmp_path):
    source = tmp_path / "source.mp4"
    make_mp4(source, seconds=1, size="160x90")
    output = tmp_path / "proxy.mp4"

    result = generate_proxy(source, output, max_height=480)

    assert result.status == "ok"
    probe = probe_media(output)
    assert probe.metadata["height"] == 90


def test_proxies_only_generated_for_assets_above_threshold(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave, seconds=1, size="640x480")
    project = load_project(project_dir)

    # threshold set above the tiny fixture's size/duration -> nothing proxied
    summary = generate_proxies_for_project(project, min_size_mb=999, min_duration_sec=999)
    assert summary.assets_requested == 0
    assert _proxy_rows(project_dir) == []

    # threshold set below -> the fixture qualifies
    summary = generate_proxies_for_project(project, min_size_mb=0, min_duration_sec=0, max_height=240)
    assert summary.assets_completed == 1
    rows = _proxy_rows(project_dir)
    assert len(rows) == 1
    assert Path(rows[0]["path"]).exists()


def test_proxies_limit_makes_progress_across_repeated_resumed_runs(tmp_path, run_ave):
    """Same lesson as the facets --limit bug: a limit smaller than the
    eligible count must advance through the backlog, not re-select the same
    already-proxied assets forever."""
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for index in range(4):
        make_mp4(source_dir / f"clip_{index}.mp4", seconds=1, size="640x480")
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    project = load_project(project_dir)

    for _ in range(4):
        generate_proxies_for_project(project, min_size_mb=0, min_duration_sec=0, max_height=240, limit=1)

    rows = _proxy_rows(project_dir)
    assert len({row["asset_id"] for row in rows}) == 4


def test_proxies_force_regenerates_single_row_per_asset(tmp_path, run_ave):
    project_dir = _make_project(tmp_path, run_ave, seconds=1, size="640x480")
    project = load_project(project_dir)

    generate_proxies_for_project(project, min_size_mb=0, min_duration_sec=0, max_height=240)
    generate_proxies_for_project(project, min_size_mb=0, min_duration_sec=0, max_height=240, force=True)

    rows = _proxy_rows(project_dir)
    assert len(rows) == 1  # never fans out the LEFT JOIN in transcript/semantics/facets


def test_facets_upload_uses_proxy_path_when_present(tmp_path, run_ave, monkeypatch):
    project_dir = _make_project(tmp_path, run_ave, seconds=1, size="640x480")
    project = load_project(project_dir)
    generate_proxies_for_project(project, min_size_mb=0, min_duration_sec=0, max_height=240)
    proxy_path = _proxy_rows(project_dir)[0]["path"]

    captured_paths = []
    real_video_session = MockProvider.video_session

    def _spy_video_session(self, video_path):
        captured_paths.append(str(video_path))
        return real_video_session(self, video_path)

    monkeypatch.setattr(MockProvider, "video_session", _spy_video_session)

    facet_analyze_project(project, provider_name="mock", limit=1)

    assert captured_paths, "expected at least one video_session call"
    assert captured_paths[0] == proxy_path
