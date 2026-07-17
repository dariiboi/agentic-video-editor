import json
import sqlite3
import sys
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor import intent as intent_module  # noqa: E402
from agentic_video_editor.intent import analyze_intent  # noqa: E402
from agentic_video_editor.project import load_project  # noqa: E402


PROVENANCES = {"user_explicit", "user_implicit", "agent"}


def _json(result):
    return json.loads(result.stdout)


def _make_indexed_project(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_0.mp4", seconds=2)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("transcribe", project_dir, "--provider", "mock")
    run_ave("semantic-analyze", project_dir, "--provider", "mock")
    run_ave("facets", project_dir, "--provider", "mock", "--json")
    return project_dir


def _intent_rows(project_dir):
    db_path = load_project(project_dir).db_path
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("select * from intent_analyses").fetchall()]


def test_battle_directive_composes_with_provenance_and_evidence(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    analysis = _json(
        run_ave(
            "intent",
            project_dir,
            "--directive",
            "create a battle between green t-shirts and blue t-shirts",
            "--duration-sec",
            "2",
            "--provider",
            "mock",
            "--json",
        )
    )

    assert analysis["operation"]["mode"] == "compose"
    assert analysis["operation"]["output"] == "timeline"
    assert analysis["edit_type"]  # free string, not validated against an enum
    assert "t-shirt color" in analysis["evidence_attributes"]
    assert analysis["requirements"]
    for requirement in analysis["requirements"]:
        assert requirement["provenance"] in PROVENANCES
        if requirement["provenance"] != "user_explicit":
            assert requirement["why"]
    assert analysis["hard_constraints"]["duration_sec"]["provenance"] == "user_explicit"
    assert abs(sum(analysis["success_rubric"].values()) - 1.0) < 0.01

    rows = _intent_rows(project_dir)
    assert len(rows) == 1
    assert rows[0]["source"] == "mock:intent_agent:v1"
    stored = json.loads(rows[0]["analysis_json"])
    assert stored["raw_response"]  # raw reply is always persisted


def test_supercut_directive_maps_to_enumerate(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    analysis = _json(
        run_ave(
            "intent",
            project_dir,
            "--directive",
            'show every time someone says "love"',
            "--duration-sec",
            "2",
            "--provider",
            "mock",
            "--json",
        )
    )

    assert analysis["operation"]["mode"] == "enumerate"
    assert analysis["edit_type"] == "supercut"
    assert any("love" in attribute for attribute in analysis["evidence_attributes"])


def test_pinned_directive_extracts_anchors(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    analysis = _json(
        run_ave(
            "intent",
            project_dir,
            "--directive",
            "open on the dog shot, end on the chorus",
            "--duration-sec",
            "2",
            "--provider",
            "mock",
            "--json",
        )
    )

    positions = {anchor["position"]: anchor for anchor in analysis["anchors"]}
    assert "first" in positions and "dog" in positions["first"]["description"]
    assert "last" in positions and "chorus" in positions["last"]["description"]
    for anchor in analysis["anchors"]:
        assert anchor["provenance"] == "user_explicit"


def test_infeasible_duration_surfaces_conflict(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    analysis = _json(
        run_ave(
            "intent",
            project_dir,
            "--directive",
            "a short documentary about the band",
            "--duration-sec",
            "300",
            "--provider",
            "mock",
            "--json",
        )
    )

    duration_conflicts = [
        conflict for conflict in analysis["conflicts"] if "duration" in " ".join(conflict["between"]).lower()
    ]
    assert duration_conflicts
    assert duration_conflicts[0]["resolution"] == "surface_to_user"
    assert duration_conflicts[0]["resolution_why"]


def test_vague_directive_is_almost_all_agent_provenance(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    analysis = _json(
        run_ave(
            "intent",
            project_dir,
            "--directive",
            "make something cool",
            "--duration-sec",
            "2",
            "--provider",
            "mock",
            "--json",
        )
    )

    assert analysis["operation"]["mode"] == "compose"
    provenances = [requirement["provenance"] for requirement in analysis["requirements"]]
    assert provenances.count("agent") >= len(provenances) - 1
    for requirement in analysis["requirements"]:
        if requirement["provenance"] == "agent":
            assert requirement["why"]


def test_garbage_reply_falls_back_and_keeps_raw(tmp_path, run_ave, monkeypatch):
    project_dir = _make_indexed_project(tmp_path, run_ave)
    project = load_project(project_dir)

    class GarbageProvider:
        def generate_text_json(self, prompt):
            return {"nonsense": ["not", "an", "intent"]}

    monkeypatch.setattr(intent_module, "provider_for_name", lambda *args, **kwargs: GarbageProvider())

    analysis = analyze_intent(project, "whatever this is", duration_sec=2, provider_name="mock")

    assert analysis["fallback"] is True
    assert analysis["operation"]["mode"] == "compose"
    assert analysis["requirements"][0]["text"] == "whatever this is"
    assert analysis["requirements"][0]["provenance"] == "user_explicit"
    assert analysis["raw_response"] == {"nonsense": ["not", "an", "intent"]}
    assert analysis["validation_warnings"]
    rows = _intent_rows(project_dir)
    assert len(rows) == 1
    stored = json.loads(rows[0]["analysis_json"])
    assert stored["fallback"] is True
    assert stored["raw_response"] == {"nonsense": ["not", "an", "intent"]}


def test_partial_reply_is_repaired_with_warnings(tmp_path, run_ave, monkeypatch):
    project_dir = _make_indexed_project(tmp_path, run_ave)
    project = load_project(project_dir)

    class PartialProvider:
        def generate_text_json(self, prompt):
            return {
                "operation": {"mode": "orchestrate", "output": "movie", "sources": "the vault"},
                "edit_type": "weird_cut",
                "requirements": [
                    {"text": "keep it fun", "provenance": "vibes"},
                    {"provenance": "agent"},
                ],
                "hard_constraints": {"max_shot_sec": 5},
                "anchors": [{"description": "the sunset", "position": "middle-ish"}],
                "success_rubric": {"fun": "lots"},
            }

    monkeypatch.setattr(intent_module, "provider_for_name", lambda *args, **kwargs: PartialProvider())

    analysis = analyze_intent(project, "keep it fun", duration_sec=2, provider_name="mock", store=False)

    assert analysis["fallback"] is False
    assert analysis["operation"] == {"sources": "corpus", "output": "timeline", "mode": "compose"}
    assert analysis["requirements"] == [{"text": "keep it fun", "provenance": "agent", "why": None}]
    assert analysis["hard_constraints"]["max_shot_sec"]["provenance"] == "agent"
    assert analysis["hard_constraints"]["duration_sec"]["provenance"] == "user_explicit"
    assert analysis["anchors"][0]["position"] == "anywhere"
    assert analysis["success_rubric"] == {}
    assert len(analysis["validation_warnings"]) >= 5
