from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .cutpoints import anchor_trim
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL
from .intent import analyze_intent
from .project import Project, utc_now
from .retrieval import analyze_directive_intent, context_search
from .structure import author_structure, expand_structure, intensity_to_weight, validate_ordering


DEFAULT_BEATS = [
    ("hook", "Open with an immediate subject/performance signal."),
    ("context", "Give the viewer documentary context before stacking performances."),
    ("process", "Show creative work, studio energy, or progression."),
    ("emotion", "Deepen the cut with vulnerability, reaction, or expressive vocals."),
    ("payoff", "End on the strongest musical or emotional resolution."),
]

ROLE_PACING = {
    "hook": {"weight": 0.8, "max_multiplier": 1.0, "why": "hooks cut fast to earn attention"},
    "reaction": {"weight": 0.8, "max_multiplier": 1.0, "why": "reactions are punctuation, keep them short"},
    "performance": {"weight": 1.1, "max_multiplier": 1.5, "why": "performance sustains energy"},
    "emotion": {"weight": 1.2, "max_multiplier": 1.75, "why": "emotional beats need room to land"},
    "payoff": {"weight": 1.4, "max_multiplier": 2.0, "why": "the payoff must breathe to feel like an ending"},
}

DEFAULT_PACING = {"weight": 1.0, "max_multiplier": 1.5, "why": "steady middle-of-cut pacing"}

MIN_ENDING_SEC = 2.0

NO_FORCED_ENDING_TERMS = {"trailer", "teaser", "loop", "looping", "cold_open", "cliffhanger"}


def create_edit_plan(
    project: Project,
    *,
    directive: str,
    duration_sec: float = 60.0,
    source: str = "context",
    store: bool = True,
) -> dict[str, Any]:
    intent = analyze_directive_intent(directive)
    beat_sheet = _beat_sheet(intent, duration_sec)
    sequencing_policy = _sequencing_policy(project, intent, beat_sheet)
    candidate_clips = []
    selected_sequence = []
    used_segments: set[str] = set()
    used_assets: set[str] = set()

    for index, beat in enumerate(beat_sheet):
        query = f"{directive} {beat['role']} {beat['retrieval_hint']}"
        search = context_search(project, query, limit=8)
        candidates = search["packets"]
        candidate_clips.append(
            {
                "beat_id": beat["id"],
                "role": beat["role"],
                "candidates": candidates,
            }
        )
        selected = _select_for_beat(
            beat["role"],
            candidates,
            used_segments,
            used_assets,
            prefer_new_asset=sequencing_policy["novelty_all_beats"] or index < 3,
            min_duration_sec=MIN_ENDING_SEC if beat["role"] == "payoff" else None,
        )
        if selected:
            used_segments.add(selected["segment_id"])
            used_assets.add(selected["asset_id"])
            selected_sequence.append(_sequence_item(beat, selected, len(selected_sequence), duration_sec))

    if sequencing_policy["force_payoff_last"]:
        selected_sequence = _ensure_payoff_last(project, directive, selected_sequence, used_segments)
    _assign_timing(selected_sequence, duration_sec)
    continuity_warnings = _continuity_warnings(selected_sequence)
    plan = {
        "plan_id": f"plan_{uuid.uuid4().hex[:16]}",
        "directive": directive,
        "duration_target_sec": duration_sec,
        "intent_analysis": intent,
        "beat_sheet": beat_sheet,
        "candidate_clips_per_beat": candidate_clips,
        "selected_sequence": selected_sequence,
        "caption_plan": [
            {
                "segment_id": item["segment_id"],
                "timeline_start_sec": item["timeline_start_sec"],
                "caption_text": item.get("caption_text"),
            }
            for item in selected_sequence
            if item.get("caption_text")
        ],
        "sequencing_policy": sequencing_policy,
        "continuity_warnings": continuity_warnings,
    }
    if store:
        _store_plan(project, plan, source)
    return plan


def _beat_sheet(intent: dict[str, Any], duration_sec: float) -> list[dict[str, Any]]:
    desired = intent.get("desired_story_roles") or []
    roles = []
    for role, _description in DEFAULT_BEATS:
        if role in desired or not desired:
            roles.append(role)
    for role in desired:
        if role not in roles and role in {"archive", "performance", "reaction"}:
            roles.insert(max(1, len(roles) - 1), role)
    if "payoff" not in roles:
        roles.append("payoff")
    if "hook" not in roles:
        roles.insert(0, "hook")

    roles = _dedupe(roles)
    if len(roles) > 6:
        non_payoff = [role for role in roles if role != "payoff"]
        roles = non_payoff[:5] + ["payoff"]
    if len(roles) < 3:
        roles = ["hook", "context", "payoff"]
    pacing_by_role = {role: ROLE_PACING.get(role, DEFAULT_PACING) for role in roles}
    total_weight = sum(pacing_by_role[role]["weight"] for role in roles)
    beats = []
    for index, role in enumerate(roles):
        description = dict(DEFAULT_BEATS).get(role, f"Use {role} material to serve the directive.")
        pacing = pacing_by_role[role]
        target = max(1.5, duration_sec * pacing["weight"] / total_weight)
        beats.append(
            {
                "id": f"beat_{index + 1}",
                "position": index + 1,
                "role": role,
                "description": description,
                "target_duration_sec": round(target, 3),
                "max_duration_sec": round(target * pacing["max_multiplier"], 3),
                "pacing": {"weight": pacing["weight"], "why": pacing["why"]},
                "retrieval_hint": _retrieval_hint(role),
            }
        )
    return beats


def _sequencing_policy(project: Project, intent: dict[str, Any], beat_sheet: list[dict[str, Any]]) -> dict[str, Any]:
    terms = {_norm(term) for term in intent.get("explicit_terms") or []}
    open_ended = bool(terms & NO_FORCED_ENDING_TERMS)
    with connect_db(project.db_path) as conn:
        migrate(conn)
        row = conn.execute(
            "select count(*) as count from assets where project_id = ? and ingest_status = ?",
            ("default", "ready"),
        ).fetchone()
    asset_count = int(row["count"] or 0)
    novelty_all = asset_count >= len(beat_sheet)
    return {
        "force_payoff_last": not open_ended,
        "force_payoff_last_why": (
            "directive suggests an open-ended form; keeping the cast order"
            if open_ended
            else "documentary-style directive; reserve a payoff for the ending"
        ),
        "novelty_all_beats": novelty_all,
        "novelty_why": (
            f"{asset_count} assets for {len(beat_sheet)} beats; prefer a fresh source at every beat"
            if novelty_all
            else f"only {asset_count} assets; allow source reuse after the opening beats"
        ),
    }


def _retrieval_hint(role: str) -> str:
    return {
        "hook": "opening identity immediate clear",
        "context": "archive backstory documentary setup",
        "archive": "older footage context era",
        "process": "studio creative process collaboration",
        "performance": "live studio performance vocal energy",
        "emotion": "emotional vulnerable expressive reaction",
        "reaction": "reaction human response",
        "payoff": "ending climax musical payoff resolution",
    }.get(role, role)


def _select_for_beat(
    beat_role: str,
    candidates: list[dict[str, Any]],
    used_segments: set[str],
    used_assets: set[str],
    *,
    prefer_new_asset: bool,
    min_duration_sec: float | None = None,
) -> dict[str, Any] | None:
    fresh = [item for item in candidates if item["segment_id"] not in used_segments]
    role_matched = [item for item in fresh if _role_matches_beat(item, beat_role)]
    if role_matched:
        fresh = role_matched
    if beat_role != "payoff":
        not_ending = [item for item in fresh if "save_for_ending" not in item.get("warnings", [])]
        if not_ending:
            fresh = not_ending
    if min_duration_sec:
        long_enough = [item for item in fresh if _trim_length(item) >= min_duration_sec]
        if long_enough:
            fresh = long_enough
    if prefer_new_asset:
        new_asset = [item for item in fresh if item["asset_id"] not in used_assets]
        if new_asset:
            return new_asset[0]
    return fresh[0] if fresh else (candidates[0] if candidates else None)


def _trim_length(packet: dict[str, Any]) -> float:
    trim_start, trim_end = packet["trim_range"]
    end = float(trim_end)
    asset_duration = packet.get("asset_duration_sec")
    if asset_duration:
        end = min(end, float(asset_duration) - 0.05)
    return max(0.0, end - float(trim_start))


def _role_matches_beat(packet: dict[str, Any], beat_role: str) -> bool:
    roles = {_norm(role) for role in packet.get("story_roles", [])}
    acceptable = {
        "hook": {"hook"},
        "context": {"context", "archive", "character_detail"},
        "archive": {"archive", "context", "character_detail"},
        "process": {"process"},
        "performance": {"performance"},
        "emotion": {"emotion", "reaction"},
        "reaction": {"reaction", "emotion"},
        "payoff": {"payoff", "conclusion", "climax"},
    }.get(beat_role, {beat_role})
    return bool(roles & acceptable)


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
        "before_context": _before_context(beat["role"]),
        "after_context": _after_context(beat["role"]),
        "caption_text": packet.get("caption_text"),
        "transition_note": _transition_note(index, beat["role"], packet),
        "continuity_score": packet.get("continuity_compatibility", 0.6),
        "warnings": packet.get("warnings", []),
        "source_evidence": packet.get("source_evidence", {}),
    }


def _ensure_payoff_last(
    project: Project,
    directive: str,
    selected_sequence: list[dict[str, Any]],
    used_segments: set[str],
) -> list[dict[str, Any]]:
    if not selected_sequence:
        return selected_sequence
    last_roles = {_norm(role) for role in selected_sequence[-1].get("story_roles", [])}
    if "payoff" in last_roles:
        return selected_sequence
    payoff_search = context_search(project, f"{directive} payoff ending climax resolution", limit=5)
    for packet in payoff_search["packets"]:
        if packet["segment_id"] not in used_segments:
            selected_sequence[-1] = _sequence_item(
                {
                    "id": selected_sequence[-1]["beat_id"],
                    "role": "payoff",
                    "target_duration_sec": selected_sequence[-1]["duration_sec"],
                },
                packet,
                selected_sequence[-1]["sequence_index"],
                selected_sequence[-1]["duration_sec"],
            )
            break
    return selected_sequence


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
    if selected_sequence:
        last_roles = {_norm(role) for role in selected_sequence[-1].get("story_roles", [])}
        if "payoff" not in last_roles:
            warnings.append("Ending may be weak because the last selected clip is not payoff-tagged.")
    return _dedupe(warnings)


def _store_plan(project: Project, plan: dict[str, Any], source: str) -> None:
    now = utc_now()
    directive_id = f"directive_{uuid.uuid4().hex[:16]}"
    intent_id = f"intent_{uuid.uuid4().hex[:16]}"
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into directives (id, project_id, text, duration_sec, mode, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (directive_id, "default", plan["directive"], plan["duration_target_sec"], "edit_plan", now),
        )
        conn.execute(
            """
            insert into intent_analyses (
                id, project_id, directive_id, directive_text, analysis_json, source, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                "default",
                directive_id,
                plan["directive"],
                json.dumps(plan["intent_analysis"], sort_keys=True),
                source,
                now,
            ),
        )
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
                directive_id,
                intent_id,
                json.dumps(plan, indent=2),
                source,
                now,
            ),
        )


def _why_here(beat: dict[str, Any], packet: dict[str, Any]) -> str:
    reasons = "; ".join(packet.get("why_matches", [])[:2])
    return f"{beat['role']} beat: {reasons or packet['source_evidence'].get('summary', 'selected for the directive')}."


def _before_context(role: str) -> str:
    return {
        "hook": "First impression; no prior setup required.",
        "context": "Follows the hook to add documentary grounding.",
        "archive": "Follows the hook to establish time, source, or history.",
        "process": "Follows context so the creative action has meaning.",
        "performance": "Follows setup or process to raise energy.",
        "emotion": "Follows setup so vulnerability feels earned.",
        "payoff": "Follows escalation and should feel like resolution.",
    }.get(role, "Placed to support the surrounding beats.")


def _after_context(role: str) -> str:
    return {
        "hook": "Should lead into context rather than another similar hook.",
        "context": "Can lead into process, performance, or emotion.",
        "archive": "Can bridge into current studio/performance footage.",
        "process": "Can lead into performance or emotional payoff.",
        "performance": "Can lead into emotion or final payoff.",
        "emotion": "Can lead into payoff if the next clip resolves the feeling.",
        "payoff": "Best used as the ending or final musical release.",
    }.get(role, "Use the next clip to add contrast or escalation.")


def _transition_note(index: int, role: str, packet: dict[str, Any]) -> str:
    if index == 0:
        return "Open cleanly; establish the subject before adding context."
    if "check_audio_transition" in packet.get("warnings", []):
        return "Use a short crossfade or continuous bed to avoid a jarring music cut."
    if role == "payoff":
        return "Let the clip breathe; avoid cutting off the musical phrase."
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


# --- Structured engine (IntentAgent -> StructureAgent -> cast -> compile) ----


def create_structured_plan(
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
    """Directive -> intent -> ad-hoc structure -> cast -> timing.

    Casting here is a BRIDGE: per-slot context_search plus a deterministic
    selector, marked for replacement by the B7 QueryAgent/CastingAgent. The
    structure authoring, expansion, intensity timing, and ordering validation
    are the durable parts.
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
    structure = author_structure(
        project,
        intent,
        duration_sec=duration_sec,
        provider_name=provider_name,
        model=model,
        env_path=env_path,
        store=store,
    )
    casting_warnings: list[str] = []
    slots = expand_structure(structure, duration_sec=duration_sec)
    slots = _expand_generator_slots(project, slots, casting_warnings)
    _assign_slot_targets(slots, duration_sec)
    lane_filters = {lane["id"]: lane["casting_filter"] for lane in structure.get("lanes", [])}
    items = _cast_slots_bridge(project, directive, slots, lane_filters, structure, duration_sec, casting_warnings)
    _assign_timing(items, duration_sec, reserve_ending=bool(structure["ending_policy"].get("reserve_ending", True)))
    plan = {
        "plan_id": f"plan_{uuid.uuid4().hex[:16]}",
        "engine": "structured",
        "directive": directive,
        "duration_target_sec": duration_sec,
        "intent_analysis": intent,
        "structure_id": structure.get("structure_id"),
        "structure": {key: value for key, value in structure.items() if key != "raw_response"},
        "expanded_slots": slots,
        "selected_sequence": items,
        "casting_warnings": casting_warnings,
        "ordering_violations": validate_ordering(structure, items),
        "continuity_warnings": _continuity_warnings(items),
        "sequencing_note": "BRIDGE casting: context_search + deterministic selector; B7 replaces with QueryAgent/CastingAgent",
    }
    if store:
        _store_structured_plan(project, intent, plan, source)
    return plan


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
        packets = context_search(project, generator["from_query"], limit=int(generator["cap"]))["packets"]
        packets = packets[: int(generator["cap"])]
        if generator.get("order") == "chronological":
            packets.sort(key=lambda packet: (packet["file_name"], float(packet["trim_range"][0])))
        if not packets:
            warnings.append(f"slot {slot['slot_id']} uncast: no matches for {generator['from_query']!r}")
            continue
        for index, packet in enumerate(packets):
            concrete = dict(slot)
            concrete["slot_id"] = f"{slot['beat_id']}#{index + 1}"
            concrete["generator"] = None
            concrete["generated_from"] = generator["from_query"]
            concrete["preselected_packet"] = packet
            expanded.append(concrete)
    for position, slot in enumerate(expanded):
        slot["position"] = position
    return expanded


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


def _cast_slots_bridge(
    project: Project,
    directive: str,
    slots: list[dict[str, Any]],
    lane_filters: dict[str, str],
    structure: dict[str, Any],
    duration_sec: float,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """BRIDGE selector (B7 replaces): retrieval-ranked, lane-aware, motif-aware."""
    items: list[dict[str, Any]] = []
    used_segments: set[str] = set()
    motif_registry: dict[str, dict[str, Any]] = {}
    hints = structure.get("transition_policy_hints") or {}

    for slot in slots:
        packets = _slot_candidates(project, directive, slot, lane_filters, warnings)
        if not packets:
            warnings.append(f"slot {slot['slot_id']} uncast: no candidates")
            slot.pop("preselected_packet", None)
            continue
        motif = slot.get("motif")
        if motif and motif["occurrence"] > 1 and motif["slot"] in motif_registry:
            packets = [motif_registry[motif["slot"]]]
            warnings.append(
                f"slot {slot['slot_id']} reuses motif {motif['slot']!r} material (sanctioned recurrence"
                + (f": {motif['transform']}" if motif.get("transform") else "")
                + ")"
            )
        shots = slot["fill"]["shots_min"] if slot.get("fill") else 1
        shots = max(1, min(shots, len(packets)))
        per_shot_target = slot["target_duration_sec"] / shots
        for shot_index in range(shots):
            packet = _pick_packet(packets, used_segments, shot_index, slot, warnings)
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
            item["motif"] = motif
            item["withhold"] = slot.get("withhold") or []
            item["recontextualizes"] = slot.get("recontextualizes")
            item["why_here"] = _structured_why(slot, packet)
            hint = hints.get(slot["function"])
            if hint:
                item["transition_note"] = f"{hint} (structure hint for {slot['function']})"
            items.append(item)
            used_segments.add(packet["segment_id"])
            if motif and motif["occurrence"] == 1 and motif["slot"] not in motif_registry:
                motif_registry[motif["slot"]] = packet
        slot.pop("preselected_packet", None)
    return items


def _slot_candidates(
    project: Project,
    directive: str,
    slot: dict[str, Any],
    lane_filters: dict[str, str],
    warnings: list[str],
) -> list[dict[str, Any]]:
    if slot.get("preselected_packet"):
        return [slot["preselected_packet"]]
    lane_filter = lane_filters.get(slot["lane"] or "", "")
    query = " ".join(
        part
        for part in (directive, lane_filter, slot["casting_filter"], slot["visual_need"], slot["word_need"])
        if part
    )
    packets = context_search(project, query, limit=8)["packets"]
    if slot["lane"] and lane_filter:
        matched = [packet for packet in packets if _lane_matches(packet, lane_filter)]
        if matched:
            packets = matched
        elif packets:
            warnings.append(
                f"slot {slot['slot_id']}: no candidate carries lane evidence for {lane_filter!r}; "
                "casting unfiltered (BRIDGE approximation)"
            )
    return packets


def _lane_matches(packet: dict[str, Any], lane_filter: str) -> bool:
    """Approximate lane check: the filter's value terms must appear in the
    packet's facet evidence (of the named facet type when one is given)."""
    facet_name, _, value = lane_filter.partition(":")
    if not value:
        facet_name, value = "", lane_filter
    facet_name = facet_name.strip().lower()
    terms = [term for term in _norm(value).split("_") if len(term) > 1]
    if not terms:
        return True
    haystack = " ".join(
        f"{facet.get('text') or ''} {facet.get('evidence') or ''}".lower()
        for facet in packet.get("source_evidence", {}).get("facets") or []
        if not facet_name or str(facet.get("observation_type") or "").lower() == facet_name
    )
    return all(term in haystack for term in terms)


def _pick_packet(
    packets: list[dict[str, Any]],
    used_segments: set[str],
    shot_index: int,
    slot: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    fresh = [packet for packet in packets if packet["segment_id"] not in used_segments]
    if fresh:
        return fresh[0]
    packet = packets[shot_index % len(packets)]
    warnings.append(f"slot {slot['slot_id']} reuses segment {packet['segment_id']} (corpus smaller than the structure)")
    return packet


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
