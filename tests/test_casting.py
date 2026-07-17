import json
import sys
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from agentic_video_editor.casting import (  # noqa: E402
    AnchorResolutionError,
    cast_slot,
    coverage_report,
    enforce_slot_filters,
    pair_features,
    resolve_anchors,
)
from agentic_video_editor.planner import create_structured_plan  # noqa: E402
from agentic_video_editor.project import load_project  # noqa: E402


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


def _packet(segment_id, facets, *, score=1.0, summary="a moment"):
    return {
        "segment_id": segment_id,
        "asset_id": f"asset_{segment_id}",
        "file_name": f"{segment_id}.mp4",
        "time_range": [0.0, 5.0],
        "trim_range": [0.0, 5.0],
        "score": score,
        "story_roles": [],
        "source_evidence": {"summary": summary, "word_units": [], "facets": facets},
        "matched_via": ["context_search"],
    }


def _facet(observation_type, text, value=None, evidence=""):
    return {
        "observation_type": observation_type,
        "time_range": [0.0, 5.0],
        "text": text,
        "evidence": evidence,
        "value": value or {},
    }


GREEN_PACKET = _packet("seg_green", [_facet("people_appearance", "P1 t-shirt green tall")])
BLUE_PACKET = _packet("seg_blue", [_facet("people_appearance", "P2 t-shirt blue wristband")])


def test_lane_enforcement_green_slot_never_sees_blue_only_segment():
    slot = {"slot_id": "b1", "lane": "green", "withhold": []}
    lane_filters = {"green": "people_appearance: green t-shirt"}

    kept = enforce_slot_filters([GREEN_PACKET, BLUE_PACKET], slot, lane_filters)

    assert [packet["segment_id"] for packet in kept] == ["seg_green"]
    # and the kept packet's facet evidence really places green in frame
    assert "green" in kept[0]["source_evidence"]["facets"][0]["text"]


def test_withhold_excludes_packets_exhibiting_the_attribute():
    slot = {"slot_id": "b1", "lane": None, "withhold": ["blue t-shirt"]}

    kept = enforce_slot_filters([GREEN_PACKET, BLUE_PACKET], slot, {})

    assert [packet["segment_id"] for packet in kept] == ["seg_green"]


def test_coverage_report_zero_for_nonsense_attribute(tmp_path, run_ave):
    project = load_project(_make_indexed_project(tmp_path, run_ave, clips=2))
    intent = {
        "directive": "battle between green t-shirts and blue t-shirts",
        "evidence_attributes": ["t-shirt color", "red hats"],
    }

    report = coverage_report(project, intent)

    assert report["attributes"]["t-shirt color"] > 0
    assert report["attributes"]["red hats"] == 0
    assert report["zero_coverage"] == ["red hats"]


def test_anchor_resolution_pins_first_and_fails_loudly(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)
    project = load_project(project_dir)

    # resolvable anchor pins the opening slot
    plan = create_structured_plan(
        project,
        directive="open on the green t-shirt performer, then build a celebration",
        duration_sec=30,
        provider_name="mock",
        store=False,
    )
    assert plan["anchors_resolved"] and plan["anchors_resolved"][0]["position"] == "first"
    assert plan["selected_sequence"][0]["segment_id"] == plan["anchors_resolved"][0]["segment_id"]
    assert plan["selected_sequence"][0]["anchor"]["position"] == "first"

    # unresolvable user_explicit anchor fails the plan loudly
    with pytest.raises(AnchorResolutionError) as excinfo:
        create_structured_plan(
            project,
            directive="open on the purple dragon, then build a celebration",
            duration_sec=30,
            provider_name="mock",
            store=False,
        )
    assert excinfo.value.failures[0]["description"] == "the purple dragon"

    # and the CLI surfaces it as a structured error with a nonzero exit
    result = run_ave(
        "edit-plan",
        project_dir,
        "--engine",
        "structured",
        "--directive",
        "open on the purple dragon, then build a celebration",
        "--provider",
        "mock",
        "--json",
        check=False,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["error"] == "anchor_resolution_failed"
    assert payload["failures"][0]["provenance"] == "user_explicit"


def test_resolve_anchors_soft_anchor_skips_without_failure(tmp_path, run_ave):
    project = load_project(_make_indexed_project(tmp_path, run_ave, clips=1))
    intent = {
        "anchors": [
            {"description": "the purple dragon", "position": "anywhere", "provenance": "user_implicit"}
        ]
    }

    resolution = resolve_anchors(project, intent)

    assert resolution["failures"] == []
    assert resolution["unresolved_soft"][0]["description"] == "the purple dragon"


def test_novelty_penalty_and_motif_exemption(tmp_path, run_ave):
    project = load_project(_make_indexed_project(tmp_path, run_ave, clips=3))

    plan = create_structured_plan(
        project,
        directive="create a battle between green t-shirts and blue t-shirts",
        duration_sec=60,
        provider_name="mock",
        store=False,
    )

    # motif occurrence 2 reuses occurrence 1's segment, recorded as sanctioned
    assert plan["sanctioned_reuse"], "motif recurrence must be recorded"
    reuse = plan["sanctioned_reuse"][0]
    occurrence_items = [item for item in plan["selected_sequence"] if item.get("motif")]
    first = next(item for item in occurrence_items if item["motif"]["occurrence"] == 1)
    second = next(item for item in occurrence_items if item["motif"]["occurrence"] == 2)
    assert first["segment_id"] == second["segment_id"] == reuse["segment_id"]

    # non-motif reuse is warned about, never silent
    reused_warnings = [warning for warning in plan["casting_warnings"] if "reuses segment" in warning]
    sequence_ids = [item["segment_id"] for item in plan["selected_sequence"] if not item.get("motif")]
    if len(sequence_ids) != len(set(sequence_ids)):
        assert reused_warnings


def test_pair_features_measures_and_nulls():
    wide = _packet(
        "seg_a",
        [
            _facet("cinematography", "wide pans left", {"shot_size": "wide", "camera_motion": "pans left"}),
            _facet("audio_character", "rising", {"energy": "rising"}),
            _facet("setting_context", "rehearsal room", {"location_type": "rehearsal room"}),
        ],
    )
    close = _packet(
        "seg_b",
        [
            _facet("cinematography", "close_up follow right", {"shot_size": "close_up", "camera_motion": "handheld follow right"}),
            _facet("audio_character", "falling", {"energy": "falling"}),
            _facet("setting_context", "rehearsal room", {"location_type": "rehearsal room"}),
        ],
    )
    bare = _packet("seg_c", [])

    features = pair_features(wide, close, lane_a="green", lane_b="blue")
    assert features["shot_size_delta"] == -2  # wide -> close_up tightens
    assert features["motion_direction_match"] is False
    assert features["energy_delta"] == -1.0
    assert features["setting_match"] is True
    assert features["lane_relation"] == "other"

    nulls = pair_features(wide, bare)
    assert nulls["shot_size_delta"] is None
    assert nulls["motion_direction_match"] is None
    assert nulls["energy_delta"] is None
    assert nulls["setting_match"] is None
    assert nulls["lane_relation"] == "none"


def test_hallucinated_casting_id_falls_back_with_warning():
    class HallucinatingProvider:
        def generate_text_json(self, prompt):
            assert "CASTING_AGENT" in prompt
            return {"selected": "seg_nope", "alternates": ["seg_blue"], "why": "made up"}

    slot = {"slot_id": "b1", "function": "open", "lane": None, "intensity": 0.5, "withhold": []}
    decision = cast_slot(HallucinatingProvider(), slot, [GREEN_PACKET, BLUE_PACKET], None)

    assert decision["packet"]["segment_id"] == "seg_green"  # top-ranked fallback
    assert decision["alternates"] == ["seg_blue"]
    assert any("unknown segment 'seg_nope'" in warning for warning in decision["warnings"])


def test_end_to_end_plan_carries_casting_evidence(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _json(
        run_ave(
            "edit-plan",
            project_dir,
            "--engine",
            "structured",
            "--directive",
            "create a battle between green t-shirts and blue t-shirts",
            "--duration-sec",
            "60",
            "--provider",
            "mock",
            "--json",
        )
    )

    assert plan["status"] == "ok"
    assert plan["coverage_report"]["attributes"]["t-shirt color"] > 0
    assert "QueryAgent/CastingAgent" in plan["sequencing_note"]
    for item in plan["selected_sequence"]:
        assert item["matched_via"], "every item names which queries matched it"
        assert item["casting_why"]
    # adjacent items carry measured pair features (nulls allowed, key must exist)
    assert all("pair_features_prev" in item for item in plan["selected_sequence"])
    assert any(item["pair_features_prev"] for item in plan["selected_sequence"][1:])
    # green-lane items only cast segments whose facet evidence shows green
    for item in plan["selected_sequence"]:
        if item.get("lane") == "green":
            facets = " ".join(
                str(facet.get("text") or "") for facet in item["source_evidence"].get("facets") or []
            )
            assert "green" in facets
