from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .casting import cast_slot, gather_candidates
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project
from .retrieval import context_search

REVISE_VERSION = "v1"

# The only operations the machinery applies. Anything else the RevisionAgent
# returns is rejected with a warning — a revision must stay a targeted diff.
REVISION_OPS = {"retime", "replace_item", "drop_item", "swap_ending", "insert_beat"}

REGION_TARGETS = {"opening", "middle", "ending", "all"}

REVISE_PROMPT_TEMPLATE = """
You are revising an existing cut. You may ONLY describe targeted changes as an
edit script; you never rebuild the plan. Untouched items must stay exactly as
they are — that is the contract of a revision.
REVISE_AGENT
DIRECTIVE: {directive}

The current cut ({item_count} items, {total_sec}s):
{item_lines}

Structure: {structure_line}

Return JSON only:
{{"operations": [
  {{"op": "retime", "target": "opening" | "middle" | "ending" | "all" | "<slot_id>" | [start_index, end_index],
    "factor": number_or_null, "target_duration_sec": number_or_null, "why": string}},
  {{"op": "replace_item", "item": index_or_slot_id, "with": "recast" | "<segment_id>", "why": string}},
  {{"op": "drop_item", "item": index_or_slot_id, "why": string}},
  {{"op": "swap_ending", "with_query": string, "why": string}},
  {{"op": "insert_beat", "after": index_or_slot_id, "need": string, "why": string}}
]}}

Rules:
- Emit the SMALLEST script that satisfies the directive; every op needs a why.
- If the directive cannot be expressed with these operations, return
  {{"operations": []}} — an honest "not understood" beats an unrequested remake.
"""


class RevisionSourceError(RuntimeError):
    """Raised when a revision has no parent plan to revise."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.payload = {"error": "revision_source_missing", "detail": message}


def plan_revision(
    project: Project,
    intent: dict[str, Any],
    *,
    directive: str,
    plan_id: str | None = None,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    store: bool = True,
    source: str = "structured",
) -> dict[str, Any]:
    """Revise an existing plan with a targeted edit script.

    The RevisionAgent proposes operations from a closed set; code applies them
    mechanically. Untouched items are copied byte-identical from the parent
    (timeline offsets included, unless an upstream duration change forces a
    shift). An empty or invalid script yields status
    ``revision_not_understood`` with the parent untouched — never a re-roll.
    """
    parent = _load_parent_plan(project, intent, plan_id)
    parent_items = parent["plan"].get("selected_sequence") or []
    if not parent_items:
        raise RevisionSourceError(f"parent plan {parent['id']} has no sequence items to revise")

    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    raw = provider.generate_text_json(_revise_prompt(directive, parent["plan"], parent_items))
    warnings: list[str] = []
    script = _validate_script(raw, len(parent_items), warnings)

    entries = [{"item": _copy(item), "changed": False, "change": None} for item in parent_items]
    changes: list[dict[str, Any]] = []
    if script:
        for operation in script:
            applied = _apply_operation(
                project,
                intent,
                provider,
                entries,
                operation,
                warnings,
            )
            if applied:
                changes.append(applied)
        status = "ok" if changes else "revision_not_understood"
    else:
        status = "revision_not_understood"
        warnings.append(
            "revision produced no valid operations; the parent plan is returned untouched"
        )
    _reflow_timeline(entries)

    items = [entry["item"] for entry in entries]
    changed = sum(1 for entry in entries if entry["changed"])
    plan = {
        "plan_id": f"plan_{uuid.uuid4().hex[:16]}",
        "engine": "structured",
        "mode": "transform",
        "status": status,
        "directive": directive,
        "duration_target_sec": round(sum(item["duration_sec"] for item in items), 3),
        "intent_analysis": intent,
        "structure_id": parent["plan"].get("structure_id"),
        "parent_plan_id": parent["id"],
        "edit_script": script,
        "raw_revision_response": raw,
        "revision_diff": {
            "items_parent": len(parent_items),
            "items_revised": len(items),
            "items_changed": changed,
            "items_kept": len(items) - changed,
            "changes": changes,
        },
        "selected_sequence": items,
        "casting_warnings": warnings,
        "sequencing_note": (
            "revision of "
            + parent["id"]
            + ": closed-set edit script applied mechanically; untouched items copied byte-identical"
        ),
    }
    if store:
        from .planner import _store_structured_plan

        _store_structured_plan(project, intent, plan, source)
    return plan


# --- Parent plan lookup -----------------------------------------------------------


def _load_parent_plan(project: Project, intent: dict[str, Any], plan_id: str | None) -> dict[str, Any]:
    sources = str(intent.get("operation", {}).get("sources") or "")
    if not plan_id and sources.startswith("timeline:") and sources != "timeline:latest":
        plan_id = sources.split(":", 1)[1]
    with connect_db(project.db_path) as conn:
        migrate(conn)
        if plan_id:
            row = conn.execute(
                "select id, plan_json from edit_plans where project_id = ? and id = ?",
                ("default", plan_id),
            ).fetchone()
            if not row:
                raise RevisionSourceError(f"no edit plan {plan_id!r} exists to revise")
            return {"id": row["id"], "plan": json.loads(row["plan_json"])}
        rows = conn.execute(
            "select id, plan_json from edit_plans where project_id = ? order by created_at desc, rowid desc",
            ("default",),
        ).fetchall()
    for row in rows:
        plan = json.loads(row["plan_json"])
        if plan.get("status") == "ok" and plan.get("selected_sequence"):
            return {"id": row["id"], "plan": plan}
    raise RevisionSourceError("no completed edit plan exists to revise; run edit-plan first")


# --- RevisionAgent I/O ------------------------------------------------------------


def _revise_prompt(directive: str, parent_plan: dict[str, Any], items: list[dict[str, Any]]) -> str:
    lines = []
    for item in items:
        lines.append(
            f"- [{item['sequence_index']} | {item.get('slot_id') or item.get('beat_id')} | "
            f"{item['segment_id']} | {item['file_name']} | {item['duration_sec']}s | "
            f"{item.get('beat_role') or '-'} | {str(item.get('why_here') or '')[:100]}]"
        )
    structure = parent_plan.get("structure") or {}
    structure_line = str(structure.get("logline") or parent_plan.get("sequencing_note") or "-")[:160]
    return REVISE_PROMPT_TEMPLATE.format(
        directive=" ".join(str(directive).split()),
        item_count=len(items),
        total_sec=round(sum(item["duration_sec"] for item in items), 2),
        item_lines="\n".join(lines),
        structure_line=structure_line,
    )


def _validate_script(raw: Any, item_count: int, warnings: list[str]) -> list[dict[str, Any]]:
    operations = raw.get("operations") if isinstance(raw, dict) else raw
    script: list[dict[str, Any]] = []
    for operation in operations if isinstance(operations, list) else []:
        if not isinstance(operation, dict):
            warnings.append("dropped a non-object edit-script entry")
            continue
        op = str(operation.get("op") or "").strip().lower()
        if op not in REVISION_OPS:
            warnings.append(f"rejected unknown edit-script op {op!r}")
            continue
        normalized = {"op": op, "why": str(operation.get("why") or "").strip() or None}
        if op == "retime":
            target = operation.get("target")
            factor = operation.get("factor")
            target_duration = operation.get("target_duration_sec")
            if not _valid_region_target(target, item_count):
                warnings.append(f"rejected retime with invalid target {target!r}")
                continue
            if not _positive(factor) and not _positive(target_duration):
                warnings.append("rejected retime without a positive factor or target_duration_sec")
                continue
            normalized.update(
                {
                    "target": target,
                    "factor": float(factor) if _positive(factor) else None,
                    "target_duration_sec": float(target_duration) if _positive(target_duration) else None,
                }
            )
        elif op in ("replace_item", "drop_item"):
            item_ref = operation.get("item")
            if not _valid_item_ref(item_ref, item_count):
                warnings.append(f"rejected {op} with invalid item reference {item_ref!r}")
                continue
            normalized["item"] = item_ref
            if op == "replace_item":
                normalized["with"] = str(operation.get("with") or "recast").strip() or "recast"
        elif op == "swap_ending":
            query = str(operation.get("with_query") or "").strip()
            if not query:
                warnings.append("rejected swap_ending without with_query")
                continue
            normalized["with_query"] = query
        elif op == "insert_beat":
            after = operation.get("after")
            need = str(operation.get("need") or "").strip()
            if not _valid_item_ref(after, item_count) or not need:
                warnings.append("rejected insert_beat without a valid position and need")
                continue
            normalized.update({"after": after, "need": need})
        script.append(normalized)
    return script


def _valid_region_target(target: Any, item_count: int) -> bool:
    if isinstance(target, str):
        return target in REGION_TARGETS or bool(target.strip())
    if isinstance(target, list) and len(target) == 2:
        try:
            start, end = int(target[0]), int(target[1])
        except (TypeError, ValueError):
            return False
        return 0 <= start <= end < item_count
    return False


def _valid_item_ref(ref: Any, item_count: int) -> bool:
    if isinstance(ref, bool):
        return False
    if isinstance(ref, int):
        return -item_count <= ref < item_count
    return isinstance(ref, str) and bool(ref.strip())


def _positive(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


# --- Mechanical application -------------------------------------------------------


def _apply_operation(
    project: Project,
    intent: dict[str, Any],
    provider,
    entries: list[dict[str, Any]],
    operation: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any] | None:
    op = operation["op"]
    if op == "retime":
        return _apply_retime(entries, operation, warnings)
    if op == "drop_item":
        index = _resolve_item_ref(entries, operation["item"])
        if index is None:
            warnings.append(f"drop_item: no item matches {operation['item']!r}")
            return None
        dropped = entries.pop(index)
        return _change(operation, dropped["item"], f"dropped item {index} ({dropped['item']['segment_id']})")
    if op == "replace_item":
        index = _resolve_item_ref(entries, operation["item"])
        if index is None:
            warnings.append(f"replace_item: no item matches {operation['item']!r}")
            return None
        return _apply_replace(project, intent, provider, entries, index, operation, warnings)
    if op == "swap_ending":
        return _apply_swap_ending(project, entries, operation, warnings)
    if op == "insert_beat":
        index = _resolve_item_ref(entries, operation["after"])
        if index is None:
            warnings.append(f"insert_beat: no item matches {operation['after']!r}")
            return None
        return _apply_insert(project, entries, index, operation, warnings)
    return None


def _apply_retime(
    entries: list[dict[str, Any]],
    operation: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any] | None:
    indices = _region_indices(entries, operation["target"])
    if not indices:
        warnings.append(f"retime: target {operation['target']!r} selects no items")
        return None
    factor = operation.get("factor")
    if factor is None:
        current = sum(entries[index]["item"]["duration_sec"] for index in indices)
        factor = operation["target_duration_sec"] / current if current > 0 else 1.0
    for index in indices:
        item = entries[index]["item"]
        cap = float(item.get("max_available_sec") or item["duration_sec"])
        duration = min(cap, max(0.5, item["duration_sec"] * factor))
        end = item["source_start_sec"] + duration
        end = _respect_word_units_end(item, end)
        item["duration_sec"] = round(end - item["source_start_sec"], 3)
        item["source_end_sec"] = round(end, 3)
        entries[index]["changed"] = True
    described = ", ".join(str(index) for index in indices)
    return _change(
        operation,
        None,
        f"retimed items [{described}] by factor {round(float(factor), 3)}",
    )


def _apply_replace(
    project: Project,
    intent: dict[str, Any],
    provider,
    entries: list[dict[str, Any]],
    index: int,
    operation: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any] | None:
    old_item = entries[index]["item"]
    plan_segments = {entry["item"]["segment_id"] for entry in entries}
    replacement = str(operation.get("with") or "recast")
    if replacement == "recast":
        slot = _slot_from_item(old_item, operation.get("why"))
        candidates = [
            packet
            for packet in gather_candidates(project, slot, intent, {})
            if packet["segment_id"] != old_item["segment_id"]
        ]
        # Prefer segments the plan does not already use; reuse is a last resort.
        candidates.sort(key=lambda packet: packet["segment_id"] in plan_segments)
        if not candidates:
            warnings.append(f"replace_item: no other candidate exists to recast item {index}")
            return None
        decision = cast_slot(provider, slot, candidates, None)
        warnings.extend(decision["warnings"])
        packet, why = decision["packet"], decision["why"]
    else:
        packet = _packet_for_segment(project, replacement)
        if not packet:
            warnings.append(f"replace_item: segment {replacement!r} not found in the index")
            return None
        why = operation.get("why") or f"user pinned segment {replacement}"
    new_item = _item_from_packet(packet, old_item, why)
    entries[index] = {"item": new_item, "changed": True, "change": None}
    return _change(
        operation,
        new_item,
        f"replaced item {index}: {old_item['segment_id']} -> {new_item['segment_id']}",
    )


def _apply_swap_ending(
    project: Project,
    entries: list[dict[str, Any]],
    operation: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any] | None:
    old_item = entries[-1]["item"]
    packets = [
        packet
        for packet in context_search(project, operation["with_query"], limit=8)["packets"]
        if packet["segment_id"] != old_item["segment_id"]
    ]
    if not packets:
        warnings.append(f"swap_ending: no candidate matches {operation['with_query']!r}")
        return None
    why = operation.get("why") or f"ending swapped for {operation['with_query']!r}"
    new_item = _item_from_packet(packets[0], old_item, why)
    entries[-1] = {"item": new_item, "changed": True, "change": None}
    return _change(
        operation,
        new_item,
        f"swapped ending: {old_item['segment_id']} -> {new_item['segment_id']}",
    )


def _apply_insert(
    project: Project,
    entries: list[dict[str, Any]],
    index: int,
    operation: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any] | None:
    used = {entry["item"]["segment_id"] for entry in entries}
    packets = [
        packet
        for packet in context_search(project, operation["need"], limit=8)["packets"]
        if packet["segment_id"] not in used
    ]
    if not packets:
        warnings.append(f"insert_beat: no unused candidate matches {operation['need']!r}")
        return None
    template = entries[index]["item"]
    why = operation.get("why") or f"inserted for need {operation['need']!r}"
    new_item = _item_from_packet(packets[0], template, why)
    new_item["slot_id"] = f"inserted_{uuid.uuid4().hex[:6]}"
    new_item["beat_id"] = new_item["slot_id"]
    new_item["beat_role"] = "inserted"
    entries.insert(index + 1, {"item": new_item, "changed": True, "change": None})
    return _change(operation, new_item, f"inserted {new_item['segment_id']} after item {index}")


def _region_indices(entries: list[dict[str, Any]], target: Any) -> list[int]:
    count = len(entries)
    if isinstance(target, list):
        start, end = int(target[0]), int(target[1])
        return list(range(max(0, start), min(count, end + 1)))
    if target == "all":
        return list(range(count))
    if target in ("opening", "middle", "ending"):
        third = max(1, count // 3)
        if target == "opening":
            return list(range(0, third))
        if target == "ending":
            return list(range(count - third, count))
        middle = list(range(third, count - third))
        return middle or [count // 2]
    return [
        index
        for index, entry in enumerate(entries)
        if target in (entry["item"].get("slot_id"), entry["item"].get("beat_id"))
    ]


def _resolve_item_ref(entries: list[dict[str, Any]], ref: Any) -> int | None:
    if isinstance(ref, int):
        index = ref if ref >= 0 else len(entries) + ref
        return index if 0 <= index < len(entries) else None
    for index, entry in enumerate(entries):
        item = entry["item"]
        if ref in (item.get("slot_id"), item.get("beat_id"), item.get("segment_id")):
            return index
    return None


def _slot_from_item(item: dict[str, Any], why: str | None) -> dict[str, Any]:
    return {
        "slot_id": item.get("slot_id") or item.get("beat_id"),
        "function": item.get("beat_role") or "revised",
        "lane": item.get("lane"),
        "intensity": item.get("intensity"),
        "visual_need": why or str(item.get("why_here") or "")[:120],
        "word_need": None,
        "casting_filter": None,
        "withhold": item.get("withhold") or [],
        "motif": None,
    }


def _packet_for_segment(project: Project, segment_id: str) -> dict[str, Any] | None:
    for packet in context_search(project, "", limit=500)["packets"]:
        if packet["segment_id"] == segment_id:
            return packet
    return None


def _item_from_packet(packet: dict[str, Any], old_item: dict[str, Any], why: str) -> dict[str, Any]:
    """Build a replacement item through the planner's item constructor, keeping
    the old item's slot identity and timing budget so the diff stays local."""
    from .planner import _sequence_item

    beat = {
        "id": old_item.get("beat_id") or old_item.get("slot_id") or "revised",
        "role": old_item.get("beat_role") or "revised",
        "target_duration_sec": old_item["duration_sec"],
        "max_duration_sec": old_item.get("max_duration_sec") or old_item["duration_sec"] * 1.5,
        "pacing": old_item.get("pacing"),
    }
    item = _sequence_item(beat, packet, int(old_item.get("sequence_index") or 0), old_item["duration_sec"])
    for key in ("slot_id", "lane", "intensity", "motif", "withhold", "recontextualizes", "anchor"):
        if key in old_item:
            item[key] = old_item[key]
    item["casting_why"] = why
    item["why_here"] = f"{beat['role']} beat (revised): {why}"
    return item


def _respect_word_units_end(item: dict[str, Any], end: float) -> float:
    from .planner import _respect_word_units

    return _respect_word_units(item, item["source_start_sec"], end)


def _reflow_timeline(entries: list[dict[str, Any]]) -> None:
    """Recompute timeline offsets; rewrite them only where they actually moved,
    so an untouched prefix stays byte-identical to the parent plan."""
    cursor = 0.0
    for index, entry in enumerate(entries):
        item = entry["item"]
        start = round(cursor, 3)
        end = round(cursor + item["duration_sec"], 3)
        if item.get("timeline_start_sec") != start or item.get("timeline_end_sec") != end:
            item["timeline_start_sec"] = start
            item["timeline_end_sec"] = end
        if item.get("sequence_index") != index:
            item["sequence_index"] = index
        cursor += item["duration_sec"]


def _change(operation: dict[str, Any], item: dict[str, Any] | None, description: str) -> dict[str, Any]:
    return {
        "op": operation["op"],
        "why": operation.get("why"),
        "description": description,
        "segment_id": item.get("segment_id") if item else None,
    }


def _copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))
