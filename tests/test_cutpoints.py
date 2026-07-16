import json
import sys
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor.cutpoints import snap_range, snap_time  # noqa: E402


def _point(time_sec, reason, detector="audio_gap"):
    return {"time_sec": time_sec, "detector": detector, "confidence": None, "reason": reason}


def test_snap_time_prefers_nearest_point_within_tolerance():
    points = [_point(4.8, "shot_change", "ffmpeg_scdet"), _point(6.2, "shot_change", "ffmpeg_scdet")]
    snapped, matched = snap_time(points, 5.0, tolerance_sec=1.0)
    assert snapped == 4.8
    assert matched["time_sec"] == 4.8

    snapped, matched = snap_time(points, 10.0, tolerance_sec=1.0)
    assert snapped == 10.0
    assert matched is None


def test_snap_range_uses_gap_edges_with_margin():
    points = [
        _point(2.0, "gap_end"),
        _point(9.5, "gap_start"),
    ]
    snap = snap_range(points, 2.4, 9.1, tolerance_sec=1.0, margin_sec=0.15)
    # In-point backs off before the speech onset; out-point extends past the phrase end.
    assert snap["start_sec"] == 1.85
    assert snap["end_sec"] == 9.65
    assert snap["start_snapped_to"] == "audio_gap"
    assert snap["end_snapped_to"] == "audio_gap"


def test_snap_range_rejects_collapse_below_min_duration():
    points = [_point(5.0, "gap_end"), _point(5.2, "gap_start")]
    snap = snap_range(points, 4.8, 5.4, tolerance_sec=1.0, min_duration_sec=1.0)
    assert snap["start_sec"] == 4.8
    assert snap["end_sec"] == 5.4
    assert snap["start_snapped_to"] is None
    assert snap["end_snapped_to"] is None


def test_snap_range_does_not_pad_shot_changes():
    points = [_point(3.0, "shot_change", "ffmpeg_scdet"), _point(8.0, "shot_change", "ffmpeg_scdet")]
    snap = snap_range(points, 3.3, 7.8, tolerance_sec=1.0, margin_sec=0.15)
    assert snap["start_sec"] == 3.0
    assert snap["end_sec"] == 8.0


def test_cutpoints_cli_detects_and_summarizes(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    make_mp4(source_dir / "clip_a.mp4", seconds=2)

    run_ave("init", project_dir)
    run_ave("ingest", project_dir, source_dir)
    result = json.loads(run_ave("cutpoints", project_dir, "--json", timeout=60).stdout)
    summary = json.loads(run_ave("cutpoints-summary", project_dir, "--json").stdout)

    assert result["assets_requested"] == 1
    assert result["assets_completed"] == 1
    assert result["scene_points"] >= 0
    assert "cut_points" in summary
