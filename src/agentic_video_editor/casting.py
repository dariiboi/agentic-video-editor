from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .facets import FACET_SPECS, facet_search
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project
from .retrieval import context_search


CASTING_VERSION = "v1"

SHOT_SIZE_ORDER = ["extreme_close_up", "close_up", "medium", "wide", "extreme_wide"]

# Coarse energy reading of audio_character trend words; anything unlisted is
# honestly unknown (None), never guessed.
ENERGY_TRENDS = {
    "rising": 1.0,
    "building": 1.0,
    "high": 1.0,
    "steady": 0.5,
    "flat": 0.5,
    "mixed": 0.5,
    "falling": 0.0,
    "fading": 0.0,
    "low": 0.0,
}

DIRECTION_WORDS = {"left", "right", "up", "down", "forward", "backward", "back"}

STOP_TERMS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of",
    "on", "or", "per", "the", "to", "with", "shot", "shots",
}

CASTING_PROMPT_TEMPLATE = """
You are the casting director of an edit suite. Fill ONE beat slot from the
candidate segments below. Answer with segment IDs from the list only; never
invent IDs or timestamps.
CASTING_AGENT
SLOT: {slot_id} | function={function} | lane={lane} | intensity={intensity}
NEEDS: visual={visual_need} | word={word_need} | filter={casting_filter}
WITHHOLD (must not appear yet): {withhold}
MOTIF: {motif}
PREVIOUS ITEM: {previous_line}

CANDIDATES (pre-filtered and ranked; pair features are measured against the
previous item, null means no evidence either way):
{candidate_lines}

Return JSON only:
{{"selected": "<segment_id>", "alternates": ["<segment_id>", ...], "why": string, "risks": [string]}}

Rules:
- selected must serve the slot's function and intensity; explain in why.
- alternates are your next-best picks in order.
- risks name real concerns (reuse, weak evidence, jarring juxtaposition).
"""


class AnchorResolutionError(RuntimeError):
    """A user_explicit anchor found no indexed evidence; the plan fails loudly."""

    def __init__(self, failures: list[dict[str, Any]]) -> None:
        descriptions = ", ".join(repr(item.get("description")) for item in failures)
        super().__init__(f"unresolvable user_explicit anchor(s): {descriptions}")
        self.failures = failures


# --- Query derivation (deterministic QueryAgent) ------------------------------


def parse_casting_filter(filter_text: str) -> dict[str, Any]:
    """Split "people_appearance: green t-shirt" into facet type + value terms.

    A prefix that is not a known facet name is treated as part of the value so
    free-text filters still work.
    """
    facet, _, value = str(filter_text or "").partition(":")
    facet = facet.strip().lower().replace("-", "_").replace(" ", "_")
    if not value or facet not in FACET_SPECS:
        facet, value = "", filter_text
    return {"facet": facet or None, "terms": _terms(value)}


def build_beat_queries(
    slot: dict[str, Any],
    intent: dict[str, Any],
    lane_filters: dict[str, str],
) -> dict[str, Any]:
    """Turn one slot's needs into the concrete sub-queries casting runs."""
    lane_filter = lane_filters.get(slot.get("lane") or "", "")
    parts = [
        str(intent.get("directive") or ""),
        lane_filter,
        slot.get("casting_filter") or "",
        slot.get("visual_need") or "",
        slot.get("word_need") or "",
    ]
    facet_lookups = []
    for filter_text in (lane_filter, slot.get("casting_filter") or ""):
        if not filter_text:
            continue
        parsed = parse_casting_filter(filter_text)
        if parsed["terms"]:
            facet_lookups.append({**parsed, "query": " ".join(parsed["terms"])})
    return {
        "context_query": " ".join(part for part in parts if part),
        "facet_lookups": facet_lookups,
        "qmd_query": (slot.get("visual_need") or slot.get("word_need") or slot.get("function") or "")[:300],
    }


def gather_candidates(
    project: Project,
    slot: dict[str, Any],
    intent: dict[str, Any],
    lane_filters: dict[str, str],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Merge context, facet, and qmd hits into one ranked candidate list.

    Lane and withhold filters are enforced HERE, in code — the CastingAgent
    only ever sees candidates that are allowed to fill the slot.
    """
    queries = build_beat_queries(slot, intent, lane_filters)
    pool = context_search(project, queries["context_query"], limit=max(24, limit * 3))["packets"]
    for packet in pool:
        packet["matched_via"] = ["context_search"]

    by_key = {(packet["asset_id"], tuple(packet["time_range"])): packet for packet in pool}
    for lookup in queries["facet_lookups"]:
        for hit in facet_search(project, lookup["query"], limit=24):
            for (asset_id, time_range), packet in by_key.items():
                if asset_id != hit["asset_id"]:
                    continue
                start, end = float(hit["start_sec"] or 0), float(hit["end_sec"] or 0)
                if start < float(time_range[1]) and float(time_range[0]) < end:
                    via = f"facet:{hit['observation_type']}"
                    if via not in packet["matched_via"]:
                        packet["matched_via"].append(via)

    for segment_id in _qmd_segment_ids(queries["qmd_query"]):
        for packet in pool:
            if packet["segment_id"] == segment_id and "qmd" not in packet["matched_via"]:
                packet["matched_via"].append("qmd")

    candidates = enforce_slot_filters(pool, slot, lane_filters)
    candidates.sort(key=lambda packet: _candidate_rank(packet, slot), reverse=True)
    return candidates[: max(limit, 1)]


def enforce_slot_filters(
    packets: list[dict[str, Any]],
    slot: dict[str, Any],
    lane_filters: dict[str, str],
) -> list[dict[str, Any]]:
    """Deterministic lane + withhold enforcement (never delegated to the LLM)."""
    lane_filter = lane_filters.get(slot.get("lane") or "", "")
    kept = []
    for packet in packets:
        if lane_filter and not evidence_matches(packet, lane_filter):
            continue
        if any(_exhibits(packet, attribute) for attribute in slot.get("withhold") or []):
            continue
        kept.append(packet)
    return kept


def evidence_matches(packet: dict[str, Any], filter_text: str) -> bool:
    """All value terms of the filter appear in the packet's facet evidence,
    scoped to the named facet type when one is given."""
    parsed = parse_casting_filter(filter_text)
    if not parsed["terms"]:
        return True
    tokens: set[str] = set()
    for facet in packet.get("source_evidence", {}).get("facets") or []:
        if parsed["facet"] and str(facet.get("observation_type") or "").lower() != parsed["facet"]:
            continue
        tokens |= _terms(f"{facet.get('text') or ''} {facet.get('evidence') or ''}", keep_stop=True)
    return all(term in tokens for term in parsed["terms"])


def _exhibits(packet: dict[str, Any], attribute: str) -> bool:
    """Whether the packet's evidence shows an attribute (for withhold checks)."""
    terms = _terms(attribute)
    return bool(terms) and terms <= _packet_tokens(packet)


def _candidate_rank(packet: dict[str, Any], slot: dict[str, Any]) -> float:
    score = float(packet.get("score") or 0)
    score += 2.0 * sum(1 for via in packet.get("matched_via", []) if via.startswith("facet:"))
    if "qmd" in packet.get("matched_via", []):
        score += 1.0
    intensity = slot.get("intensity")
    energy = packet_energy(packet)
    if intensity is not None and energy is not None:
        score += 1.5 * (1.0 - abs(energy - float(intensity)))
    return score


def packet_energy(packet: dict[str, Any]) -> float | None:
    """Facet-derived energy reading (audio_character trend), or None."""
    for facet in packet.get("source_evidence", {}).get("facets") or []:
        if facet.get("observation_type") != "audio_character":
            continue
        value = facet.get("value") if isinstance(facet.get("value"), dict) else {}
        trend = str(value.get("energy") or "").strip().lower()
        if trend in ENERGY_TRENDS:
            return ENERGY_TRENDS[trend]
    return None


def _qmd_segment_ids(query: str, *, top_k: int = 8) -> list[str]:
    """Optional qmd hybrid search; degrades to nothing when qmd is unavailable."""
    if not query.strip():
        return []
    try:
        from .qmd_bridge import _qmd_runner

        stdout = _qmd_runner(["vsearch", query, "-n", str(top_k), "--json"])
        results = json.loads(stdout)
    except Exception:
        return []
    ids = []
    for result in results if isinstance(results, list) else []:
        match = re.search(r"qmd://[^/]+/(.+)\.md$", str(result.get("file") or ""))
        if match:
            ids.append(match.group(1).replace("-", "_"))
    return ids


# --- Coverage ------------------------------------------------------------------


def coverage_report(project: Project, intent: dict[str, Any]) -> dict[str, Any]:
    """Count real candidates per directive-critical evidence attribute.

    Zero coverage is surfaced instead of noise-cast: the plan says which
    attributes the index simply does not hold.
    """
    attributes = intent.get("evidence_attributes") or []
    if not attributes:
        return {"attributes": {}, "zero_coverage": []}
    pool = context_search(project, str(intent.get("directive") or ""), limit=200)["packets"]
    report: dict[str, int] = {}
    for attribute in attributes:
        terms = {term for term in _terms(attribute) if len(term) > 2}
        count = sum(1 for packet in pool if terms & _packet_tokens(packet))
        count = max(count, len(facet_search(project, " ".join(terms) or attribute, limit=50)))
        report[str(attribute)] = count
    return {
        "attributes": report,
        "zero_coverage": [attribute for attribute, count in report.items() if count == 0],
    }


# --- Anchors ---------------------------------------------------------------------


def resolve_anchors(project: Project, intent: dict[str, Any]) -> dict[str, Any]:
    """Map pinned content descriptions to segments via the same search stack."""
    resolved, failures, unresolved_soft = [], [], []
    for anchor in intent.get("anchors") or []:
        description = str(anchor.get("description") or "")
        terms = {term for term in _terms(description) if len(term) > 2}
        best = None
        for packet in context_search(project, description, limit=8)["packets"]:
            hits = terms & _packet_tokens(packet)
            if hits:
                best = (packet, sorted(hits))
                break
        if best:
            packet, hits = best
            resolved.append(
                {
                    **anchor,
                    "segment_id": packet["segment_id"],
                    "packet": packet,
                    "why": f"evidence matches anchor terms: {', '.join(hits)}",
                }
            )
        elif anchor.get("provenance") == "user_explicit":
            failures.append({**anchor, "why": "no indexed evidence matches this pinned content"})
        else:
            unresolved_soft.append({**anchor, "why": "no evidence match; non-explicit anchor skipped"})
    return {"resolved": resolved, "failures": failures, "unresolved_soft": unresolved_soft}


# --- Juxtaposition ---------------------------------------------------------------


def pair_features(
    packet_a: dict[str, Any],
    packet_b: dict[str, Any],
    *,
    lane_a: str | None = None,
    lane_b: str | None = None,
) -> dict[str, Any]:
    """Measured adjacency features; null where evidence is missing, never guessed."""
    cin_a, cin_b = _first_facet_value(packet_a, "cinematography"), _first_facet_value(packet_b, "cinematography")
    shot_delta = None
    if cin_a and cin_b:
        size_a, size_b = cin_a.get("shot_size"), cin_b.get("shot_size")
        if size_a in SHOT_SIZE_ORDER and size_b in SHOT_SIZE_ORDER:
            shot_delta = SHOT_SIZE_ORDER.index(size_b) - SHOT_SIZE_ORDER.index(size_a)

    motion_match = None
    if cin_a and cin_b:
        dirs_a = DIRECTION_WORDS & _terms(str(cin_a.get("camera_motion") or ""), keep_stop=True)
        dirs_b = DIRECTION_WORDS & _terms(str(cin_b.get("camera_motion") or ""), keep_stop=True)
        if dirs_a and dirs_b:
            motion_match = bool(dirs_a & dirs_b)

    energy_a, energy_b = packet_energy(packet_a), packet_energy(packet_b)
    energy_delta = round(energy_b - energy_a, 3) if energy_a is not None and energy_b is not None else None

    set_a, set_b = _first_facet_value(packet_a, "setting_context"), _first_facet_value(packet_b, "setting_context")
    setting_match = None
    if set_a and set_b and set_a.get("location_type") and set_b.get("location_type"):
        setting_match = str(set_a["location_type"]).casefold() == str(set_b["location_type"]).casefold()

    if lane_a is None and lane_b is None:
        lane_relation = "none"
    elif lane_a == lane_b:
        lane_relation = "same"
    else:
        lane_relation = "other"

    return {
        "shot_size_delta": shot_delta,
        "motion_direction_match": motion_match,
        "energy_delta": energy_delta,
        "setting_match": setting_match,
        "lane_relation": lane_relation,
    }


def continuity_compatible(packet_a: dict[str, Any], packet_b: dict[str, Any], keys: list[str]) -> bool:
    """Shots of one scene must share evidence for every continuity facet key."""
    for key in keys or []:
        tokens_a = _facet_tokens(packet_a, key)
        tokens_b = _facet_tokens(packet_b, key)
        if not tokens_a or not tokens_b or not (tokens_a & tokens_b):
            return False
    return True


def _first_facet_value(packet: dict[str, Any], facet_type: str) -> dict[str, Any] | None:
    for facet in packet.get("source_evidence", {}).get("facets") or []:
        if facet.get("observation_type") == facet_type and isinstance(facet.get("value"), dict):
            return facet["value"]
    return None


def _facet_tokens(packet: dict[str, Any], facet_type: str) -> set[str]:
    tokens: set[str] = set()
    for facet in packet.get("source_evidence", {}).get("facets") or []:
        if facet.get("observation_type") == facet_type:
            tokens |= _terms(str(facet.get("text") or ""))
    return tokens


# --- CastingAgent ------------------------------------------------------------------


def cast_slot(
    provider,
    slot: dict[str, Any],
    candidates: list[dict[str, Any]],
    previous: dict[str, Any] | None,
) -> dict[str, Any]:
    """One LLM casting decision, validated in code.

    The agent answers in IDs only; a hallucinated or malformed reply falls
    back deterministically to the top-ranked candidate with a warning.
    """
    warnings: list[str] = []
    known = {packet["segment_id"]: packet for packet in candidates}
    prompt = CASTING_PROMPT_TEMPLATE.format(
        slot_id=slot.get("slot_id"),
        function=slot.get("function"),
        lane=slot.get("lane"),
        intensity=slot.get("intensity"),
        visual_need=slot.get("visual_need") or "-",
        word_need=slot.get("word_need") or "-",
        casting_filter=slot.get("casting_filter") or "-",
        withhold=", ".join(slot.get("withhold") or []) or "-",
        motif=json.dumps(slot.get("motif")) if slot.get("motif") else "-",
        previous_line=_candidate_line(previous, None) if previous else "(sequence start)",
        candidate_lines="\n".join(
            _candidate_line(packet, pair_features(previous, packet) if previous else None)
            for packet in candidates
        ),
    )
    reply = provider.generate_text_json(prompt)
    reply = reply if isinstance(reply, dict) else {}
    selected_id = str(reply.get("selected") or "")
    if selected_id not in known:
        if selected_id:
            warnings.append(
                f"slot {slot.get('slot_id')}: casting reply named unknown segment {selected_id!r}; "
                "fell back to the top-ranked candidate"
            )
        else:
            warnings.append(f"slot {slot.get('slot_id')}: casting reply malformed; fell back to the top-ranked candidate")
        selected_id = candidates[0]["segment_id"]
    return {
        "packet": known[selected_id],
        "alternates": [alt for alt in _string_list(reply.get("alternates")) if alt in known and alt != selected_id],
        "why": str(reply.get("why") or "").strip() or "top-ranked candidate for the slot",
        "risks": _string_list(reply.get("risks")),
        "warnings": warnings,
    }


def _candidate_line(packet: dict[str, Any] | None, features: dict[str, Any] | None) -> str:
    if not packet:
        return "(none)"
    trim_start, trim_end = packet["trim_range"]
    evidence = packet.get("source_evidence", {})
    facet_bits = "; ".join(
        f"{facet['observation_type']}: {facet.get('text') or facet.get('evidence') or ''}"
        for facet in (evidence.get("facets") or [])[:4]
    )
    summary = str(evidence.get("summary") or "")[:120]
    line = f"- [{packet['segment_id']} | {float(trim_start):.2f}-{float(trim_end):.2f} | {summary}"
    if facet_bits:
        line += f" | {facet_bits}"
    if features:
        line += f" | pair_vs_prev: {json.dumps(features)}"
    return line + "]"


# --- Full-structure casting ---------------------------------------------------------


def cast_structure(
    project: Project,
    intent: dict[str, Any],
    structure: dict[str, Any],
    slots: list[dict[str, Any]],
    *,
    anchors_resolved: list[dict[str, Any]],
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    warnings: list[str],
) -> dict[str, Any]:
    """Cast every expanded slot: gather -> enforce -> rank -> LLM-select.

    Returns per-shot decisions for the planner to turn into sequence items,
    plus the coverage report and the sanctioned motif reuses.
    """
    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    lane_filters = {lane["id"]: lane["casting_filter"] for lane in structure.get("lanes", [])}
    _pin_anchor_slots(slots, anchors_resolved, warnings)

    decisions: list[dict[str, Any]] = []
    overlay_decisions: list[dict[str, Any]] = []
    sanctioned_reuse: list[dict[str, Any]] = []
    motif_registry: dict[str, dict[str, Any]] = {}
    used_segments: set[str] = set()
    used_assets: set[str] = set()
    previous_packet: dict[str, Any] | None = None
    previous_lane: str | None = None

    for slot in slots:
        candidates = _slot_candidate_pool(project, slot, intent, lane_filters, motif_registry, sanctioned_reuse, warnings)
        if not candidates:
            warnings.append(f"slot {slot['slot_id']} uncast: no candidates satisfy its filters")
            slot.pop("preselected_packet", None)
            continue

        fill = slot.get("fill") or {}
        shots_min = int(fill.get("shots_min") or 1)
        shots_max = int(fill.get("shots_max") or shots_min)
        continuity_keys = fill.get("continuity") or []

        shot_packets: list[dict[str, Any]] = []
        remaining = list(candidates)
        while len(shot_packets) < shots_max and remaining:
            ranked = _novelty_ranked(remaining, used_segments, used_assets, exempt=bool(slot.get("motif")))
            decision = cast_slot(provider, slot, ranked, previous_packet)
            warnings.extend(decision["warnings"])
            packet = decision["packet"]
            features = pair_features(previous_packet, packet, lane_a=previous_lane, lane_b=slot.get("lane")) if previous_packet else None
            decisions.append(
                {
                    "slot": slot,
                    "packet": packet,
                    "shot_index": len(shot_packets),
                    "why": decision["why"],
                    "risks": decision["risks"],
                    "alternates": decision["alternates"],
                    "matched_via": packet.get("matched_via", []),
                    "pair_features_prev": features,
                }
            )
            if packet["segment_id"] in used_segments and not slot.get("motif"):
                warnings.append(
                    f"slot {slot['slot_id']} reuses segment {packet['segment_id']} (corpus smaller than the structure)"
                )
            used_segments.add(packet["segment_id"])
            used_assets.add(packet["asset_id"])
            _register_motif(slot, packet, motif_registry)
            previous_packet, previous_lane = packet, slot.get("lane")
            shot_packets.append(packet)

            if len(shot_packets) >= shots_max:
                break
            remaining = [
                item
                for item in remaining
                if item["segment_id"] not in {shot["segment_id"] for shot in shot_packets}
                and continuity_compatible(shot_packets[0], item, continuity_keys)
            ]
            if not remaining and len(shot_packets) < shots_min:
                warnings.append(
                    f"slot {slot['slot_id']} wanted >={shots_min} continuity-compatible shots; only "
                    f"{len(shot_packets)} exist — falling back"
                )
                break
            if len(shot_packets) >= shots_min and not remaining:
                break
        slot.pop("preselected_packet", None)

        if slot.get("overlay") and shot_packets:
            overlay_decision = _cast_overlay(
                project, provider, slot, shot_packets[0], intent, lane_filters, warnings
            )
            if overlay_decision:
                overlay_decisions.append(overlay_decision)
                used_segments.add(overlay_decision["packet"]["segment_id"])
                used_assets.add(overlay_decision["packet"]["asset_id"])
                # previous_packet stays the primary: the overlay does not change
                # the join context — its audio is the primary's, continuing.

    return {
        "decisions": decisions,
        "overlay_decisions": overlay_decisions,
        "coverage_report": coverage_report(project, intent),
        "sanctioned_reuse": sanctioned_reuse,
    }


def _cast_overlay(
    project: Project,
    provider,
    slot: dict[str, Any],
    primary_packet: dict[str, Any],
    intent: dict[str, Any],
    lane_filters: dict[str, str],
    warnings: list[str],
) -> dict[str, Any] | None:
    """Cast one cutaway for a slot that declared an overlay.

    The overlay's own need text governs its casting (no lane filter — a lane
    requirement belongs in the need text); the slot's withhold filters still
    apply so staged reveals cannot leak in through b-roll. Candidates are
    preferred voiceless-first (their audio is discarded), then by visual
    evidence; the primary's segment is never its own cutaway.
    """
    overlay = slot["overlay"]
    overlay_slot = {
        **slot,
        "slot_id": f"{slot['slot_id']}/overlay",
        "function": f"{slot['function']}_cutaway",
        "lane": None,
        "visual_need": overlay["need"],
        "word_need": "",
        "casting_filter": "",
        "motif": None,
        "fill": None,
        "generator": None,
    }
    pool = [
        packet
        for packet in gather_candidates(project, overlay_slot, intent, lane_filters)
        if packet["segment_id"] != primary_packet["segment_id"]
    ]
    voiceless = [packet for packet in pool if "abrupt_audio_or_no_audio" in (packet.get("warnings") or [])]
    visual = [packet for packet in pool if packet.get("source_evidence", {}).get("visual_affordance")]
    pool = voiceless or visual or pool
    if not pool:
        warnings.append(f"slot {slot['slot_id']} overlay dropped: no cutaway candidates for {overlay['need']!r}")
        return None
    decision = cast_slot(provider, overlay_slot, pool, primary_packet)
    warnings.extend(decision["warnings"])
    return {
        "slot": slot,
        "packet": decision["packet"],
        "overlay_of": slot["slot_id"],
        "need": overlay["need"],
        "audio": overlay["audio"],
        "why": decision["why"],
        "risks": decision["risks"],
        "matched_via": decision["packet"].get("matched_via", []),
    }


def _slot_candidate_pool(
    project: Project,
    slot: dict[str, Any],
    intent: dict[str, Any],
    lane_filters: dict[str, str],
    motif_registry: dict[str, dict[str, Any]],
    sanctioned_reuse: list[dict[str, Any]],
    warnings: list[str],
) -> list[dict[str, Any]]:
    motif = slot.get("motif")
    if motif and motif["occurrence"] > 1 and motif["slot"] in motif_registry:
        packet = motif_registry[motif["slot"]]
        record = {
            "slot_id": slot["slot_id"],
            "motif": motif["slot"],
            "segment_id": packet["segment_id"],
            "transform": motif.get("transform"),
        }
        sanctioned_reuse.append(record)
        warnings.append(
            f"slot {slot['slot_id']} reuses motif {motif['slot']!r} material (sanctioned recurrence"
            + (f": {motif['transform']}" if motif.get("transform") else "")
            + ")"
        )
        return [packet]
    if slot.get("preselected_packet"):
        packet = slot["preselected_packet"]
        packet.setdefault("matched_via", ["preselected"])
        return [packet]
    return gather_candidates(project, slot, intent, lane_filters)


def _novelty_ranked(
    candidates: list[dict[str, Any]],
    used_segments: set[str],
    used_assets: set[str],
    *,
    exempt: bool,
) -> list[dict[str, Any]]:
    """Spend the novelty budget: penalize reuse unless the slot is a motif."""
    if exempt:
        return list(candidates)

    def _penalty(packet: dict[str, Any]) -> float:
        penalty = 0.0
        if packet["segment_id"] in used_segments:
            penalty += 3.0
        if packet["asset_id"] in used_assets:
            penalty += 1.0
        return penalty

    return sorted(
        candidates,
        key=lambda packet: float(packet.get("score") or 0) - _penalty(packet),
        reverse=True,
    )


def _register_motif(slot: dict[str, Any], packet: dict[str, Any], registry: dict[str, dict[str, Any]]) -> None:
    motif = slot.get("motif")
    if motif and motif["occurrence"] == 1 and motif["slot"] not in registry:
        registry[motif["slot"]] = packet


def _pin_anchor_slots(
    slots: list[dict[str, Any]],
    anchors_resolved: list[dict[str, Any]],
    warnings: list[str],
) -> None:
    """Pin resolved anchors to their slots; the structure is authored around them."""
    if not slots:
        return
    by_description = {anchor["description"]: anchor for anchor in anchors_resolved}
    floating: list[dict[str, Any]] = []
    for anchor in anchors_resolved:
        position = str(anchor.get("position") or "anywhere")
        if position == "first":
            slots[0]["preselected_packet"] = anchor["packet"]
            slots[0]["anchor"] = {"description": anchor["description"], "position": position}
        elif position == "last":
            slots[-1]["preselected_packet"] = anchor["packet"]
            slots[-1]["anchor"] = {"description": anchor["description"], "position": position}
        elif position.startswith("after:"):
            reference = by_description.get(position.split(":", 1)[1].strip())
            index = 0
            if reference:
                for slot_index, slot in enumerate(slots):
                    if (slot.get("anchor") or {}).get("description") == reference["description"]:
                        index = slot_index
                        break
            target = min(index + 1, len(slots) - 1)
            if slots[target].get("preselected_packet"):
                warnings.append(f"anchor {anchor['description']!r} could not pin: slot already anchored")
                continue
            slots[target]["preselected_packet"] = anchor["packet"]
            slots[target]["anchor"] = {"description": anchor["description"], "position": position}
        else:
            floating.append(anchor)
    for anchor in floating:
        target = next(
            (slot for slot in slots if not slot.get("preselected_packet") and not slot.get("anchor")),
            None,
        )
        if target is None:
            warnings.append(f"anchor {anchor['description']!r} resolved but no free slot could host it")
            continue
        target["preselected_packet"] = anchor["packet"]
        target["anchor"] = {"description": anchor["description"], "position": "anywhere"}


# --- Text helpers -----------------------------------------------------------------


def _packet_tokens(packet: dict[str, Any]) -> set[str]:
    evidence = packet.get("source_evidence", {})
    parts = [
        str(evidence.get("summary") or ""),
        str(evidence.get("transcript_summary") or ""),
        " ".join(str(unit.get("text") or "") for unit in evidence.get("word_units") or [] if isinstance(unit, dict)),
        " ".join(packet.get("story_roles") or []),
    ]
    for facet in evidence.get("facets") or []:
        parts.append(str(facet.get("observation_type") or ""))
        parts.append(str(facet.get("text") or ""))
        parts.append(str(facet.get("evidence") or ""))
    return _terms(" ".join(parts), keep_stop=True)


def _terms(text: Any, *, keep_stop: bool = False) -> set[str]:
    tokens = {token for token in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(token) > 1}
    if keep_stop:
        return tokens
    return {token for token in tokens if token not in STOP_TERMS}


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]
