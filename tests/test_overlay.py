"""A-roll/b-roll overlay track (Phase C step 12).

An overlay beat casts a cutaway: b-roll video over the beat's continuing
primary audio (documentary J/L grammar). Structure carries it as the ninth
closed primitive, the compiler writes a broll-track item paired to its
primary, and the renderer muxes video from the b-roll with audio from the
primary before the unchanged concat/transition/loudness pipeline.
"""

import json
import sys
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor import timeline as timeline_module  # noqa: E402
from agentic_video_editor.db import connect_db  # noqa: E402
from agentic_video_editor.project import load_project  # noqa: E402
from agentic_video_editor.render import _clip_command, _pair_overlays  # noqa: E402
from agentic_video_editor.structure import _validate_structure, expand_structure  # noqa: E402

MINIDOC_DIRECTIVE = "make a mini documentary about the crew"


def _json(result):
    return json.loads(result.stdout)


def _make_indexed_project(tmp_path, run_ave, clips=3, seconds=2):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for index in range(clips):
        make_mp4(source_dir / f"clip_{index}.mp4", seconds=seconds)
    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    run_ave("transcribe", project_dir, "--provider", "mock")
    run_ave("semantic-analyze", project_dir, "--provider", "mock")
    run_ave("facets", project_dir, "--provider", "mock", "--json")
    return project_dir


def _minidoc_plan(project_dir, run_ave):
    return _json(
        run_ave(
            "edit-plan",
            project_dir,
            "--directive",
            MINIDOC_DIRECTIVE,
            "--duration-sec",
            "30",
            "--provider",
            "mock",
            "--json",
        )
    )


def test_structure_validates_overlay_and_carries_it_into_slots():
    intent = {"directive": "doc", "hard_constraints": {}}
    raw = {
        "logline": "x",
        "beats": [
            {"id": "b1", "function": "work", "overlay": {"need": "hands at work", "audio": "mute_broll"}},
            {"id": "b2", "function": "close", "overlay": {"audio": "keep_primary"}},
        ],
    }
    structure = _validate_structure(raw, intent, 30.0)

    overlay = structure["beats"][0]["overlay"]
    assert overlay == {"need": "hands at work", "audio": "keep_primary"}
    assert any("unsupported" in warning for warning in structure["validation_warnings"])
    # an overlay without a need is dropped, not invented
    assert structure["beats"][1]["overlay"] is None
    assert any("without a need" in warning for warning in structure["validation_warnings"])

    slots = expand_structure(structure, duration_sec=30.0)
    assert slots[0]["overlay"] == {"need": "hands at work", "audio": "keep_primary"}


def test_minidoc_plan_casts_an_overlay_with_evidence(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _minidoc_plan(project_dir, run_ave)

    work_items = [item for item in plan["selected_sequence"] if item["beat_id"] == "m3"]
    assert work_items, "the overlay beat itself must still cast a primary item"
    overlay = work_items[0].get("overlay")
    assert overlay, "the cast cutaway attaches to the beat's first item"
    assert overlay["audio"] == "keep_primary"
    assert overlay["segment_id"] != work_items[0]["segment_id"]
    assert overlay["need"] == "hands at work, the craft in close detail"
    assert overlay["why"].strip()
    assert overlay["source_end_sec"] > overlay["source_start_sec"]


def test_timeline_compiles_broll_row_paired_to_primary(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)
    run_ave(
        "timeline",
        project_dir,
        "--directive",
        MINIDOC_DIRECTIVE,
        "--duration-sec",
        "30",
        "--context-aware",
        "--provider",
        "mock",
    )

    with connect_db(load_project(Path(project_dir)).db_path) as conn:
        rows = [dict(row) for row in conn.execute("select * from timeline_items").fetchall()]
    overlays = [row for row in rows if row["track_kind"] == "broll"]
    assert len(overlays) == 1
    overlay = overlays[0]
    assert overlay["track_name"] == "overlay"
    assert overlay["role"] == "cutaway"

    meta = json.loads(overlay["overlay_json"])
    primary = next(row for row in rows if row["id"] == meta["overlay_of"])
    assert primary["track_kind"] == "video"
    # the overlay rides the primary's slot on the timeline...
    assert overlay["timeline_start_sec"] == primary["timeline_start_sec"]
    assert overlay["timeline_end_sec"] == primary["timeline_end_sec"]
    # ...its video is b-roll, its audio is the primary's continuing range
    assert overlay["asset_id"] != primary["asset_id"]
    assert meta["audio"] == "keep_primary"
    assert meta["audio_asset_id"] == primary["asset_id"]
    assert meta["audio_source_start_sec"] == primary["source_start_sec"]
    assert meta["audio_source_end_sec"] == primary["source_end_sec"]
    # video covers the audio exactly
    video_duration = overlay["source_end_sec"] - overlay["source_start_sec"]
    assert abs(video_duration - (primary["timeline_end_sec"] - primary["timeline_start_sec"])) < 0.01

    with connect_db(load_project(Path(project_dir)).db_path) as conn:
        timeline_json = json.loads(
            conn.execute("select timeline_json from timelines order by created_at desc limit 1").fetchone()[0]
        )
    kinds = [track["kind"] for track in timeline_json["tracks"]]
    assert kinds == ["video", "broll"]


def test_overlay_item_drops_broll_too_short_to_cover():
    primary = {
        "id": "item_primary",
        "asset_id": "asset_a",
        "source_start_sec": 4.0,
        "source_end_sec": 6.0,
        "timeline_start_sec": 0.0,
        "timeline_end_sec": 2.0,
    }
    short = {
        "segment_id": "seg_b",
        "asset_id": "asset_b",
        "source_start_sec": 1.0,
        "source_end_sec": 2.0,
        "why": "w",
        "need": "n",
        "audio": "keep_primary",
    }
    assert timeline_module._overlay_item(short, primary, cut_points=[], snap_tolerance_sec=0.0) is None

    long_enough = dict(short, source_end_sec=9.0)
    item = timeline_module._overlay_item(long_enough, primary, cut_points=[], snap_tolerance_sec=0.0)
    assert item["track_kind"] == "broll"
    assert item["source_end_sec"] - item["source_start_sec"] == 2.0
    assert item["overlay_meta"]["overlay_of"] == "item_primary"
    assert item["overlay_meta"]["audio_source_start_sec"] == 4.0


def test_clip_command_muxes_broll_video_over_primary_audio(tmp_path):
    item = {
        "asset_path": "/media/interview.mp4",
        "source_start_sec": 4.0,
        "source_end_sec": 6.0,
        "caption_text": None,
    }
    plain = _clip_command(
        item, tmp_path / "plain.mp4", start=4.0, end=6.0, micro_fade_in=True, micro_fade_out=True, burn_captions=False
    )
    assert plain.count("-i") == 1
    assert "-vf" in plain and "-af" in plain

    item["overlay_render"] = {"video_path": "/media/broll.mp4", "video_start_sec": 1.5, "video_end_sec": 3.5}
    muxed = _clip_command(
        item, tmp_path / "mux.mp4", start=4.0, end=6.0, micro_fade_in=True, micro_fade_out=True, burn_captions=False
    )
    assert muxed.count("-i") == 2
    assert muxed[muxed.index("-i") + 1] == "/media/broll.mp4"  # b-roll is the video input
    assert "/media/interview.mp4" in muxed
    graph = muxed[muxed.index("-filter_complex") + 1]
    assert graph.startswith("[0:v]") and "[1:a]" in graph
    assert muxed[muxed.index("-map") + 1] == "[v]"
    assert "-vf" not in muxed and "-af" not in muxed


def test_pair_overlays_folds_broll_rows_and_skips_orphans():
    rows = [
        {"id": "p1", "track_kind": "video", "asset_path": "/a.mp4"},
        {
            "id": "o1",
            "track_kind": "broll",
            "asset_path": "/b.mp4",
            "source_start_sec": 1.0,
            "source_end_sec": 3.0,
            "overlay_json": json.dumps({"overlay_of": "p1"}),
        },
        {
            "id": "o2",
            "track_kind": "broll",
            "asset_path": "/c.mp4",
            "source_start_sec": 0.0,
            "source_end_sec": 1.0,
            "overlay_json": json.dumps({"overlay_of": "missing"}),
        },
    ]
    primaries = _pair_overlays(rows)
    assert [row["id"] for row in primaries] == ["p1"]
    assert primaries[0]["overlay_render"]["video_path"] == "/b.mp4"
    assert primaries[0]["overlay_render"]["video_start_sec"] == 1.0


def test_render_end_to_end_with_overlay(tmp_path, run_ave):
    # 6s fixtures: the pre-existing xfade/acrossfade join chain cannot digest
    # sub-second clips (fails with or without overlays); realistic clip lengths
    # keep this test about the overlay mux, not that quirk.
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3, seconds=6)
    run_ave(
        "timeline",
        project_dir,
        "--directive",
        MINIDOC_DIRECTIVE,
        "--duration-sec",
        "30",
        "--context-aware",
        "--provider",
        "mock",
    )

    result = _json(run_ave("render", project_dir, "--json", timeout=120))
    assert result["status"] == "complete"
    assert Path(result["path"]).exists()
