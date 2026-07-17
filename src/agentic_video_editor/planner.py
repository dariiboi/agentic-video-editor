from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from .casting import AnchorResolutionError, _packet_tokens, _terms, cast_structure, resolve_anchors
from .cutpoints import anchor_trim
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL
from .intent import analyze_intent
from .project import Project, utc_now
from .retrieval import context_search
from .structure import author_structure, expand_structure, intensity_to_weight, validate_ordering
from .subtract import plan_subtraction


# Fallback pacing ONLY for structures that omit intensity targets; the
# structure document's intensity curve is the real pacing surface.
ROLE_PACING = {
    "hook": {"weight": 0.8, "max_multiplier": 1.0, "why": "hooks cut fast to earn attention"},
    "reaction": {"weight": 0.8, "max_multiplier": 1.0, "why": "reactions are punctuation, keep them short"},
    "performance": {"weight": 1.1, "max_multiplier": 1.5, "why": "performance sustains energy"},
    "emotion": {"weight": 1.2, "max_multiplier": 1.75, "why": "emotional beats need room to land"},
    "payoff": {"weight": 1.4, "max_multiplier": 2.0, "why": "the payoff must breathe to feel like an ending"},
}

DEFAULT_PACING = {"weight": 1.0, "max_multiplier": 1.5, "why": "steady middle-of-cut pacing"}

MIN_ENDING_SEC = 2.0


def _sequence_item(beat: dict[str, Any], packet: dict[str, Any], index: int, duration_sec: float) -> dict[str, Any]:
    trim_start, trim_end = (float(value) for value in packet["trim_range"])
    asset_duration = packet.get("asset_duration_sec")
    if asset_duration:
        # legacy selects sometimes claim trim ranges past the end of the file
        trim_end = min(trim_end, float(asset_duration) - 0.05)
        trim_start = min(trim_start, max(0.0, trim_end - 0.1))
    raw_duration = max(0.1, trim_end - trim_start)
    target = max(1.0, float(beat.get("target_duration_sec") or raw_duration))
    max_sec = float(beat.get("max_duration_sec") or target * DEFAULT_PACING["max_multiplier"])
    clip_duration = min(raw_duration, max(2.0, target), max(2.0, duration_sec))
    word_units = packet.get("source_evidence", {}).get("word_units") or []
    source_start, source_end, trim_anchor = anchor_trim(
        trim_start,
        trim_end,
        clip_duration,
        word_units,
        hint=str(packet.get("source_evidence", {}).get("select_reason") or ""),
    )
    return {
        "sequence_index": index,
        "beat_id": beat["id"],
        "beat_role": beat["role"],
        "segment_id": packet["segment_id"],
        "select_id": packet.get("select_id"),
        "asset_id": packet["asset_id"],
        "file_name": packet["file_name"],
        "source_start_sec": round(source_start, 3),
        "source_end_sec": round(source_end, 3),
        "duration_sec": round(source_end - source_start, 3),
        "target_duration_sec": round(target, 3),
        "max_available_sec": round(min(max_sec, trim_end - source_start), 3),
        "max_duration_sec": round(max_sec, 3),
        "trim_anchor": trim_anchor,
        "pacing": beat.get("pacing"),
        "story_roles": packet.get("story_roles", []),
        "why_here": _why_here(beat, packet),
        "caption_text": packet.get("caption_text"),
        "transition_note": _transition_note(index, beat["role"], packet),
        "continuity_score": packet.get("continuity_compatibility", 0.6),
        "warnings": packet.get("warnings", []),
        "source_evidence": packet.get("source_evidence", {}),
    }


def _assign_timing(selected_sequence: list[dict[str, Any]], duration_sec: float, *, reserve_ending: bool = True) -> None:
    if not selected_sequence:
        return
    targets = [max(0.5, float(item.get("target_duration_sec") or item["duration_sec"])) for item in selected_sequence]
    caps = [max(0.5, float(item.get("max_available_sec") or item["duration_sec"])) for item in selected_sequence]
    durations = _fit_durations(targets, caps, duration_sec)
    if reserve_ending:
        _reserve_ending(durations, caps)

    cursor = 0.0
    for item, duration in zip(selected_sequence, durations):
        end = item["source_start_sec"] + duration
        end = _respect_word_units(item, item["source_start_sec"], end)
        duration = end - item["source_start_sec"]
        item["duration_sec"] = round(duration, 3)
        item["source_end_sec"] = round(end, 3)
        item["timeline_start_sec"] = round(cursor, 3)
        item["timeline_end_sec"] = round(cursor + duration, 3)
        cursor += duration


def _fit_durations(targets: list[float], caps: list[float], budget: float) -> list[float]:
    """Scale targets to the budget proportionally instead of greedily.

    The old left-to-right refit made later clips absorb all accumulated error,
    which is why payoffs kept getting crushed.
    """
    durations = [min(target, cap) for target, cap in zip(targets, caps)]
    for _ in range(5):
        total = sum(durations)
        if total <= 0 or abs(total - budget) < 0.05:
            break
        scale = budget / total
        durations = [min(cap, max(0.5, duration * scale)) for duration, cap in zip(durations, caps)]
    return durations


def _reserve_ending(durations: list[float], caps: list[float]) -> None:
    """The final clip must be long enough to land as an ending."""
    minimum = min(MIN_ENDING_SEC, caps[-1])
    deficit = minimum - durations[-1]
    if deficit <= 0:
        return
    durations[-1] = minimum
    pool = sum(max(0.0, duration - 1.0) for duration in durations[:-1])
    if pool <= 0:
        return
    for index in range(len(durations) - 1):
        share = max(0.0, durations[index] - 1.0) / pool
        durations[index] = max(1.0, durations[index] - deficit * share)


def _respect_word_units(item: dict[str, Any], start: float, end: float) -> float:
    """Never let a duration change cut a word unit in half.

    Extend to the unit's end when the source allows it, otherwise pull the
    out-point back to just before the unit begins.
    """
    units = item.get("source_evidence", {}).get("word_units") or []
    max_end = start + float(item.get("max_available_sec") or (end - start))
    for unit in units:
        if not isinstance(unit, dict):
            continue
        try:
            unit_start = float(unit["start_sec"])
            unit_end = float(unit["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if unit_start < end < unit_end:
            if unit_end + 0.1 <= max_end:
                end = unit_end + 0.1
                item["word_unit_guard"] = {
                    "action": "extended",
                    "why": f'let "{str(unit.get("text") or "")[:48]}" finish before the cut',
                }
            else:
                end = max(start + 0.5, unit_start - 0.05)
                item["word_unit_guard"] = {
                    "action": "excluded",
                    "why": f'no room to finish "{str(unit.get("text") or "")[:48]}"; cut before it starts',
                }
    return end


def _continuity_warnings(selected_sequence: list[dict[str, Any]]) -> list[str]:
    warnings = []
    for previous, current in zip(selected_sequence, selected_sequence[1:]):
        if previous["asset_id"] == current["asset_id"]:
            warnings.append(f"Possible redundancy between {previous['segment_id']} and {current['segment_id']}.")
        if "check_audio_transition" in current.get("warnings", []):
            warnings.append(f"Check or crossfade audio into {current['segment_id']}.")
    return _dedupe(warnings)


def _why_here(beat: dict[str, Any], packet: dict[str, Any]) -> str:
    reasons = "; ".join(packet.get("why_matches", [])[:2])
    return f"{beat['role']} beat: {reasons or packet['source_evidence'].get('summary', 'selected for the directive')}."


def _transition_note(index: int, role: str, packet: dict[str, Any]) -> str:
    del role
    if index == 0:
        return "Open cleanly; establish the subject before adding context."
    if "check_audio_transition" in packet.get("warnings", []):
        return "Use a short crossfade or continuous bed to avoid a jarring music cut."
    return "Bridge with a simple cut or caption if the source era/style changes."


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = _norm(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


# --- The edit-plan engine (IntentAgent -> StructureAgent -> cast -> compile) --


def create_edit_plan(
    project: Project,
    *,
    directive: str,
    duration_sec: float = 60.0,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    source: str = "structured",
    store: bool = True,
) -> dict[str, Any]:
    """Directive -> intent -> ad-hoc structure -> QueryAgent/CastingAgent -> timing.

    An unresolvable user_explicit anchor raises AnchorResolutionError after
    storing a failed plan record — pinned content is never silently skipped.
    """
    intent = analyze_intent(
        project,
        directive,
        duration_sec=duration_sec,
        provider_name=provider_name,
        model=model,
        env_path=env_path,
        store=store,
    )
    mode = intent["operation"]["mode"]
    if mode == "subtract":
        plan = plan_subtraction(project, intent, duration_sec=duration_sec)
        if store:
            _store_structured_plan(project, intent, plan, source)
        return plan
    casting_warnings: list[str] = []
    if mode == "transform":
        casting_warnings.append(
            "transform mode is not implemented yet (revision lineage lands in a later phase); planning as compose"
        )
    structure = author_structure(
        project,
        intent,
        duration_sec=duration_sec,
        provider_name=provider_name,
        model=model,
        env_path=env_path,
        store=store,
    )
    slots = expand_structure(structure, duration_sec=duration_sec)
    slots = _expand_generator_slots(project, slots, casting_warnings)
    _assign_slot_targets(slots, duration_sec)

    anchor_resolution = resolve_anchors(project, intent)
    for anchor in anchor_resolution["unresolved_soft"]:
        casting_warnings.append(f"anchor {anchor['description']!r} skipped: {anchor['why']}")
    if anchor_resolution["failures"]:
        failed = {
            "plan_id": f"plan_{uuid.uuid4().hex[:16]}",
            "engine": "structured",
            "status": "failed_anchor_resolution",
            "directive": directive,
            "duration_target_sec": duration_sec,
            "intent_analysis": intent,
            "structure_id": structure.get("structure_id"),
            "anchor_failures": anchor_resolution["failures"],
            "selected_sequence": [],
        }
        if store:
            _store_structured_plan(project, intent, failed, source)
        raise AnchorResolutionError(anchor_resolution["failures"])

    casting = cast_structure(
        project,
        intent,
        structure,
        slots,
        anchors_resolved=anchor_resolution["resolved"],
        provider_name=provider_name,
        model=model,
        env_path=env_path,
        warnings=casting_warnings,
    )
    items = _decisions_to_items(casting["decisions"], structure, duration_sec, casting_warnings)
    _assign_timing(items, duration_sec, reserve_ending=bool(structure["ending_policy"].get("reserve_ending", True)))
    plan = {
        "plan_id": f"plan_{uuid.uuid4().hex[:16]}",
        "engine": "structured",
        "status": "ok",
        "directive": directive,
        "duration_target_sec": duration_sec,
        "intent_analysis": intent,
        "structure_id": structure.get("structure_id"),
        "structure": {key: value for key, value in structure.items() if key != "raw_response"},
        "expanded_slots": slots,
        "selected_sequence": items,
        "coverage_report": casting["coverage_report"],
        "anchors_resolved": [
            {"description": anchor["description"], "position": anchor["position"], "segment_id": anchor["segment_id"]}
            for anchor in anchor_resolution["resolved"]
        ],
        "sanctioned_reuse": casting["sanctioned_reuse"],
        "casting_warnings": casting_warnings,
        "ordering_violations": validate_ordering(structure, items),
        "continuity_warnings": _continuity_warnings(items),
        "sequencing_note": "QueryAgent/CastingAgent casting: facet-filtered packets, ID-only selection, code-enforced lanes/withhold/novelty",
    }
    if store:
        _store_structured_plan(project, intent, plan, source)
    return plan


def _decisions_to_items(
    decisions: list[dict[str, Any]],
    structure: dict[str, Any],
    duration_sec: float,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Assemble sequence items from casting decisions (shots share slot targets)."""
    hints = structure.get("transition_policy_hints") or {}
    shots_per_slot: dict[str, int] = {}
    for decision in decisions:
        slot_id = decision["slot"]["slot_id"]
        shots_per_slot[slot_id] = max(shots_per_slot.get(slot_id, 0), decision["shot_index"] + 1)

    items: list[dict[str, Any]] = []
    for decision in decisions:
        slot = decision["slot"]
        packet = decision["packet"]
        per_shot_target = slot["target_duration_sec"] / shots_per_slot[slot["slot_id"]]
        beat = {
            "id": slot["slot_id"],
            "role": slot["function"],
            "target_duration_sec": round(per_shot_target, 3),
            "max_duration_sec": slot["max_duration_sec"],
            "pacing": {"weight": slot["pacing_weight"], "why": slot["pacing_why"]},
        }
        item = _sequence_item(beat, packet, len(items), duration_sec)
        item["slot_id"] = slot["slot_id"]
        item["beat_id"] = slot["beat_id"]
        item["lane"] = slot.get("lane")
        item["intensity"] = slot.get("intensity")
        item["motif"] = slot.get("motif")
        item["withhold"] = slot.get("withhold") or []
        item["recontextualizes"] = slot.get("recontextualizes")
        item["anchor"] = slot.get("anchor")
        item["why_here"] = _structured_why(slot, packet)
        item["casting_why"] = decision["why"]
        item["risks"] = decision["risks"]
        item["alternates"] = decision["alternates"]
        item["matched_via"] = decision["matched_via"]
        item["pair_features_prev"] = decision["pair_features_prev"]
        hint = hints.get(slot["function"])
        if hint:
            item["transition_note"] = f"{hint} (structure hint for {slot['function']})"
        items.append(item)
    _link_recontextualizations(items, decisions, warnings)
    return items


def _expand_generator_slots(
    project: Project,
    slots: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Unroll enumerate generator slots into one concrete slot per match."""
    expanded: list[dict[str, Any]] = []
    for slot in slots:
        generator = slot.get("generator")
        if not generator:
            expanded.append(slot)
            continue
        cap = int(generator["cap"])
        packets = context_search(project, generator["from_query"], limit=max(24, cap * 3))["packets"]
        packets, match_terms = _generator_matches(generator["from_query"], packets)
        packets = packets[:cap]
        if generator.get("order") == "chronological":
            packets.sort(key=lambda packet: (packet["file_name"], float(packet["trim_range"][0])))
        if not packets:
            warnings.append(f"slot {slot['slot_id']} uncast: no evidence matches {generator['from_query']!r}")
            continue
        for index, packet in enumerate(packets):
            concrete = dict(slot)
            concrete["slot_id"] = f"{slot['beat_id']}#{index + 1}"
            concrete["generator"] = None
            concrete["generated_from"] = generator["from_query"]
            concrete["generated_match"] = sorted(match_terms)
            concrete["preselected_packet"] = packet
            expanded.append(concrete)
    for position, slot in enumerate(expanded):
        slot["position"] = position
    return expanded


def _generator_matches(
    from_query: str,
    packets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Enumerate beats keep only packets whose evidence actually matches.

    A quoted phrase must appear in full; otherwise any content term counts.
    Retrieval's zero-score fallback never qualifies — a supercut of noise is
    worse than an honest "no matches".
    """
    quoted = re.search(r"['\"]([^'\"]+)['\"]", from_query)
    if quoted:
        required = _terms(quoted.group(1))
        need_all = True
    else:
        required = {term for term in _terms(from_query) if len(term) > 2}
        need_all = False
    if not required:
        return packets, set()
    kept = []
    for packet in packets:
        tokens = _packet_tokens(packet)
        if (need_all and required <= tokens) or (not need_all and required & tokens):
            kept.append(packet)
    return kept, required


def _link_recontextualizations(
    items: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    """Attach the linking evidence for reveal pairs; absence is flagged, not hidden."""
    packets_by_segment: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        packets_by_segment.setdefault(decision["packet"]["segment_id"], decision["packet"])
    first_by_beat: dict[str, dict[str, Any]] = {}
    for item in items:
        first_by_beat.setdefault(item["beat_id"], item)
    for item in items:
        target = item.get("recontextualizes")
        if not target:
            continue
        earlier = first_by_beat.get(target)
        if not earlier or earlier["sequence_index"] >= item["sequence_index"]:
            warnings.append(f"item {item['slot_id']} recontextualizes {target!r} but no earlier cast item exists")
            continue
        packet = packets_by_segment.get(item["segment_id"], {})
        edges = [
            rel
            for rel in packet.get("relationship_expansion") or []
            if rel.get("segment_id") == earlier["segment_id"]
        ]
        link = {
            "of_beat": target,
            "earlier_segment_id": earlier["segment_id"],
            "earlier_setup_questions": earlier.get("source_evidence", {}).get("setup_questions") or [],
            "payoff_answers": item.get("source_evidence", {}).get("payoff_answers") or [],
            "relationship_edges": edges,
        }
        item["recontextualization_link"] = link
        if not (link["earlier_setup_questions"] or link["payoff_answers"] or edges):
            warnings.append(
                f"item {item['slot_id']} recontextualizes {target} but no setup/payoff "
                "or relationship evidence links the pair"
            )


def _assign_slot_targets(slots: list[dict[str, Any]], duration_sec: float) -> None:
    """Intensity -> duration weight; ROLE_PACING only when intensity is absent."""
    for slot in slots:
        if slot.get("intensity") is not None:
            slot["pacing_weight"] = intensity_to_weight(slot["intensity"])
            slot["pacing_why"] = f"intensity target {slot['intensity']} from the structure"
            slot["max_multiplier"] = DEFAULT_PACING["max_multiplier"]
        else:
            fallback = ROLE_PACING.get(_norm(slot["function"]), DEFAULT_PACING)
            slot["pacing_weight"] = fallback["weight"]
            slot["pacing_why"] = f"structure omitted intensity; role-pacing fallback ({fallback['why']})"
            slot["max_multiplier"] = fallback["max_multiplier"]
    total = sum(slot["pacing_weight"] for slot in slots) or 1.0
    for slot in slots:
        target = max(1.5, duration_sec * slot["pacing_weight"] / total)
        slot["target_duration_sec"] = round(target, 3)
        slot["max_duration_sec"] = round(target * slot["max_multiplier"], 3)


def _structured_why(slot: dict[str, Any], packet: dict[str, Any]) -> str:
    reasons = "; ".join(packet.get("why_matches", [])[:2])
    intensity = f", intensity {slot['intensity']}" if slot.get("intensity") is not None else ""
    facets = packet.get("source_evidence", {}).get("facets") or []
    facet_bit = ""
    if facets:
        evidence = facets[0].get("evidence") or facets[0].get("text")
        if evidence:
            facet_bit = f" Facet evidence: {evidence}"
    core = reasons or str(packet.get("source_evidence", {}).get("summary") or "cast for the directive")
    return f"{slot['function']} beat{intensity}: {core}.{facet_bit}"


def _store_structured_plan(project: Project, intent: dict[str, Any], plan: dict[str, Any], source: str) -> None:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into edit_plans (
                id, project_id, directive_id, intent_analysis_id, plan_json, source, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan["plan_id"],
                "default",
                intent.get("directive_id"),
                intent.get("intent_id"),
                json.dumps(plan, indent=2),
                source,
                utc_now(),
            ),
        )
        conn.commit()


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
