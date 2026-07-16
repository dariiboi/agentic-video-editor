import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor.render import _joined_command, _planned_joins  # noqa: E402
from agentic_video_editor.timeline import _assign_transitions, _decide_transition  # noqa: E402


def _item(asset_id, warnings=None, audio=None):
    return {"asset_id": asset_id, "warnings": warnings or [], "audio_affordance": audio}


def test_same_source_join_gets_a_dissolve():
    transition = _decide_transition(_item("asset_a"), _item("asset_a"))
    assert transition["type"] == "crossfade"
    assert "jump cut" in transition["why"]


def test_dialogue_boundary_gets_a_hard_cut():
    transition = _decide_transition(
        _item("asset_a", audio="clean_dialogue"),
        _item("asset_b", audio="clean_dialogue"),
    )
    assert transition["type"] == "cut"


def test_music_to_music_gets_a_crossfade():
    transition = _decide_transition(
        _item("asset_a", audio="music_bed"),
        _item("asset_b", audio="music_bed"),
    )
    assert transition["type"] == "crossfade"


def test_abrupt_audio_warning_gets_a_crossfade():
    transition = _decide_transition(
        _item("asset_a"),
        _item("asset_b", warnings=["abrupt_audio_or_no_audio"]),
    )
    assert transition["type"] == "crossfade"


def test_assign_transitions_marks_opening_clip_as_cut():
    items = [_item("asset_a"), _item("asset_a"), _item("asset_b")]
    _assign_transitions(items)
    assert items[0]["transition"]["type"] == "cut"
    assert items[1]["transition"]["type"] == "crossfade"  # same source
    assert items[2]["transition"]["type"] == "cut"  # no risk signals


def test_planned_joins_honors_per_item_transitions_and_override():
    items = [
        {"transition": {"type": "cut"}},
        {"transition": {"type": "crossfade", "duration_sec": 0.4}},
        {"transition_json": '{"type": "cut", "why": "dialogue"}'},
    ]
    joins = _planned_joins(items, override_crossfade_sec=0.0)
    assert [j["type"] for j in joins] == ["crossfade", "cut"]
    assert joins[0]["duration_sec"] == 0.4

    forced = _planned_joins(items, override_crossfade_sec=0.25)
    assert [j["type"] for j in forced] == ["crossfade", "crossfade"]
    assert all(j["duration_sec"] == 0.25 for j in forced)


def test_joined_command_mixes_xfade_and_concat_per_join():
    paths = [Path("/tmp/a.mp4"), Path("/tmp/b.mp4"), Path("/tmp/c.mp4")]
    joins = [{"type": "crossfade", "duration_sec": 0.4}, {"type": "cut"}]
    command = _joined_command(paths, [4.0, 4.0, 4.0], joins, Path("/tmp/out.mp4"))
    filtergraph = command[command.index("-filter_complex") + 1]
    assert filtergraph.count("xfade") == 1
    assert filtergraph.count("acrossfade") == 1
    assert filtergraph.count("concat=n=2:v=1:a=0") == 1
    assert filtergraph.count("concat=n=2:v=0:a=1") == 1
    # xfade offset = first clip duration minus the fade
    assert "offset=3.600" in filtergraph


def test_joined_command_falls_back_to_cut_when_clips_too_short():
    paths = [Path("/tmp/a.mp4"), Path("/tmp/b.mp4")]
    joins = [{"type": "crossfade", "duration_sec": 2.0}]
    command = _joined_command(paths, [0.05, 4.0], joins, Path("/tmp/out.mp4"))
    filtergraph = command[command.index("-filter_complex") + 1]
    assert "xfade" not in filtergraph
    assert "concat" in filtergraph
