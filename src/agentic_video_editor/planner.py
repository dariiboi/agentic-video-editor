from __future__ import annotations

import json
import uuid
from typing import Any

from .db import connect_db, migrate
from .project import Project, utc_now
from .retrieval import analyze_directive_intent, context_search


DEFAULT_BEATS = [
    ("hook", "Open with an immediate subject/performance signal."),
    ("context", "Give the viewer documentary context before stacking performances."),
    ("process", "Show creative work, studio energy, or progression."),
    ("emotion", "Deepen the cut with vulnerability, reaction, or expressive vocals."),
    ("payoff", "End on the strongest musical or emotional resolution."),
]


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
            prefer_new_asset=index < 3,
        )
        if selected:
            used_segments.add(selected["segment_id"])
            used_assets.add(selected["asset_id"])
            selected_sequence.append(_sequence_item(beat, selected, len(selected_sequence), duration_sec))

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
    clip_target = max(2.0, duration_sec / len(roles))
    beats = []
    for index, role in enumerate(roles):
        description = dict(DEFAULT_BEATS).get(role, f"Use {role} material to serve the directive.")
        beats.append(
            {
                "id": f"beat_{index + 1}",
                "position": index + 1,
                "role": role,
                "description": description,
                "target_duration_sec": round(clip_target, 3),
                "retrieval_hint": _retrieval_hint(role),
            }
        )
    return beats


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
) -> dict[str, Any] | None:
    fresh = [item for item in candidates if item["segment_id"] not in used_segments]
    role_matched = [item for item in fresh if _role_matches_beat(item, beat_role)]
    if role_matched:
        fresh = role_matched
    if beat_role != "payoff":
        not_ending = [item for item in fresh if "save_for_ending" not in item.get("warnings", [])]
        if not_ending:
            fresh = not_ending
    if prefer_new_asset:
        new_asset = [item for item in fresh if item["asset_id"] not in used_assets]
        if new_asset:
            return new_asset[0]
    return fresh[0] if fresh else (candidates[0] if candidates else None)


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
    trim_start, trim_end = packet["trim_range"]
    raw_duration = max(0.1, float(trim_end) - float(trim_start))
    clip_duration = min(raw_duration, max(2.0, beat["target_duration_sec"]), max(2.0, duration_sec))
    return {
        "sequence_index": index,
        "beat_id": beat["id"],
        "beat_role": beat["role"],
        "segment_id": packet["segment_id"],
        "select_id": packet.get("select_id"),
        "asset_id": packet["asset_id"],
        "file_name": packet["file_name"],
        "source_start_sec": round(float(trim_start), 3),
        "source_end_sec": round(float(trim_start) + clip_duration, 3),
        "duration_sec": round(clip_duration, 3),
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


def _assign_timing(selected_sequence: list[dict[str, Any]], duration_sec: float) -> None:
    cursor = 0.0
    remaining_items = len(selected_sequence)
    for item in selected_sequence:
        remaining = max(0.1, duration_sec - cursor)
        max_for_item = remaining / max(1, remaining_items)
        clip_duration = min(item["duration_sec"], max(0.1, remaining), max(2.0, max_for_item))
        item["duration_sec"] = round(clip_duration, 3)
        item["source_end_sec"] = round(item["source_start_sec"] + clip_duration, 3)
        item["timeline_start_sec"] = round(cursor, 3)
        item["timeline_end_sec"] = round(cursor + clip_duration, 3)
        cursor += clip_duration
        remaining_items -= 1


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


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
