import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from agentic_video_editor.cutpoints import anchor_trim  # noqa: E402
from agentic_video_editor.planner import (  # noqa: E402
    _assign_timing,
    _beat_sheet,
    _fit_durations,
    _reserve_ending,
)
from agentic_video_editor.retrieval import _weight_profile, analyze_directive_intent  # noqa: E402
from agentic_video_editor.semantics import _moment_range  # noqa: E402
from agentic_video_editor.timeline import _assign_captions  # noqa: E402


def test_beat_sheet_weights_payoff_longer_than_hook():
    intent = {"desired_story_roles": ["hook", "context", "performance", "emotion", "payoff"]}
    beats = _beat_sheet(intent, 60.0)
    by_role = {beat["role"]: beat for beat in beats}
    assert by_role["payoff"]["target_duration_sec"] > by_role["hook"]["target_duration_sec"]
    assert abs(by_role["payoff"]["max_duration_sec"] - by_role["payoff"]["target_duration_sec"] * 2.0) < 0.01
    assert by_role["hook"]["pacing"]["why"]
    assert abs(sum(beat["target_duration_sec"] for beat in beats) - 60.0) < 1.0


def test_fit_durations_scales_proportionally_and_respects_caps():
    durations = _fit_durations([10.0, 10.0, 10.0], [12.0, 4.0, 12.0], 20.0)
    assert abs(sum(durations) - 20.0) < 0.1
    assert durations[1] <= 4.0
    # remaining items share the surplus instead of the last absorbing it all
    assert abs(durations[0] - durations[2]) < 0.1


def test_reserve_ending_rescues_a_crushed_payoff():
    durations = [8.0, 8.0, 0.6]
    _reserve_ending(durations, [10.0, 10.0, 6.0])
    assert durations[-1] == 2.0
    assert durations[0] < 8.0 and durations[1] < 8.0


def test_reserve_ending_cannot_exceed_source_cap():
    durations = [8.0, 0.9]
    _reserve_ending(durations, [10.0, 1.2])
    assert durations[-1] == 1.2


def test_assign_timing_does_not_crush_the_last_item():
    items = [
        {
            "source_start_sec": 0.0,
            "duration_sec": 6.0,
            "target_duration_sec": 6.0,
            "max_available_sec": 20.0,
        }
        for _ in range(5)
    ]
    items.append(
        {
            "source_start_sec": 100.0,
            "duration_sec": 8.0,
            "target_duration_sec": 8.0,
            "max_available_sec": 20.0,
        }
    )
    _assign_timing(items, 30.0)
    durations = [item["duration_sec"] for item in items]
    assert durations[-1] >= 2.0
    assert durations[-1] >= max(durations) * 0.5
    assert abs(sum(durations) - 30.0) < 1.0


def test_assign_timing_extends_past_word_unit_when_possible():
    items = [
        {
            "source_start_sec": 10.0,
            "duration_sec": 4.0,
            "target_duration_sec": 4.0,
            "max_available_sec": 8.0,
            "source_evidence": {
                "word_units": [{"text": "the answer", "start_sec": 13.5, "end_sec": 15.0}]
            },
        }
    ]
    _assign_timing(items, 4.0)
    # a 4s cut would land at 14.0, mid-unit; the guard extends to the unit end
    assert items[0]["source_end_sec"] >= 15.0
    assert items[0]["word_unit_guard"]["action"] == "extended"


def test_anchor_trim_prefers_word_unit_over_first_seconds():
    start, end, anchor = anchor_trim(
        0.0,
        30.0,
        5.0,
        [{"text": "music is like breathing", "start_sec": 12.0, "end_sec": 14.5}],
    )
    assert start <= 12.0 <= end
    assert 14.5 <= end + 0.01
    assert "word unit" in anchor["why"]


def test_anchor_trim_centers_without_word_units():
    start, end, anchor = anchor_trim(0.0, 30.0, 6.0, [])
    assert abs(start - 12.0) < 0.01
    assert abs(end - 18.0) < 0.01
    assert "centered" in anchor["why"]


def test_anchor_trim_noop_when_budget_covers_range():
    start, end, anchor = anchor_trim(5.0, 10.0, 6.0, [])
    assert (start, end) == (5.0, 10.0)
    assert anchor is None


def test_weight_profiles_follow_directive_intent():
    word = _weight_profile(analyze_directive_intent("a small story created thru words and lyrics"))
    visual = _weight_profile(analyze_directive_intent("high energy visual montage"))
    default = _weight_profile(analyze_directive_intent("a short documentary about the band"))
    assert word["name"] == "word_driven"
    assert visual["name"] == "visual_driven"
    assert default["name"] == "default"
    assert default["term"] == 2.0 and default["role_base"] == 3.0


def test_assign_captions_honors_needs_caption_and_role_fallback():
    items = [
        {"caption_text": "Archive footage", "role": "context", "needs_caption": 0},
        {"caption_text": "Chorus", "role": "performance", "needs_caption": 0},
        {"caption_text": "He explains", "role": "performance", "needs_caption": 1},
        {"caption_text": None, "role": "context"},
    ]
    _assign_captions(items)
    assert items[0]["caption_decision"]["burn"] is True
    assert items[1]["caption_decision"]["burn"] is False
    assert items[2]["caption_decision"]["burn"] is True
    assert items[3]["caption_decision"]["burn"] is False


def test_moment_range_scales_with_duration():
    assert _moment_range(50.0) == (4, 5)
    assert _moment_range(150.0) == (5, 8)
    assert _moment_range(280.0) == (9, 14)
    assert _moment_range(1200.0) == (16, 16)
    assert _moment_range(None) == (4, 8)
