"""Transform mode: `ave revise` applies a targeted edit script to a stored plan.

Acceptance test 16 (minimal-diff revision) lives here: untouched items must
come out byte-identical to the parent plan, and an unintelligible revision must
return the parent untouched rather than re-rolling the cut.
"""

import json

from helpers import make_mp4

BATTLE_DIRECTIVE = (
    "there are various teams with different t-shirt colors, "
    "create a battle between green t-shirts and blue t-shirts"
)


def _json(result):
    return json.loads(result.stdout)


def _make_indexed_project(tmp_path, run_ave, clips=3, seconds=4):
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


def _plan(project_dir, run_ave, directive, duration="60"):
    return _json(
        run_ave(
            "edit-plan", project_dir, "--directive", directive,
            "--duration-sec", duration, "--provider", "mock", "--json",
        )
    )


def _revise(project_dir, run_ave, directive, check=True):
    return run_ave(
        "revise", project_dir, "--directive", directive, "--provider", "mock", "--json",
        check=check,
    )


def _minus_offsets(item):
    clone = dict(item)
    clone.pop("timeline_start_sec", None)
    clone.pop("timeline_end_sec", None)
    return clone


# Acceptance test 16: "make the middle faster" changes only the middle items;
# the opening item is byte-identical (offsets included) and the ending item is
# identical minus timeline offsets; lineage links revision to parent.
def test_retime_middle_is_a_minimal_diff(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)
    parent = _plan(project_dir, run_ave, BATTLE_DIRECTIVE)
    parent_items = parent["selected_sequence"]
    assert len(parent_items) >= 3

    revision = _json(_revise(project_dir, run_ave, "revise: make the middle faster"))

    assert revision["status"] == "ok"
    assert revision["parent_plan_id"] == parent["plan_id"]
    assert [op["op"] for op in revision["edit_script"]] == ["retime"]

    items = revision["selected_sequence"]
    assert len(items) == len(parent_items)
    count = len(parent_items)
    third = max(1, count // 3)
    middle = set(range(third, count - third)) or {count // 2}

    assert items[0] == parent_items[0]  # byte-identical, offsets included
    assert _minus_offsets(items[-1]) == _minus_offsets(parent_items[-1])
    for index in range(count):
        if index not in middle:
            assert _minus_offsets(items[index]) == _minus_offsets(parent_items[index])
    middle_before = sum(parent_items[index]["duration_sec"] for index in middle)
    middle_after = sum(items[index]["duration_sec"] for index in middle)
    assert middle_after < middle_before

    diff = revision["revision_diff"]
    assert diff["items_changed"] == len(middle)
    assert diff["items_kept"] == count - len(middle)
    assert diff["items_parent"] == count


def test_replace_item_recasts_exactly_one(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=4)
    parent = _plan(project_dir, run_ave, BATTLE_DIRECTIVE)
    parent_items = parent["selected_sequence"]
    assert len(parent_items) >= 2

    revision = _json(_revise(project_dir, run_ave, "revise: replace the second clip"))

    assert revision["status"] == "ok"
    items = revision["selected_sequence"]
    assert len(items) == len(parent_items)
    assert items[1]["segment_id"] != parent_items[1]["segment_id"]
    assert items[0] == parent_items[0]
    for index in range(len(items)):
        if index != 1:
            assert _minus_offsets(items[index]) == _minus_offsets(parent_items[index])
    assert revision["revision_diff"]["items_changed"] == 1
    assert "replaced item 1" in revision["revision_diff"]["changes"][0]["description"]


def test_unrecognized_revision_returns_parent_untouched(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave)
    parent = _plan(project_dir, run_ave, BATTLE_DIRECTIVE)

    revision = _json(_revise(project_dir, run_ave, "revise: sprinkle some pixie dust on it"))

    assert revision["status"] == "revision_not_understood"
    assert revision["parent_plan_id"] == parent["plan_id"]
    assert revision["selected_sequence"] == parent["selected_sequence"]  # no re-roll
    assert revision["revision_diff"]["items_changed"] == 0
    assert "raw_revision_response" in revision
    assert any("no valid operations" in warning for warning in revision["casting_warnings"])


def test_revision_without_parent_plan_fails_loudly(tmp_path, run_ave):
    project_dir = tmp_path / "project"
    run_ave("init", project_dir)

    result = _revise(project_dir, run_ave, "revise: make the middle faster", check=False)

    assert result.returncode == 1
    payload = _json(result)
    assert payload["error"] == "revision_source_missing"


def test_swap_ending_replaces_only_the_last_item(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=4)
    parent = _plan(project_dir, run_ave, BATTLE_DIRECTIVE)
    parent_items = parent["selected_sequence"]

    revision = _json(_revise(project_dir, run_ave, "revise: end on a performance moment"))

    assert revision["status"] == "ok"
    items = revision["selected_sequence"]
    assert len(items) == len(parent_items)
    assert items[-1]["segment_id"] != parent_items[-1]["segment_id"]
    for index in range(len(items) - 1):
        assert _minus_offsets(items[index]) == _minus_offsets(parent_items[index])
    assert revision["revision_diff"]["items_changed"] == 1
