"""Subtract mode: kept-region planning (Phase C step 10, acceptance test 17).

Pure region math is unit-tested directly; the evidence-driven paths run
against a mock-indexed project with synthetic scene_boundaries/word_alignments
rows so the numbers are exact.
"""

import json
import sys
import uuid
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor import subtract  # noqa: E402
from agentic_video_editor.db import connect_db  # noqa: E402
from agentic_video_editor.project import load_project  # noqa: E402


def _json(result):
    return json.loads(result.stdout)


def _make_indexed_project(tmp_path, run_ave, clips=1):
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


def _db_path(project_dir):
    return load_project(Path(project_dir)).db_path


def _asset_id(project_dir):
    with connect_db(_db_path(project_dir)) as conn:
        row = conn.execute("select id from assets order by rowid limit 1").fetchone()
    return row["id"]


def _insert_gap(project_dir, asset_id, start_sec, end_sec):
    with connect_db(_db_path(project_dir)) as conn:
        for time_sec, reason in ((start_sec, "gap_start"), (end_sec, "gap_end")):
            conn.execute(
                """
                insert into scene_boundaries (
                    id, project_id, asset_id, time_sec, detector,
                    confidence, reason, params_json, created_at
                )
                values (?, 'default', ?, ?, 'audio_gap', null, ?, '{}', datetime('now'))
                """,
                (f"cut_{uuid.uuid4().hex[:16]}", asset_id, time_sec, reason),
            )
        conn.commit()


def _plan(project_dir, run_ave, directive):
    return _json(
        run_ave(
            "edit-plan",
            project_dir,
            "--directive",
            directive,
            "--duration-sec",
            "10",
            "--provider",
            "mock",
            "--json",
        )
    )


# --- Pure region math ---------------------------------------------------------


def test_merge_and_margin_region_math():
    close = [
        {"start_sec": 1.0, "end_sec": 1.4, "evidence": [{"type": "a"}], "why": ["first"]},
        {"start_sec": 1.6, "end_sec": 2.0, "evidence": [{"type": "b"}], "why": ["second"]},
    ]
    merged = subtract.merge_removals(close, merge_gap_sec=0.5)
    assert len(merged) == 1
    assert merged[0]["start_sec"] == 1.0 and merged[0]["end_sec"] == 2.0
    assert {entry["type"] for entry in merged[0]["evidence"]} == {"a", "b"}
    assert merged[0]["why"] == ["first", "second"]

    apart = [
        {"start_sec": 1.0, "end_sec": 1.4, "evidence": [], "why": []},
        {"start_sec": 2.0, "end_sec": 2.4, "evidence": [], "why": []},
    ]
    assert len(subtract.merge_removals(apart, merge_gap_sec=0.5)) == 2

    padded = subtract.apply_margin([{"start_sec": 1.0, "end_sec": 2.0}], margin_sec=0.2)
    assert padded[0]["start_sec"] == 1.2 and padded[0]["end_sec"] == 1.8
    # a removal that collapses under the margin vanishes: kept content wins
    assert subtract.apply_margin([{"start_sec": 0.5, "end_sec": 0.8}], margin_sec=0.2) == []


def test_sliver_absorption_partitions_the_timeline():
    result = subtract.subtract_regions(
        [{"start_sec": 9.0, "end_sec": 9.8, "evidence": [{"type": "audio_gap"}], "why": ["silence"]}],
        10.0,
        margin_sec=0.2,
        min_keep_sec=0.5,
    )
    # padded removal [9.2, 9.6] leaves a 0.4s tail sliver that gets absorbed
    assert [(round(r["start_sec"], 2), round(r["end_sec"], 2)) for r in result["kept"]] == [(0.0, 9.2)]
    assert len(result["removed"]) == 1
    removed = result["removed"][0]
    assert (round(removed["start_sec"], 2), round(removed["end_sec"], 2)) == (9.2, 10.0)
    assert removed["evidence"] and "too short to keep" in removed["why"]
    # kept + removed partition [0, 10] exactly
    covered = sum(r["end_sec"] - r["start_sec"] for r in result["kept"] + result["removed"])
    assert abs(covered - 10.0) < 0.01


def test_kept_regions_never_split_word_units():
    units = [
        {"text": "hello there", "start_sec": 0.9, "end_sec": 1.3},
        {"text": "and welcome back", "start_sec": 1.8, "end_sec": 2.3},
    ]
    result = subtract.subtract_regions(
        [{"start_sec": 1.0, "end_sec": 2.0, "evidence": [{"type": "audio_gap"}], "why": ["silence"]}],
        4.0,
        margin_sec=0.0,
        word_units=units,
    )
    for region in result["kept"]:
        for unit in units:
            assert not (unit["start_sec"] < region["start_sec"] < unit["end_sec"])
            assert not (unit["start_sec"] < region["end_sec"] < unit["end_sec"])
    assert result["word_unit_guards"], "guard actions should be recorded"
    # the removal shrank to the gap between the two finished units
    removed = result["removed"][0]
    assert removed["start_sec"] >= 1.3 and removed["end_sec"] <= 1.8


# --- Evidence-driven planning (acceptance test 17) ------------------------------


def test_gap_subtraction_end_to_end(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)
    _insert_gap(project_dir, _asset_id(project_dir), 0.6, 1.4)

    plan = _plan(project_dir, run_ave, "cut out the silences and dead moments")

    assert plan["mode"] == "subtract"
    assert plan["status"] == "ok"
    assert plan["removed_regions"], "the inserted silence must be removed"
    for region in plan["removed_regions"]:
        assert region["evidence"], f"removed region {region} carries no evidence"
        assert region["why"]
    kinds = {entry["type"] for region in plan["removed_regions"] for entry in region["evidence"]}
    assert "audio_gap" in kinds
    silence = plan["removed_regions"][0]
    assert 0.5 <= silence["start_sec"] <= 1.0 and 1.0 <= silence["end_sec"] <= 1.5
    assert "silence" in silence["why"]

    assert len(plan["selected_sequence"]) == 2
    for item in plan["selected_sequence"]:
        assert item["beat_role"] == "kept_region"
        assert "kept" in item["why_here"]
    assert "removal" in plan["selected_sequence"][0]["why_here"]
    assert abs(plan["kept_total_sec"] + plan["removed_total_sec"] - 2.0) < 0.05

    result = run_ave(
        "timeline",
        project_dir,
        "--directive",
        "cut out the silences and dead moments",
        "--duration-sec",
        "10",
        "--context-aware",
        "--provider",
        "mock",
        "--json",
    )
    summary = _json(result)
    assert summary["items_created"] == 2


def test_semantic_target_and_zero_coverage(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)

    plan = _plan(project_dir, run_ave, "remove the ball throws and the juggling")

    assert plan["mode"] == "subtract"
    targets = plan["removal_targets"]
    assert targets["attributes"]["ball throws"] >= 1
    assert targets["zero_coverage"] == ["juggling"]
    assert any("juggling" in warning for warning in plan["casting_warnings"])

    semantic = [
        entry
        for region in plan["removed_regions"]
        for entry in region["evidence"]
        if entry["type"] == "semantic_match"
    ]
    assert semantic, "the throwing observation must drive a removal"
    assert semantic[0]["target"] == "ball throws"
    assert semantic[0]["observation_type"] == "actions_events"
    removed = plan["removed_regions"][0]
    assert removed["start_sec"] >= 0.9  # the throw starts at 1.0; margin keeps a lead-in
    assert "ball throws" in removed["why"]


def test_no_recognizable_target_keeps_everything(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)
    project = load_project(Path(project_dir))
    intent = {
        "directive": "trim this down somehow",
        "operation": {"sources": "corpus", "output": "timeline", "mode": "subtract"},
        "evidence_attributes": [],
    }

    plan = subtract.plan_subtraction(project, intent)

    assert plan["status"] == "no_removal_evidence"
    assert plan["removed_regions"] == []
    assert len(plan["selected_sequence"]) == 1
    item = plan["selected_sequence"][0]
    assert item["source_start_sec"] == 0.0
    assert abs(item["source_end_sec"] - 2.0) < 0.11
    assert any("no recognizable removal target" in warning for warning in plan["casting_warnings"])
