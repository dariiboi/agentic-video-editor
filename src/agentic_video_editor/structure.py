from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from .corpus_profile import corpus_profile, corpus_profile_markdown
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now


STRUCTURE_VERSION = "v1"

# The closed vocabulary the compiler executes. Everything else in a structure
# document (functions, needs, juxtaposition rules) is free text that flows into
# casting prompts and the why trail but never into a code branch.
PATTERNS = {"alternate", "enumerate", "repeat"}
ORDERING_TYPES = {"before", "after", "adjacent", "never_adjacent"}

PROVENANCES = {"user_explicit", "user_implicit", "agent"}

DEFAULT_ENUMERATE_CAP = 12


STRUCTURE_PROMPT_TEMPLATE = """
You are the story architect of an edit suite. Invent the narrative structure
for THIS directive — do not pick a genre template. You compose from a small
closed set of mechanical primitives; everything finer-grained is free text.
STRUCTURE_AGENT
EDIT_TYPE: {edit_type}
MODE: {mode}
TARGET DURATION_SEC: {duration_sec}

The brief (from the intake producer, with decision provenance):
{intent_json}

What the footage index actually holds:
{profile_markdown}

Return JSON only:
{{
  "logline": string,
  "constraints_ack": {{"<name>": {{"value": any, "provenance": string, "why": string}}}},
  "word_spine": [{{"text": string, "start_sec": number, "end_sec": number, "file_name": string}}],
  "lanes": [{{"id": string, "casting_filter": string}}],
  "beats": [
    {{
      "id": string,
      "function": string,
      "lane": string_or_null,
      "pattern": null | "alternate" | "repeat" | "enumerate",
      "lanes": [string],
      "count": integer,
      "from_query": string,
      "order": string,
      "cap": integer,
      "intensity_target": number_or_[from, to],
      "fill": {{"shots": [min, max], "continuity": [facet_name]}},
      "motif": {{"slot": string, "occurrence": integer, "transform": string}},
      "withhold": [string],
      "recontextualizes": string_or_null,
      "overlay": {{"need": string, "audio": "keep_primary"}},
      "visual_need": string,
      "word_need": string,
      "casting_filter": string
    }}
  ],
  "ordering_constraints": [{{"type": "before" | "after" | "adjacent" | "never_adjacent", "a": beat_id, "b": beat_id}}],
  "juxtaposition_rules": [string],
  "transition_policy_hints": {{"<function>": string}},
  "ending_policy": {{"intent": string, "reserve_ending": boolean}}
}}

Rules:
- Invent the structure for this directive from the primitives; combine them
  freely (parallel lanes, alternation, recurrence, withheld reveals, tension
  ramps). Do not reach for a stock arc.
- "function" is a free string you invent; nothing downstream switches on it.
  It exists for the why trail and the rubric.
- Beat count scales with the target duration; there is no fixed clamp.
- intensity_target (0..1) is how the macro shape lands as micro timing: higher
  intensity means shorter shots and a faster cut rate. A [from, to] pair ramps
  across a pattern's expanded slots.
- "fill" requests a multi-shot scene; continuity names the facets its shots
  must share (e.g. setting_context).
- A motif slot recurs: the same material returns at each occurrence, optionally
  transformed. Recurrence is the sanctioned exception to novelty.
- "withhold" names evidence attributes that must not appear before this beat;
  "recontextualizes" marks a beat cast to change the meaning of an earlier one.
- enumerate beats generate one slot per match of from_query (this is how
  supercuts and inventories fall out of the same schema).
- "overlay" requests a cutaway for the beat: b-roll video plays over the
  beat's continuing primary audio (documentary J/L grammar — narration over
  picture). "need" says what the cutaway should show; audio is always
  "keep_primary". Put any lane/attribute requirement for the cutaway into the
  need text itself.
- For word-driven directives, word_spine quotes the carrying lines VERBATIM
  from the Quotable lines section with their exact timestamps; never invent or
  round a timestamp. Omit word_spine when the directive is not word-driven.
- Echo every hard constraint you are honoring in constraints_ack with its
  provenance; never relabel a user_explicit constraint.
- Only cast against evidence the footage index plausibly holds; the brief's
  evidence_attributes say what casting must find.
"""


def author_structure(
    project: Project,
    intent: dict[str, Any],
    *,
    duration_sec: float = 60.0,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    store: bool = True,
) -> dict[str, Any]:
    """Run the StructureAgent over an intent brief and the corpus profile.

    Always keeps the raw provider reply (``raw_response``); a malformed reply
    never escapes as a malformed structure: validation repairs with warnings
    and otherwise falls back to a minimal linear structure derived from the
    intent.
    """
    profile = corpus_profile(project)
    prompt = STRUCTURE_PROMPT_TEMPLATE.format(
        edit_type=str(intent.get("edit_type") or "unspecified_edit"),
        mode=str((intent.get("operation") or {}).get("mode") or "compose"),
        duration_sec=duration_sec,
        intent_json=json.dumps(_intent_brief(intent), indent=2, sort_keys=True),
        profile_markdown=corpus_profile_markdown(profile),
    )
    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    raw = provider.generate_text_json(prompt)
    structure = _validate_structure(raw, intent, duration_sec)
    structure["source"] = f"{provider_name}:structure_agent:{STRUCTURE_VERSION}"
    structure["raw_response"] = raw
    if store:
        _store_structure(project, intent, structure)
    return structure


def _intent_brief(intent: dict[str, Any]) -> dict[str, Any]:
    """The intent fields the StructureAgent needs, without raw_response noise."""
    return {
        key: intent.get(key)
        for key in (
            "directive",
            "operation",
            "edit_type",
            "requirements",
            "hard_constraints",
            "anchors",
            "conflicts",
            "evidence_attributes",
            "success_rubric",
        )
    }


def _validate_structure(raw: Any, intent: dict[str, Any], duration_sec: float) -> dict[str, Any]:
    warnings: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("beats"), list) or not raw.get("beats"):
        return _fallback_structure(intent, duration_sec, "structure reply malformed; using minimal linear structure")

    lanes = _norm_lanes(raw.get("lanes"), warnings)
    lane_ids = {lane["id"] for lane in lanes}
    beats = []
    for index, item in enumerate(raw["beats"]):
        beat = _norm_beat(item, index, lane_ids, warnings)
        if beat:
            beats.append(beat)
    if not beats:
        return _fallback_structure(intent, duration_sec, "no valid beats in structure reply; using minimal linear structure")
    beat_ids = {beat["id"] for beat in beats}
    for beat in beats:
        if beat["recontextualizes"] and beat["recontextualizes"] not in beat_ids:
            warnings.append(
                f"beat {beat['id']} recontextualizes unknown beat {beat['recontextualizes']!r}; reference dropped"
            )
            beat["recontextualizes"] = None

    structure = {
        "logline": str(raw.get("logline") or "").strip() or "(no logline returned)",
        "constraints_ack": _norm_constraints_ack(raw.get("constraints_ack"), intent, warnings),
        "word_spine": _norm_word_spine(raw.get("word_spine"), warnings),
        "lanes": lanes,
        "beats": beats,
        "ordering_constraints": _norm_ordering(raw.get("ordering_constraints"), beat_ids, warnings),
        "juxtaposition_rules": _string_list(raw.get("juxtaposition_rules")),
        "transition_policy_hints": _norm_hints(raw.get("transition_policy_hints")),
        "ending_policy": _norm_ending_policy(raw.get("ending_policy")),
        "fallback": False,
    }
    structure["validation_warnings"] = warnings
    return structure


def _fallback_structure(intent: dict[str, Any], duration_sec: float, warning: str) -> dict[str, Any]:
    """Minimal linear safety net; deliberately plain, never a hidden template."""
    beat_count = max(3, int(round(duration_sec / 10.0)))
    beats = []
    for index in range(beat_count):
        if index == 0:
            function = "open"
        elif index == beat_count - 1:
            function = "close"
        else:
            function = f"develop_{index}"
        fraction = index / max(1, beat_count - 1)
        intensity = round(0.35 + 0.35 * fraction, 3) if index < beat_count - 1 else 0.3
        beats.append(_norm_beat({"id": f"fb{index + 1}", "function": function, "intensity_target": intensity}, index, set(), []))
    return {
        "logline": f"Linear fallback for: {intent.get('directive') or 'the directive'}",
        "constraints_ack": dict(intent.get("hard_constraints") or {}),
        "word_spine": [],
        "lanes": [],
        "beats": beats,
        "ordering_constraints": [],
        "juxtaposition_rules": [],
        "transition_policy_hints": {},
        "ending_policy": {"intent": "", "reserve_ending": True},
        "fallback": True,
        "validation_warnings": [warning],
    }


def _norm_lanes(raw: Any, warnings: list[str]) -> list[dict[str, str]]:
    lanes = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            warnings.append("dropped a lane without an id")
            continue
        lanes.append(
            {
                "id": str(item["id"]).strip(),
                "casting_filter": str(item.get("casting_filter") or "").strip(),
            }
        )
    return lanes


def _norm_beat(raw: Any, index: int, lane_ids: set[str], warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        warnings.append(f"dropped beat #{index + 1}: not an object")
        return None
    beat_id = str(raw.get("id") or f"beat_{index + 1}").strip()
    function = str(raw.get("function") or "").strip()
    if not function:
        warnings.append(f"beat {beat_id} had no function; named 'unnamed'")
        function = "unnamed"

    pattern = raw.get("pattern")
    pattern = str(pattern).strip().lower() if pattern else None
    if pattern and pattern not in PATTERNS:
        warnings.append(f"beat {beat_id} had unknown pattern {pattern!r}; treated as a plain beat")
        pattern = None

    lane = raw.get("lane")
    lane = str(lane).strip() if lane else None
    if lane and lane_ids and lane not in lane_ids:
        warnings.append(f"beat {beat_id} names unknown lane {lane!r}; lane cleared")
        lane = None
    pattern_lanes = [str(item).strip() for item in raw.get("lanes") or [] if str(item).strip()]
    if pattern == "alternate":
        unknown = [item for item in pattern_lanes if lane_ids and item not in lane_ids]
        if unknown:
            warnings.append(f"beat {beat_id} alternates unknown lanes {unknown}; they were dropped")
            pattern_lanes = [item for item in pattern_lanes if item not in unknown]
        if len(pattern_lanes) < 2:
            warnings.append(f"beat {beat_id} alternate pattern needs >=2 lanes; treated as a plain beat")
            pattern = None

    return {
        "id": beat_id,
        "function": function,
        "lane": lane,
        "pattern": pattern,
        "lanes": pattern_lanes,
        "count": _int_or_none(raw.get("count")),
        "from_query": str(raw.get("from_query") or "").strip(),
        "order": str(raw.get("order") or "chronological").strip().lower(),
        "cap": _int_or_none(raw.get("cap")) or DEFAULT_ENUMERATE_CAP,
        "intensity_target": _norm_intensity(raw.get("intensity_target"), beat_id, warnings),
        "fill": _norm_fill(raw.get("fill"), beat_id, warnings),
        "motif": _norm_motif(raw.get("motif"), beat_id, warnings),
        "withhold": _string_list(raw.get("withhold")),
        "recontextualizes": str(raw.get("recontextualizes")).strip() if raw.get("recontextualizes") else None,
        "overlay": _norm_overlay(raw.get("overlay"), beat_id, warnings),
        "visual_need": str(raw.get("visual_need") or "").strip(),
        "word_need": str(raw.get("word_need") or "").strip(),
        "casting_filter": str(raw.get("casting_filter") or "").strip(),
    }


def _norm_intensity(raw: Any, beat_id: str, warnings: list[str]) -> float | list[float] | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return _clamp01(float(raw))
    if isinstance(raw, list) and len(raw) == 2:
        try:
            return [_clamp01(float(raw[0])), _clamp01(float(raw[1]))]
        except (TypeError, ValueError):
            pass
    warnings.append(f"beat {beat_id} had invalid intensity_target {raw!r}; dropped")
    return None


def _norm_fill(raw: Any, beat_id: str, warnings: list[str]) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.append(f"beat {beat_id} had invalid fill {raw!r}; dropped")
        return None
    shots = raw.get("shots")
    if isinstance(shots, list) and len(shots) == 2:
        try:
            low, high = int(shots[0]), int(shots[1])
        except (TypeError, ValueError):
            low, high = 1, 1
    else:
        low = high = _int_or_none(shots) or 1
    low = max(1, low)
    high = max(low, high)
    return {"shots_min": low, "shots_max": high, "continuity": _string_list(raw.get("continuity"))}


def _norm_motif(raw: Any, beat_id: str, warnings: list[str]) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not str(raw.get("slot") or "").strip():
        warnings.append(f"beat {beat_id} had a motif without a slot name; dropped")
        return None
    return {
        "slot": str(raw["slot"]).strip(),
        "occurrence": _int_or_none(raw.get("occurrence")) or 1,
        "transform": str(raw.get("transform") or "").strip() or None,
    }


def _norm_overlay(raw: Any, beat_id: str, warnings: list[str]) -> dict[str, Any] | None:
    """Cutaway request: b-roll video over the beat's continuing primary audio.

    keep_primary is the only audio policy the renderer executes today; an
    unknown policy is kept as a warning rather than silently invented.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict) or not str(raw.get("need") or "").strip():
        warnings.append(f"beat {beat_id} had an overlay without a need; dropped")
        return None
    audio = str(raw.get("audio") or "keep_primary").strip().lower()
    if audio != "keep_primary":
        warnings.append(f"beat {beat_id} overlay audio policy {audio!r} unsupported; using keep_primary")
        audio = "keep_primary"
    return {"need": str(raw["need"]).strip(), "audio": audio}


def _norm_word_spine(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    """Verbatim quoted spans with timestamps; entries missing either are dropped.

    Evidence pass-through, not an executable primitive: casting and the why
    trail consume it, the compiler never branches on it.
    """
    spine = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            warnings.append("dropped a word-spine entry without text")
            continue
        try:
            start, end = float(item["start_sec"]), float(item["end_sec"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"dropped word-spine entry {str(item.get('text'))[:32]!r} without timestamps")
            continue
        entry: dict[str, Any] = {"text": str(item["text"]).strip(), "start_sec": start, "end_sec": end}
        if item.get("file_name"):
            entry["file_name"] = str(item["file_name"]).strip()
        spine.append(entry)
    return spine


def _norm_constraints_ack(raw: Any, intent: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict) or not raw:
        warnings.append("constraints_ack missing; echoed from the intent brief")
        return dict(intent.get("hard_constraints") or {})
    ack: dict[str, Any] = {}
    intent_constraints = intent.get("hard_constraints") or {}
    for name, value in raw.items():
        entry = value if isinstance(value, dict) else {"value": value}
        provenance = str(entry.get("provenance") or "").strip().lower()
        if provenance not in PROVENANCES:
            provenance = "agent"
        original = intent_constraints.get(str(name))
        if isinstance(original, dict) and original.get("provenance") == "user_explicit" and provenance != "user_explicit":
            # provenance discipline: the structure may never relabel the user's constraint
            warnings.append(f"constraints_ack demoted user_explicit constraint {name}; restored")
            provenance = "user_explicit"
        ack[str(name)] = {
            "value": entry.get("value"),
            "provenance": provenance,
            "why": str(entry.get("why") or "").strip() or None,
        }
    return ack


def _norm_ordering(raw: Any, beat_ids: set[str], warnings: list[str]) -> list[dict[str, str]]:
    constraints = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        a, b = str(item.get("a") or "").strip(), str(item.get("b") or "").strip()
        if kind not in ORDERING_TYPES:
            warnings.append(f"dropped ordering constraint with unknown type {kind!r}")
            continue
        if a not in beat_ids or b not in beat_ids:
            warnings.append(f"dropped ordering constraint referencing unknown beats {a!r}/{b!r}")
            continue
        constraints.append({"type": kind, "a": a, "b": b})
    return constraints


def _norm_hints(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if str(value).strip()}


def _norm_ending_policy(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "intent": str(raw.get("intent") or "").strip(),
        # mechanical flag the compiler already knows how to honor
        "reserve_ending": bool(raw.get("reserve_ending", True)),
    }


def _store_structure(project: Project, intent: dict[str, Any], structure: dict[str, Any]) -> None:
    structure_id = f"structure_{uuid.uuid4().hex[:16]}"
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into structures (
                id, project_id, directive_id, intent_analysis_id,
                structure_json, raw_json, source, fallback, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                structure_id,
                "default",
                intent.get("directive_id"),
                intent.get("intent_id"),
                json.dumps({key: value for key, value in structure.items() if key != "raw_response"}, sort_keys=True),
                json.dumps(structure.get("raw_response"), sort_keys=True),
                structure["source"],
                1 if structure.get("fallback") else 0,
                utc_now(),
            ),
        )
        conn.commit()
    structure["structure_id"] = structure_id


# --- Deterministic expansion -------------------------------------------------


def expand_structure(structure: dict[str, Any], *, duration_sec: float) -> list[dict[str, Any]]:
    """Expand a structure document into concrete beat slots.

    Pure function: patterns unroll mechanically, intensity ramps resolve per
    slot, and enumerate beats become generator specs for the casting layer.
    ``duration_sec`` is accepted for future duration-aware expansion but the
    slot targets themselves are assigned by the planner's fitter.
    """
    del duration_sec
    slots: list[dict[str, Any]] = []
    for beat in structure.get("beats", []):
        pattern = beat.get("pattern")
        if pattern == "alternate":
            count = _pattern_count(beat, default=2 * len(beat["lanes"]))
            for position in range(count):
                slots.append(
                    _slot(
                        beat,
                        occurrence=position + 1,
                        total=count,
                        lane=beat["lanes"][position % len(beat["lanes"])],
                    )
                )
        elif pattern == "repeat":
            count = _pattern_count(beat, default=2)
            for position in range(count):
                slots.append(_slot(beat, occurrence=position + 1, total=count, lane=beat.get("lane")))
        elif pattern == "enumerate":
            slot = _slot(beat, occurrence=1, total=1, lane=beat.get("lane"))
            slot["generator"] = {
                "type": "enumerate",
                "from_query": beat.get("from_query") or beat.get("word_need") or beat.get("visual_need") or beat["function"],
                "order": beat.get("order") or "chronological",
                "cap": beat.get("cap") or DEFAULT_ENUMERATE_CAP,
            }
            slots.append(slot)
        else:
            slots.append(_slot(beat, occurrence=1, total=1, lane=beat.get("lane")))
    for position, slot in enumerate(slots):
        slot["position"] = position
    return slots


def _slot(beat: dict[str, Any], *, occurrence: int, total: int, lane: str | None) -> dict[str, Any]:
    suffix = f"#{occurrence}" if total > 1 else ""
    return {
        "slot_id": f"{beat['id']}{suffix}",
        "beat_id": beat["id"],
        "function": beat["function"],
        "lane": lane,
        "intensity": _resolve_intensity(beat.get("intensity_target"), occurrence - 1, total),
        "fill": beat.get("fill"),
        "motif": beat.get("motif"),
        "withhold": list(beat.get("withhold") or []),
        "recontextualizes": beat.get("recontextualizes"),
        "overlay": beat.get("overlay"),
        "visual_need": beat.get("visual_need") or "",
        "word_need": beat.get("word_need") or "",
        "casting_filter": beat.get("casting_filter") or "",
        "generator": None,
    }


def _pattern_count(beat: dict[str, Any], *, default: int) -> int:
    if beat.get("count"):
        return max(1, int(beat["count"]))
    span = re.fullmatch(r"[a-zA-Z_]*(\d+)\s*-\s*[a-zA-Z_]*(\d+)", str(beat.get("id") or ""))
    if span:
        low, high = int(span.group(1)), int(span.group(2))
        if high >= low:
            return high - low + 1
    return max(1, default)


def _resolve_intensity(target: Any, index: int, total: int) -> float | None:
    if target is None:
        return None
    if isinstance(target, (int, float)):
        return _clamp01(float(target))
    start, end = float(target[0]), float(target[1])
    if total <= 1:
        return _clamp01(start)
    return _clamp01(round(start + (end - start) * index / (total - 1), 4))


def intensity_to_weight(intensity: float) -> float:
    """Map a beat's intensity target to a duration weight.

    Linear and monotone decreasing: intensity 0.0 -> weight 1.6 (room to
    breathe), intensity 1.0 -> weight 0.6 (fast cut rate). The agent sets the
    target; this deterministic mapping is how the macro curve lands as timing.
    """
    return round(1.6 - _clamp01(float(intensity)), 4)


def validate_ordering(structure: dict[str, Any], cast: list[dict[str, Any]]) -> list[str]:
    """Mechanical ordering checks over cast items (in timeline order).

    Returns violation strings for repair/reporting; beats that ended up uncast
    are skipped (casting warnings already flag those).
    """
    positions: dict[str, list[int]] = {}
    for index, item in enumerate(cast):
        beat_id = str(item.get("beat_id") or "")
        positions.setdefault(beat_id, []).append(index)
    violations = []
    for constraint in structure.get("ordering_constraints", []):
        kind, a, b = constraint["type"], constraint["a"], constraint["b"]
        pos_a, pos_b = positions.get(a), positions.get(b)
        if not pos_a or not pos_b:
            continue
        if kind == "before" and max(pos_a) >= min(pos_b):
            violations.append(f"ordering violated: {a} must come before {b}")
        elif kind == "after" and min(pos_a) <= max(pos_b):
            violations.append(f"ordering violated: {a} must come after {b}")
        elif kind == "adjacent" and not _any_adjacent(pos_a, pos_b):
            violations.append(f"ordering violated: {a} and {b} must be adjacent")
        elif kind == "never_adjacent" and _any_adjacent(pos_a, pos_b):
            violations.append(f"ordering violated: {a} and {b} must never be adjacent")
    return violations


def _any_adjacent(pos_a: list[int], pos_b: list[int]) -> bool:
    set_b = set(pos_b)
    return any(index + 1 in set_b or index - 1 in set_b for index in pos_a)


def structure_markdown(structure: dict[str, Any]) -> str:
    """Human-readable rendering for the CLI's default output."""
    lines = [f"Logline: {structure['logline']}"]
    if structure.get("fallback"):
        lines.append("(fallback structure: the agent reply was malformed)")
    if structure.get("word_spine"):
        lines.append("")
        lines.append("Word spine:")
        for entry in structure["word_spine"]:
            where = f" [{entry.get('file_name', '?')} {entry['start_sec']:.2f}-{entry['end_sec']:.2f}]"
            lines.append(f'- "{entry["text"]}"{where}')
    if structure.get("lanes"):
        lines.append("")
        lines.append("Lanes:")
        for lane in structure["lanes"]:
            lines.append(f"- {lane['id']}: {lane['casting_filter'] or '(no filter)'}")
    lines.append("")
    lines.append("Beats:")
    for beat in structure["beats"]:
        parts = [beat["function"]]
        if beat.get("pattern"):
            parts.append(f"pattern={beat['pattern']}")
        if beat.get("lane"):
            parts.append(f"lane={beat['lane']}")
        if beat.get("intensity_target") is not None:
            parts.append(f"intensity={beat['intensity_target']}")
        if beat.get("motif"):
            parts.append(f"motif={beat['motif']['slot']}x{beat['motif']['occurrence']}")
        if beat.get("fill"):
            parts.append(f"shots={beat['fill']['shots_min']}-{beat['fill']['shots_max']}")
        if beat.get("overlay"):
            parts.append(f"overlay=\"{beat['overlay']['need']}\"")
        lines.append(f"- {beat['id']}: {', '.join(parts)}")
    if structure.get("ordering_constraints"):
        lines.append("")
        lines.append("Ordering:")
        for constraint in structure["ordering_constraints"]:
            lines.append(f"- {constraint['a']} {constraint['type']} {constraint['b']}")
    ending = structure.get("ending_policy") or {}
    if ending.get("intent"):
        lines.append("")
        lines.append(f"Ending: {ending['intent']} (reserve_ending={ending.get('reserve_ending')})")
    if structure.get("validation_warnings"):
        lines.append("")
        lines.append("Validation warnings:")
        for warning in structure["validation_warnings"]:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
