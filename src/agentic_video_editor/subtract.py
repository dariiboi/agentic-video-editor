from __future__ import annotations

import json
import uuid
from typing import Any

from .casting import _terms
from .cutpoints import load_cut_points, snap_range
from .db import connect_db, migrate
from .facets import observation_text
from .project import Project


# Vocabulary that routes a subtractive directive to the mechanical detectors.
# These are recognition tables for what the user asked to remove — routing,
# not an editorial default: nothing is removed unless the directive names it.
SILENCE_MARKERS = ("silence", "silences", "silent", "quiet", "dead air", "pauses", "pause")
DEAD_MARKERS = ("dead moment", "dead time", "boring", "dull", "filler", "nothing happens", "uneventful")

# Low-energy evidence phrases looked for in facet observations when the
# directive asks for "dead moments" to go. Multi-word entries match as
# phrases, single words as whole tokens.
LOW_ENERGY_PHRASES = ("low energy", "no motion", "no movement", "nothing happens", "still frame", "static shot", "empty frame", "dead air")
LOW_ENERGY_TOKENS = {"idle", "motionless", "uneventful", "silent"}

_SUFFIXES = ("ing", "ers", "er", "ed", "es", "s")


def plan_subtraction(
    project: Project,
    intent: dict[str, Any],
    *,
    duration_sec: float | None = None,
    margin_sec: float = 0.2,
    merge_gap_sec: float = 0.5,
    min_keep_sec: float = 0.5,
    min_removal_sec: float = 0.2,
    min_silence_sec: float = 0.6,
    min_speechless_sec: float = 1.5,
    snap_tolerance_sec: float = 0.5,
) -> dict[str, Any]:
    """Keep-and-remove planning: the plan is the kept-region list with whys.

    Every removed region carries its evidence (audio gap, ASR speechless
    stretch, low-energy observation, or a semantic match for a target the
    directive named) and a why. Output duration is emergent — the remainder
    is the deliverable, so nothing is fitted to a duration budget.
    """
    directive = str(intent.get("directive") or "")
    lowered = " ".join(directive.lower().split())
    remove_silence = any(marker in lowered for marker in SILENCE_MARKERS)
    remove_dead = any(marker in lowered for marker in DEAD_MARKERS)
    targets = _semantic_targets(intent)

    sources: list[dict[str, Any]] = []
    if remove_silence:
        sources.append({"source": "audio_gap", "activated_by": "directive asks to remove silences/pauses"})
        sources.append({"source": "asr_speechless", "activated_by": "directive asks to remove silences/pauses"})
    if remove_dead:
        sources.append({"source": "low_energy_observation", "activated_by": "directive asks to remove dead/boring moments"})
        if not remove_silence:
            sources.append({"source": "asr_speechless", "activated_by": "dead moments include long speechless stretches"})
    for target in targets:
        sources.append({"source": "semantic_match", "activated_by": f"directive names removal target {target!r}"})

    warnings: list[str] = []
    if not sources:
        warnings.append(
            "directive named no recognizable removal target; nothing was removed "
            "(kept regions cover the full assets)"
        )

    target_counts: dict[str, int] = {target: 0 for target in targets}
    removed_regions: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    kept_total = 0.0
    removed_total = 0.0
    cursor = 0.0

    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = [
            dict(row)
            for row in conn.execute(
                """
                select id, file_name, duration_sec, has_audio
                from assets
                where project_id = ? and ingest_status = ?
                order by rowid
                """,
                ("default", "ready"),
            ).fetchall()
        ]
        for asset in assets:
            asset_duration = float(asset["duration_sec"] or 0.0)
            if asset_duration <= 0:
                continue
            candidates: list[dict[str, Any]] = []
            if remove_silence:
                candidates += _gap_removals(conn, asset["id"], asset_duration, min_silence_sec)
            if remove_silence or remove_dead:
                candidates += _speechless_removals(conn, asset["id"], asset_duration, min_speechless_sec)
            if remove_dead:
                candidates += _low_energy_removals(conn, asset["id"], asset_duration)
            for target in targets:
                matches = _semantic_removals(conn, asset["id"], asset_duration, target)
                target_counts[target] += len(matches)
                candidates += matches

            units = _word_units(conn, asset["id"])
            cut_points = load_cut_points(conn, asset["id"])
            result = subtract_regions(
                candidates,
                asset_duration,
                margin_sec=margin_sec,
                merge_gap_sec=merge_gap_sec,
                min_keep_sec=min_keep_sec,
                min_removal_sec=min_removal_sec,
                word_units=units,
                cut_points=cut_points,
                snap_tolerance_sec=snap_tolerance_sec,
            )
            if not result["kept"]:
                warnings.append(f"{asset['file_name']}: every region matched removal evidence; asset dropped entirely")
            for region in result["removed"]:
                removed_total += region["end_sec"] - region["start_sec"]
                removed_regions.append(
                    {"asset_id": asset["id"], "file_name": asset["file_name"], **region}
                )
            for region in result["kept"]:
                item = _kept_item(
                    conn,
                    asset,
                    region,
                    result["removed"],
                    units,
                    index=len(items),
                    cursor=cursor,
                )
                cursor = item["timeline_end_sec"]
                kept_total += item["duration_sec"]
                items.append(item)

    for target, count in target_counts.items():
        if count == 0:
            warnings.append(f"removal target {target!r}: no evidence found in the index; nothing removed for it")

    duration_note = None
    if duration_sec is not None:
        duration_note = (
            f"subtractive output duration is emergent ({round(kept_total, 3)}s kept); "
            f"the {duration_sec}s target is not fitted against"
        )

    return {
        "plan_id": f"plan_{uuid.uuid4().hex[:16]}",
        "engine": "structured",
        "mode": "subtract",
        "status": "ok" if sources else "no_removal_evidence",
        "directive": directive,
        "duration_target_sec": duration_sec,
        "duration_note": duration_note,
        "intent_analysis": intent,
        "structure_id": None,
        "removal_sources": sources,
        "removal_targets": {
            "attributes": target_counts,
            "zero_coverage": sorted(target for target, count in target_counts.items() if count == 0),
        },
        "removed_regions": removed_regions,
        "selected_sequence": items,
        "kept_total_sec": round(kept_total, 3),
        "removed_total_sec": round(removed_total, 3),
        "casting_warnings": warnings,
        "sequencing_note": "kept regions in chronological source order; removals evidence-driven per region",
    }


# --- Region math (pure) -----------------------------------------------------------


def merge_removals(regions: list[dict[str, Any]], *, merge_gap_sec: float = 0.5) -> list[dict[str, Any]]:
    """Merge overlapping removals and removals whose kept sliver would be
    shorter than merge_gap_sec (auto-editor's merge step)."""
    ordered = sorted(
        (dict(region) for region in regions if region["end_sec"] > region["start_sec"]),
        key=lambda region: (region["start_sec"], region["end_sec"]),
    )
    merged: list[dict[str, Any]] = []
    for region in ordered:
        region.setdefault("evidence", [])
        region.setdefault("why", [])
        if merged and region["start_sec"] - merged[-1]["end_sec"] < merge_gap_sec:
            merged[-1]["end_sec"] = max(merged[-1]["end_sec"], region["end_sec"])
            merged[-1]["evidence"] = merged[-1]["evidence"] + region["evidence"]
            merged[-1]["why"] = _dedupe(merged[-1]["why"] + region["why"])
        else:
            merged.append(region)
    return merged


def apply_margin(
    regions: list[dict[str, Any]],
    *,
    margin_sec: float = 0.2,
    min_removal_sec: float = 0.2,
) -> list[dict[str, Any]]:
    """Pad kept content by shrinking each removal margin_sec per side
    (auto-editor's margin step); removals that collapse are dropped."""
    padded = []
    for region in regions:
        start = region["start_sec"] + margin_sec
        end = region["end_sec"] - margin_sec
        if end - start >= min_removal_sec:
            padded.append({**region, "start_sec": start, "end_sec": end})
    return padded


def complement_regions(removals: list[dict[str, Any]], duration_sec: float) -> list[dict[str, Any]]:
    """Kept regions are the complement of the removals over [0, duration]."""
    kept = []
    cursor = 0.0
    for region in sorted(removals, key=lambda item: item["start_sec"]):
        start = max(0.0, min(region["start_sec"], duration_sec))
        end = max(0.0, min(region["end_sec"], duration_sec))
        if start - cursor > 1e-6:
            kept.append({"start_sec": cursor, "end_sec": start})
        cursor = max(cursor, end)
    if duration_sec - cursor > 1e-6:
        kept.append({"start_sec": cursor, "end_sec": duration_sec})
    return kept


def subtract_regions(
    candidates: list[dict[str, Any]],
    duration_sec: float,
    *,
    margin_sec: float = 0.2,
    merge_gap_sec: float = 0.5,
    min_keep_sec: float = 0.5,
    min_removal_sec: float = 0.2,
    word_units: list[dict[str, Any]] | None = None,
    cut_points: list[dict[str, Any]] | None = None,
    snap_tolerance_sec: float = 0.0,
) -> dict[str, Any]:
    """Full per-asset region pipeline: clamp -> merge -> margin -> complement
    -> sliver absorption -> snap -> word-unit guard -> final evidence mapping.

    Returns {"kept": [...], "removed": [...]}; kept and removed partition
    [0, duration] exactly, and every removed region carries evidence + why.
    """
    clamped = []
    for region in candidates:
        start = max(0.0, float(region["start_sec"]))
        end = min(duration_sec, float(region["end_sec"]))
        if end - start > 1e-6:
            clamped.append({**region, "start_sec": start, "end_sec": end})
    merged = merge_removals(clamped, merge_gap_sec=merge_gap_sec)
    padded = apply_margin(merged, margin_sec=margin_sec, min_removal_sec=min_removal_sec)

    kept = complement_regions(padded, duration_sec)
    evidence_sources = list(merged)
    slivers = [region for region in kept if region["end_sec"] - region["start_sec"] < min_keep_sec]
    if slivers and len(slivers) < len(kept):
        sliver_removals = [
            {
                **sliver,
                "evidence": [],
                "why": [f"kept sliver {round(sliver['end_sec'] - sliver['start_sec'], 3)}s is too short to keep"],
            }
            for sliver in slivers
        ]
        evidence_sources += sliver_removals
        padded = merge_removals(padded + sliver_removals, merge_gap_sec=0.001)
        kept = complement_regions(padded, duration_sec)

    guards: list[dict[str, Any]] = []
    for region in kept:
        if snap_tolerance_sec > 0 and cut_points:
            snap = snap_range(
                cut_points,
                region["start_sec"],
                region["end_sec"],
                tolerance_sec=snap_tolerance_sec,
                min_duration_sec=min_keep_sec,
            )
            if region["start_sec"] > 1e-6 and snap["start_snapped_to"]:
                region["start_sec"] = max(0.0, snap["start_sec"])
                region["start_snapped_to"] = snap["start_snapped_to"]
            if region["end_sec"] < duration_sec - 1e-6 and snap["end_snapped_to"]:
                region["end_sec"] = min(duration_sec, snap["end_sec"])
                region["end_snapped_to"] = snap["end_snapped_to"]
        guards += _guard_kept_region(region, word_units or [], duration_sec)

    kept = _merge_kept(kept)
    removed = _map_removed_evidence(kept, evidence_sources, duration_sec)
    return {"kept": kept, "removed": removed, "word_unit_guards": guards}


def _guard_kept_region(
    region: dict[str, Any],
    word_units: list[dict[str, Any]],
    duration_sec: float,
) -> list[dict[str, Any]]:
    """Never let a kept boundary land inside a word unit: extend the kept
    region to let the unit finish (keeping a whole word beats clipping it)."""
    guards = []
    for unit in word_units:
        try:
            unit_start = float(unit["start_sec"])
            unit_end = float(unit["end_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if unit_start < region["start_sec"] < unit_end:
            region["start_sec"] = max(0.0, unit_start - 0.05)
            guard = {"action": "extended_start", "text": str(unit.get("text") or "")[:48]}
            region["word_unit_guard"] = guard
            guards.append(guard)
        if unit_start < region["end_sec"] < unit_end:
            region["end_sec"] = min(duration_sec, unit_end + 0.1)
            guard = {"action": "extended_end", "text": str(unit.get("text") or "")[:48]}
            region["word_unit_guard"] = guard
            guards.append(guard)
    return guards


def _merge_kept(kept: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(kept, key=lambda region: region["start_sec"])
    merged: list[dict[str, Any]] = []
    for region in ordered:
        if merged and region["start_sec"] <= merged[-1]["end_sec"] + 1e-6:
            merged[-1]["end_sec"] = max(merged[-1]["end_sec"], region["end_sec"])
        else:
            merged.append(region)
    return [region for region in merged if region["end_sec"] - region["start_sec"] > 1e-6]


def _map_removed_evidence(
    kept: list[dict[str, Any]],
    merged_candidates: list[dict[str, Any]],
    duration_sec: float,
) -> list[dict[str, Any]]:
    """Final removed regions are the complement of the final kept regions;
    each collects the evidence of every original candidate it overlaps."""
    removed = complement_regions(
        [{"start_sec": r["start_sec"], "end_sec": r["end_sec"]} for r in kept], duration_sec
    )
    final = []
    for region in removed:
        overlapping = [
            candidate
            for candidate in merged_candidates
            if candidate["start_sec"] < region["end_sec"] and candidate["end_sec"] > region["start_sec"]
        ]
        evidence = [entry for candidate in overlapping for entry in candidate.get("evidence", [])]
        whys = _dedupe([why for candidate in overlapping for why in candidate.get("why", [])])
        if not whys:
            whys = ["margin or sliver consolidation around kept content"]
        final.append(
            {
                "start_sec": round(region["start_sec"], 3),
                "end_sec": round(region["end_sec"], 3),
                "evidence": evidence,
                "why": "; ".join(whys),
            }
        )
    return final


# --- Evidence sources -------------------------------------------------------------


def _gap_removals(conn, asset_id: str, duration_sec: float, min_silence_sec: float) -> list[dict[str, Any]]:
    points = [point for point in load_cut_points(conn, asset_id) if point["reason"] in ("gap_start", "gap_end")]
    intervals: list[tuple[float, float]] = []
    pending: float | None = None
    for point in points:
        time_sec = float(point["time_sec"])
        if point["reason"] == "gap_start":
            pending = time_sec
        elif point["reason"] == "gap_end":
            intervals.append((pending if pending is not None else 0.0, time_sec))
            pending = None
    if pending is not None:
        intervals.append((pending, duration_sec))
    removals = []
    for start, end in intervals:
        if end - start < min_silence_sec:
            continue
        removals.append(
            {
                "start_sec": start,
                "end_sec": end,
                "evidence": [
                    {
                        "type": "audio_gap",
                        "detail": f"detected silence {round(start, 3)}-{round(end, 3)}s",
                        "time_range": [round(start, 3), round(end, 3)],
                    }
                ],
                "why": [f"silence of {round(end - start, 2)}s; the directive asks to remove silences"],
            }
        )
    return removals


def _speechless_removals(conn, asset_id: str, duration_sec: float, min_speechless_sec: float) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select start_sec, end_sec from word_alignments where asset_id = ? order by start_sec",
        (asset_id,),
    ).fetchall()
    if not rows:
        return []
    edges = [(0.0, float(rows[0]["start_sec"]))]
    for previous, current in zip(rows, rows[1:]):
        edges.append((float(previous["end_sec"]), float(current["start_sec"])))
    edges.append((float(rows[-1]["end_sec"]), duration_sec))
    removals = []
    for start, end in edges:
        if end - start < min_speechless_sec:
            continue
        removals.append(
            {
                "start_sec": start,
                "end_sec": end,
                "evidence": [
                    {
                        "type": "asr_speechless",
                        "detail": f"no aligned speech {round(start, 3)}-{round(end, 3)}s",
                        "time_range": [round(start, 3), round(end, 3)],
                    }
                ],
                "why": [f"{round(end - start, 2)}s without any aligned speech"],
            }
        )
    return removals


def _low_energy_removals(conn, asset_id: str, duration_sec: float) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select id, observation_type, value, start_sec, end_sec
        from observations
        where asset_id = ? and observation_type in ('audio_character', 'actions_events')
          and start_sec is not null and end_sec is not null
        """,
        (asset_id,),
    ).fetchall()
    removals = []
    for row in rows:
        text = observation_text(_json_value(row["value"])).lower()
        tokens = _terms(text, keep_stop=True)
        phrase_hit = next((phrase for phrase in LOW_ENERGY_PHRASES if phrase in text), None)
        token_hit = next((token for token in LOW_ENERGY_TOKENS if token in tokens), None)
        hit = phrase_hit or token_hit
        if not hit:
            continue
        start = max(0.0, float(row["start_sec"]))
        end = min(duration_sec, float(row["end_sec"]))
        if end <= start:
            continue
        removals.append(
            {
                "start_sec": start,
                "end_sec": end,
                "evidence": [
                    {
                        "type": "low_energy_observation",
                        "observation_id": row["id"],
                        "observation_type": row["observation_type"],
                        "detail": text[:160],
                        "time_range": [round(start, 3), round(end, 3)],
                    }
                ],
                "why": [f"{row['observation_type']} observation reads low-energy ({hit!r})"],
            }
        )
    return removals


def _semantic_removals(conn, asset_id: str, duration_sec: float, target: str) -> list[dict[str, Any]]:
    required = {_stem(term) for term in _terms(target) if len(term) > 2}
    if not required:
        return []
    removals = []
    rows = conn.execute(
        """
        select id, observation_type, value, start_sec, end_sec
        from observations
        where asset_id = ? and start_sec is not null and end_sec is not null
        """,
        (asset_id,),
    ).fetchall()
    for row in rows:
        text = observation_text(_json_value(row["value"]))
        if not _stems_match(required, text):
            continue
        start = max(0.0, float(row["start_sec"]))
        end = min(duration_sec, float(row["end_sec"]))
        if end <= start:
            continue
        removals.append(
            {
                "start_sec": start,
                "end_sec": end,
                "evidence": [
                    {
                        "type": "semantic_match",
                        "target": target,
                        "observation_id": row["id"],
                        "observation_type": row["observation_type"],
                        "detail": text[:160],
                        "time_range": [round(start, 3), round(end, 3)],
                    }
                ],
                "why": [f"matches removal target {target!r} via {row['observation_type']} evidence"],
            }
        )
    spans = conn.execute(
        "select id, start_sec, end_sec, text from transcript_spans where asset_id = ?",
        (asset_id,),
    ).fetchall()
    for span in spans:
        if not _stems_match(required, str(span["text"] or "")):
            continue
        start = max(0.0, float(span["start_sec"]))
        end = min(duration_sec, float(span["end_sec"]))
        if end <= start:
            continue
        removals.append(
            {
                "start_sec": start,
                "end_sec": end,
                "evidence": [
                    {
                        "type": "semantic_match",
                        "target": target,
                        "span_id": span["id"],
                        "detail": str(span["text"] or "")[:160],
                        "time_range": [round(start, 3), round(end, 3)],
                    }
                ],
                "why": [f"matches removal target {target!r} in the transcript"],
            }
        )
    return removals


def _semantic_targets(intent: dict[str, Any]) -> list[str]:
    mechanical = set(SILENCE_MARKERS) | set(DEAD_MARKERS)
    targets = []
    for attribute in intent.get("evidence_attributes") or []:
        text = str(attribute).strip()
        if text and text.lower() not in mechanical:
            targets.append(text)
    return targets


# --- Kept-region plan items -------------------------------------------------------


def _kept_item(
    conn,
    asset: dict[str, Any],
    region: dict[str, Any],
    removed: list[dict[str, Any]],
    word_units: list[dict[str, Any]],
    *,
    index: int,
    cursor: float,
) -> dict[str, Any]:
    start, end = region["start_sec"], region["end_sec"]
    duration = end - start
    before = next((r for r in removed if abs(r["end_sec"] - start) < 0.05), None)
    after = next((r for r in removed if abs(r["start_sec"] - end) < 0.05), None)
    why_parts = [f"kept {round(start, 2)}-{round(end, 2)}s of {asset['file_name']}"]
    if before:
        why_parts.append(f"follows a removal ({before['why']})")
    if after:
        why_parts.append(f"precedes a removal ({after['why']})")
    if not before and not after:
        why_parts.append("no removal evidence inside this range")
    units_in_range = [
        unit
        for unit in word_units
        if float(unit.get("end_sec", 0)) > start and float(unit.get("start_sec", 0)) < end
    ]
    if index == 0:
        transition_note = "Open on the first kept region."
    elif before:
        transition_note = "Join across a removed region; expect a jump cut within the same source."
    else:
        transition_note = "Continues from the previous kept region."
    segment_row = conn.execute(
        """
        select id from segments
        where asset_id = ? and start_sec < ? and end_sec > ?
        order by start_sec limit 1
        """,
        (asset["id"], end, start),
    ).fetchone()
    item = {
        "sequence_index": index,
        "beat_id": f"keep_{index + 1}",
        "beat_role": "kept_region",
        "slot_id": f"keep_{index + 1}",
        "segment_id": segment_row["id"] if segment_row else None,
        "select_id": None,
        "asset_id": asset["id"],
        "file_name": asset["file_name"],
        "source_start_sec": round(start, 3),
        "source_end_sec": round(end, 3),
        "duration_sec": round(duration, 3),
        "timeline_start_sec": round(cursor, 3),
        "timeline_end_sec": round(cursor + duration, 3),
        "why_here": "; ".join(why_parts),
        "transition_note": transition_note,
        "warnings": [],
        "source_evidence": {
            "summary": why_parts[0],
            "word_units": units_in_range,
            "removed_before": {"start_sec": before["start_sec"], "end_sec": before["end_sec"], "why": before["why"]} if before else None,
            "removed_after": {"start_sec": after["start_sec"], "end_sec": after["end_sec"], "why": after["why"]} if after else None,
        },
    }
    if region.get("word_unit_guard"):
        item["word_unit_guard"] = region["word_unit_guard"]
    if region.get("start_snapped_to") or region.get("end_snapped_to"):
        item["cut_snap"] = {
            "start_snapped_to": region.get("start_snapped_to"),
            "end_snapped_to": region.get("end_snapped_to"),
        }
    return item


def _word_units(conn, asset_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select start_sec, end_sec, text from word_alignments where asset_id = ? order by start_sec",
        (asset_id,),
    ).fetchall()
    return [dict(row) for row in rows]


# --- Small helpers ----------------------------------------------------------------


def _stem(token: str) -> str:
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


def _stems_match(required_stems: set[str], text: str) -> bool:
    stems = {_stem(token) for token in _terms(text, keep_stop=True)}
    return required_stems <= stems


def _json_value(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {"items": value}


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
