"""Acceptance tests 1-15 from docs/design/generalized-directive-engine-handoff.md.

Mock-provider only (the ⚑ real-footage variants run manually on a real corpus).
Scenarios 7 (facet ingest breadth: test_facets.py + test_facet_search.py) and
8 (budget-mode schema parity: test_facets.py) are already pinned by module
tests; everything here asserts at the plan level.
"""

import json
import statistics
import sys
from pathlib import Path

from helpers import make_mp4

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import agentic_video_editor.planner as planner_module  # noqa: E402

BATTLE_DIRECTIVE = (
    "there are various teams with different t-shirt colors, "
    "create a battle between green t-shirts and blue t-shirts"
)

DOCUMENTARY_DEFAULT_ROLES = {"hook", "context", "process", "performance", "emotion", "payoff"}


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


def _plan(project_dir, run_ave, directive, duration="30"):
    return _json(
        run_ave(
            "edit-plan",
            project_dir,
            "--directive",
            directive,
            "--duration-sec",
            duration,
            "--provider",
            "mock",
            "--json",
        )
    )


def _facet_text(item, facet_type=None):
    return " ".join(
        f"{facet.get('text') or ''} {facet.get('evidence') or ''}"
        for facet in item["source_evidence"].get("facets") or []
        if facet_type is None or facet.get("observation_type") == facet_type
    ).lower()


# 1 + 6: t-shirt battle — lanes, correct-color casting, escalation timing,
# evidence-citing whys, and the plan compiles to a timeline.
def test_acceptance_battle_lanes_casting_escalation_and_whys(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _plan(project_dir, run_ave, BATTLE_DIRECTIVE, "60")

    assert {lane["id"] for lane in plan["structure"]["lanes"]} == {"green", "blue"}
    assert any(beat["pattern"] == "alternate" for beat in plan["structure"]["beats"])

    # every lane-bound item's facet evidence places the right color in frame
    lane_items = [item for item in plan["selected_sequence"] if item.get("lane")]
    assert lane_items
    for item in lane_items:
        assert item["lane"] in _facet_text(item, "people_appearance")

    # escalation ramp: intensities rise, per-slot targets tighten monotonically
    escalation = [slot for slot in plan["expanded_slots"] if slot["function"] == "escalation"]
    assert len(escalation) >= 2
    intensities = [slot["intensity"] for slot in escalation]
    targets = [slot["target_duration_sec"] for slot in escalation]
    assert intensities == sorted(intensities) and intensities[0] < intensities[-1]
    assert targets == sorted(targets, reverse=True) and targets[0] > targets[-1]

    # acceptance 6: every item's why cites the invented function AND observation evidence
    for item in plan["selected_sequence"]:
        assert item["why_here"].startswith(item["beat_role"])
        assert "Facet evidence:" in item["why_here"]

    # the plan compiles into a real timeline
    timeline = _json(
        run_ave(
            "timeline",
            project_dir,
            "--directive",
            BATTLE_DIRECTIVE,
            "--duration-sec",
            "10",
            "--max-clip-sec",
            "2",
            "--context-aware",
            "--provider",
            "mock",
            "--json",
        )
    )
    assert timeline["items_created"] >= 1


# 2: word story — the structure carries a word spine of verbatim spans with timestamps.
def test_acceptance_word_story_has_verbatim_word_spine(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(project_dir, run_ave, "tell a small story through the spoken words", "40")

    assert plan["intent_analysis"]["edit_type"] == "word_story"
    spine = plan["structure"]["word_spine"]
    assert spine
    for entry in spine:
        assert entry["text"]
        assert entry["end_sec"] > entry["start_sec"]

    # spine lines are verbatim indexed word units, not invented text/timestamps
    indexed_units = {
        (unit["text"], float(unit["start_sec"]), float(unit["end_sec"]))
        for item in plan["selected_sequence"]
        for unit in item["source_evidence"]["word_units"]
    }
    for entry in spine:
        assert (entry["text"], entry["start_sec"], entry["end_sec"]) in indexed_units

    # beats ride the spine: the carrying line is quoted in a beat's word need
    quoted = f'"{spine[0]["text"]}"'
    assert any(quoted in (beat.get("word_need") or "") for beat in plan["structure"]["beats"])


# 3: trailer — trailer-invented functions, hot open, no forced breathing ending.
def test_acceptance_trailer_functions_are_trailer_invented(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(project_dir, run_ave, "cut a punchy teaser trailer", "30")

    functions = [beat["function"] for beat in plan["structure"]["beats"]]
    assert not (set(functions) & DOCUMENTARY_DEFAULT_ROLES)
    assert plan["structure"]["beats"][0]["intensity_target"] >= 0.8  # opens hot
    assert plan["structure"]["ending_policy"]["reserve_ending"] is False
    assert plan["structure"]["fallback"] is False
    assert plan["selected_sequence"]


# 4: mini-doc — grounding precedes the landing, emergent from the structure
# (DEFAULT_BEATS no longer exists to impose it).
def test_acceptance_minidoc_context_precedes_payoff_emergently(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _plan(project_dir, run_ave, "make a mini documentary about the crew", "45")

    assert not hasattr(planner_module, "DEFAULT_BEATS")
    functions = [beat["function"] for beat in plan["structure"]["beats"]]
    assert functions.index("arrive_in_the_place") < functions.index("where_it_lands")

    sequence_beats = [item["beat_id"] for item in plan["selected_sequence"]]
    assert sequence_beats.index("m1") < sequence_beats.index("m5")


# 5: nonsense attributes — zero coverage is reported; faction lanes cast nothing
# instead of casting noise.
def test_acceptance_nonsense_attributes_report_zero_coverage(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(project_dir, run_ave, "create a battle between red hats and top hats", "30")

    assert {"red hats", "top hats"} <= set(plan["coverage_report"]["zero_coverage"])
    assert {lane["id"] for lane in plan["structure"]["lanes"]} == {"red", "top"}
    assert any("no candidates satisfy its filters" in warning for warning in plan["casting_warnings"])
    # nothing was noise-cast into the zero-evidence faction lanes
    assert all(item.get("lane") is None for item in plan["selected_sequence"])


# 9: vague directive — nearly-all-agent provenance, structure pitched from
# corpus_profile facts, no genre default.
def test_acceptance_vague_directive_pitches_from_the_footage(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _plan(project_dir, run_ave, "make something cool", "20")

    intent = plan["intent_analysis"]
    assert intent["edit_type"] == "footage_first_pitch"
    provenances = [requirement["provenance"] for requirement in intent["requirements"]]
    assert provenances.count("agent") >= len(provenances) - 1
    for requirement in intent["requirements"]:
        if requirement["provenance"] == "agent":
            assert requirement["why"]

    logline = plan["structure"]["logline"]
    assert "3 clip" in logline  # asset count from the corpus profile
    assert "t-shirt" in logline  # top indexed facet term
    functions = {beat["function"] for beat in plan["structure"]["beats"]}
    assert not (functions & DOCUMENTARY_DEFAULT_ROLES)


# 10: pinned + over-constrained — the anchor lands first, the infeasible
# duration is surfaced, nothing is silently overridden.
def test_acceptance_pinned_and_overconstrained_surfaces_conflict(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(
        project_dir,
        run_ave,
        "open on the green t-shirt performer, then celebrate the whole crew",
        "600",
    )

    intent = plan["intent_analysis"]
    duration_conflicts = [
        conflict for conflict in intent["conflicts"] if "duration" in " ".join(conflict["between"]).lower()
    ]
    assert duration_conflicts
    assert duration_conflicts[0]["resolution"] == "surface_to_user"
    assert intent["hard_constraints"]["duration_sec"]["provenance"] == "user_explicit"
    assert plan["duration_target_sec"] == 600  # never silently reduced

    assert plan["anchors_resolved"] and plan["anchors_resolved"][0]["position"] == "first"
    assert plan["selected_sequence"][0]["segment_id"] == plan["anchors_resolved"][0]["segment_id"]


# 11: supercut — enumerate mode, every item carries a matching verbatim span,
# chronological order; a phrase with no evidence casts nothing.
def test_acceptance_supercut_items_carry_matching_spans(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=3)

    plan = _plan(project_dir, run_ave, 'show every time someone says "opening"', "30")

    assert plan["intent_analysis"]["operation"]["mode"] == "enumerate"
    generated = [slot for slot in plan["expanded_slots"] if slot.get("generated_from")]
    assert generated and all("opening" in slot["generated_from"] for slot in generated)
    assert all("opening" in slot["generated_match"] for slot in generated)
    assert plan["selected_sequence"]
    for item in plan["selected_sequence"]:
        unit_text = " ".join(
            str(unit.get("text") or "") for unit in item["source_evidence"]["word_units"]
        ).lower()
        assert "opening" in unit_text
    starts = [(item["file_name"], item["source_start_sec"]) for item in plan["selected_sequence"]]
    assert starts == sorted(starts)

    # a phrase the corpus never says yields an honest empty cut, not noise
    empty = _plan(project_dir, run_ave, 'show every time someone says "love"', "30")
    assert empty["selected_sequence"] == []
    assert any("no evidence matches" in warning for warning in empty["casting_warnings"])


# 12: buildup — rising intensity ramp lands as a monotonically tightening cut rate.
def test_acceptance_buildup_cut_rate_tightens_monotonically(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(project_dir, run_ave, "one continuous build in tension to the end", "45")

    tighten = [slot for slot in plan["expanded_slots"] if slot["beat_id"] == "t2"]
    assert len(tighten) == 5
    intensities = [slot["intensity"] for slot in tighten]
    targets = [slot["target_duration_sec"] for slot in tighten]
    assert intensities == sorted(intensities) and intensities[0] < intensities[-1]
    assert targets == sorted(targets, reverse=True) and targets[0] > targets[-1]

    detonation = next(slot for slot in plan["expanded_slots"] if slot["beat_id"] == "t3")
    assert detonation["target_duration_sec"] < min(targets)  # peak cuts fastest


# 13: motif bookend — same material opens and closes, the return is shorter,
# and the reuse is recorded as sanctioned.
def test_acceptance_bookend_motif_returns_shorter(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(project_dir, run_ave, "bookend the film with the banner image", "20")

    sequence = plan["selected_sequence"]
    first = next(item for item in sequence if item.get("motif") and item["motif"]["occurrence"] == 1)
    second = next(item for item in sequence if item.get("motif") and item["motif"]["occurrence"] == 2)
    assert sequence[0]["slot_id"] == first["slot_id"]  # opens the piece
    assert sequence[-1]["slot_id"] == second["slot_id"]  # closes the piece
    assert first["segment_id"] == second["segment_id"]

    slot_targets = {slot["slot_id"]: slot["target_duration_sec"] for slot in plan["expanded_slots"]}
    assert slot_targets[second["slot_id"]] < slot_targets[first["slot_id"]]

    assert plan["sanctioned_reuse"]
    reuse = plan["sanctioned_reuse"][0]
    assert reuse["segment_id"] == second["segment_id"]
    assert reuse["transform"]


# 14: pathos ending — the final beat is the longest hold, low intensity, cast
# on emotion evidence, with a dissolve-permitting transition hint.
def test_acceptance_pathos_ending_holds_long_and_low(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(project_dir, run_ave, "make it feel like a memory fading", "30")

    slots = plan["expanded_slots"]
    final_slot = slots[-1]
    assert final_slot["function"] == "let_go"
    assert final_slot["target_duration_sec"] == max(slot["target_duration_sec"] for slot in slots)
    median_intensity = statistics.median(slot["intensity"] for slot in slots)
    assert final_slot["intensity"] <= median_intensity

    last_item = plan["selected_sequence"][-1]
    assert last_item["beat_role"] == "let_go"
    assert "dissolve" in last_item["transition_note"].lower()
    assert any(
        facet["observation_type"] == "emotion_tone"
        for facet in last_item["source_evidence"]["facets"]
    )
    assert plan["structure"]["ending_policy"]["reserve_ending"] is True


# 15: recontextualization — a reveal beat points back at an earlier beat and the
# pair is linked by setup/payoff evidence.
def test_acceptance_reveal_recontextualizes_an_earlier_beat(tmp_path, run_ave):
    project_dir = _make_indexed_project(tmp_path, run_ave, clips=2)

    plan = _plan(project_dir, run_ave, "build to a reveal that recasts the opening moment", "30")

    beats = plan["structure"]["beats"]
    reveal = next(beat for beat in beats if beat["recontextualizes"])
    beat_ids = [beat["id"] for beat in beats]
    assert beat_ids.index(reveal["recontextualizes"]) < beat_ids.index(reveal["id"])

    item = next(item for item in plan["selected_sequence"] if item.get("recontextualizes"))
    earlier_item = next(
        candidate
        for candidate in plan["selected_sequence"]
        if candidate["beat_id"] == reveal["recontextualizes"]
    )
    link = item["recontextualization_link"]
    assert link["of_beat"] == reveal["recontextualizes"]
    assert link["earlier_segment_id"] == earlier_item["segment_id"]
    assert link["earlier_setup_questions"]  # what the innocent open raised
    assert link["payoff_answers"]  # what the reveal answers
