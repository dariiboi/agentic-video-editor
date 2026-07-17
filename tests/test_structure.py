import json
import sqlite3
import sys
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor import structure as structure_module  # noqa: E402
from agentic_video_editor.project import load_project  # noqa: E402
from agentic_video_editor.structure import (  # noqa: E402
    author_structure,
    expand_structure,
    intensity_to_weight,
    validate_ordering,
)


def _json(result):
    return json.loads(result.stdout)


def _make_indexed_project(tmp_path, run_ave, clips=3):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for index in range(clips):
        make_mp4(source_dir / f"clip_{index}.mp4", seconds=2)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("transcribe", project_dir, "--provider", "mock")
    run_ave("semantic-analyze", project_dir, "--provider", "mock")
    run_ave("facets", project_dir, "--provider", "mock", "--json")
    return project_dir


def _rows(project_dir, table):
    db_path = load_project(project_dir).db_path
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"select * from {table}").fetchall()]


INTENT_STUB = {
    "directive": "test directive",
    "operation": {"sources": "corpus", "output": "timeline", "mode": "compose"},
    "edit_type": "custom_edit",
    "requirements": [],
    "hard_constraints": {
        "duration_sec": {"value": 60, "provenance": "user_explicit", "why": None}
    },
    "anchors": [],
    "conflicts": [],
    "evidence_attributes": [],
    "success_rubric": {},
}


def _validated(raw):
    return structure_module._validate_structure(raw, INTENT_STUB, 60.0)


def test_expand_alternate_resolves_lanes_and_ramp():
    structure = _validated(
        {
            "logline": "x",
            "lanes": [
                {"id": "green", "casting_filter": "people_appearance: green t-shirt"},
                {"id": "blue", "casting_filter": "people_appearance: blue t-shirt"},
            ],
            "beats": [
                {
                    "id": "b1-b4",
                    "pattern": "alternate",
                    "lanes": ["green", "blue"],
                    "function": "escalation",
                    "intensity_target": [0.4, 0.9],
                }
            ],
        }
    )
    slots = expand_structure(structure, duration_sec=60)

    assert [slot["lane"] for slot in slots] == ["green", "blue", "green", "blue"]
    assert [slot["slot_id"] for slot in slots] == ["b1-b4#1", "b1-b4#2", "b1-b4#3", "b1-b4#4"]
    intensities = [slot["intensity"] for slot in slots]
    assert intensities[0] == 0.4 and intensities[-1] == 0.9
    assert intensities == sorted(intensities)  # the ramp rises monotonically
    assert all(slot["function"] == "escalation" for slot in slots)


def test_expand_repeat_fill_and_motif_carry_through():
    structure = _validated(
        {
            "logline": "x",
            "beats": [
                {"id": "r1", "pattern": "repeat", "count": 3, "function": "texture", "intensity_target": 0.5},
                {
                    "id": "m1",
                    "function": "callback",
                    "motif": {"slot": "hook_image", "occurrence": 2, "transform": "shorter"},
                    "fill": {"shots": [2, 4], "continuity": ["setting_context"]},
                },
            ],
        }
    )
    slots = expand_structure(structure, duration_sec=60)

    assert len(slots) == 4
    assert [slot["beat_id"] for slot in slots[:3]] == ["r1", "r1", "r1"]
    motif_slot = slots[3]
    assert motif_slot["motif"] == {"slot": "hook_image", "occurrence": 2, "transform": "shorter"}
    assert motif_slot["fill"] == {"shots_min": 2, "shots_max": 4, "continuity": ["setting_context"]}
    assert [slot["position"] for slot in slots] == [0, 1, 2, 3]


def test_intensity_to_weight_is_monotone_decreasing_and_clamped():
    grid = [0.0, 0.2, 0.5, 0.8, 1.0]
    weights = [intensity_to_weight(value) for value in grid]
    assert weights == sorted(weights, reverse=True)
    assert weights[0] == 1.6 and weights[-1] == 0.6
    assert intensity_to_weight(-5) == 1.6
    assert intensity_to_weight(5) == 0.6


def test_validate_ordering_reports_violations():
    structure = {
        "ordering_constraints": [
            {"type": "before", "a": "b1", "b": "b2"},
            {"type": "never_adjacent", "a": "b2", "b": "b3"},
        ]
    }
    good = [{"beat_id": "b1"}, {"beat_id": "b2"}, {"beat_id": "b4"}, {"beat_id": "b3"}]
    assert validate_ordering(structure, good) == []

    bad = [{"beat_id": "b2"}, {"beat_id": "b3"}, {"beat_id": "b1"}]
    violations = validate_ordering(structure, bad)
    assert any("b1 must come before b2" in violation for violation in violations)
    assert any("never be adjacent" in violation for violation in violations)


def test_garbage_reply_falls_back_and_stores_raw(tmp_path, run_ave, monkeypatch):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=1)
    project = load_project(project_dir)

    class GarbageProvider:
        def generate_text_json(self, prompt):
            return {"nonsense": True}

    monkeypatch.setattr(structure_module, "provider_for_name", lambda *args, **kwargs: GarbageProvider())

    structure = author_structure(project, dict(INTENT_STUB), duration_sec=60, provider_name="mock")

    assert structure["fallback"] is True
    assert len(structure["beats"]) >= 3
    assert structure["validation_warnings"]
    assert structure["raw_response"] == {"nonsense": True}
    rows = _rows(project_dir, "structures")
    assert len(rows) == 1
    assert rows[0]["fallback"] == 1
    assert json.loads(rows[0]["raw_json"]) == {"nonsense": True}
    stored = json.loads(rows[0]["structure_json"])
    assert stored["fallback"] is True


def test_mock_battle_structure_has_lanes_alternation_and_rising_intensity(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=1)

    structure = _json(
        run_ave(
            "structure",
            project_dir,
            "--directive",
            "create a battle between green t-shirts and blue t-shirts",
            "--duration-sec",
            "60",
            "--provider",
            "mock",
            "--json",
        )
    )

    assert {lane["id"] for lane in structure["lanes"]} == {"green", "blue"}
    alternate = [beat for beat in structure["beats"] if beat["pattern"] == "alternate"]
    assert alternate and alternate[0]["lanes"] == ["green", "blue"]
    ramp = alternate[0]["intensity_target"]
    assert ramp[0] < ramp[1]  # escalation rises
    assert structure["constraints_ack"]["duration_sec"]["provenance"] == "user_explicit"
    assert structure["fallback"] is False
    motifs = [beat["motif"] for beat in structure["beats"] if beat.get("motif")]
    assert {motif["occurrence"] for motif in motifs} == {1, 2}


def test_structured_plan_end_to_end(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _json(
        run_ave(
            "edit-plan",
            project_dir,
            "--directive",
            "create a battle between green t-shirts and blue t-shirts",
            "--duration-sec",
            "60",
            "--provider",
            "mock",
            "--json",
        )
    )

    assert plan["engine"] == "structured"
    assert plan["structure_id"]
    assert plan["selected_sequence"]

    # every item's why cites the invented beat function, not a search-term dump
    for item in plan["selected_sequence"]:
        assert item["why_here"].startswith(item["beat_role"])
        assert item["slot_id"] and item["beat_id"]

    # intensity drives targets: every escalation slot is tighter than aftermath
    slot_targets = {slot["slot_id"]: slot["target_duration_sec"] for slot in plan["expanded_slots"]}
    escalation = [target for slot_id, target in slot_targets.items() if slot_id.startswith("b3-b6")]
    aftermath = [target for slot_id, target in slot_targets.items() if slot_id.startswith("b8")]
    assert escalation and aftermath
    assert max(escalation) < min(aftermath)

    # ordering violations are reported, not silently repaired
    assert plan["ordering_violations"] == []
    assert "QueryAgent/CastingAgent" in plan["sequencing_note"]

    # motif recurrence is a sanctioned, recorded reuse
    assert any("motif" in warning for warning in plan["casting_warnings"])

    rows = _rows(project_dir, "edit_plans")
    assert len(rows) == 1
    assert rows[0]["source"] == "structured"
    stored = json.loads(rows[0]["plan_json"])
    assert stored["plan_id"] == plan["plan_id"]


def test_structured_plan_enumerate_supercut(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _json(
        run_ave(
            "edit-plan",
            project_dir,
            "--directive",
            'show every time someone says "love"',
            "--duration-sec",
            "30",
            "--provider",
            "mock",
            "--json",
        )
    )

    assert plan["intent_analysis"]["operation"]["mode"] == "enumerate"
    generated = [slot for slot in plan["expanded_slots"] if slot.get("generated_from")]
    assert generated and all("love" in slot["generated_from"] for slot in generated)
    assert plan["selected_sequence"]
    assert len(plan["selected_sequence"]) == len(generated)
    # chronological order across the generated slots
    starts = [(item["file_name"], item["source_start_sec"]) for item in plan["selected_sequence"]]
    assert starts == sorted(starts)
