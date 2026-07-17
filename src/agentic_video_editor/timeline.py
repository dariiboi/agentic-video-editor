from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cutpoints import anchor_trim, load_cut_points, snap_range
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL
from .planner import create_edit_plan
from .project import Project, utc_now


@dataclass(frozen=True)
class TimelineSummary:
    timeline_id: str
    directive_id: str
    items_created: int
    duration_sec: float


def create_timeline(
    project: Project,
    *,
    directive: str,
    duration_sec: float = 60.0,
    query: str | None = None,
    max_clip_sec: float = 12.0,
    context_aware: bool = False,
    snap_tolerance_sec: float = 1.0,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
) -> TimelineSummary:
    directive_id = f"directive_{uuid.uuid4().hex[:16]}"
    timeline_id = f"timeline_{uuid.uuid4().hex[:16]}"
    now = utc_now()
    search_query = query or directive
    edit_plan: dict[str, Any] | None = None
    if context_aware:
        edit_plan = create_edit_plan(
            project,
            directive=directive,
            duration_sec=duration_sec,
            provider_name=provider_name,
            model=model,
            env_path=env_path,
        )

    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into directives (id, project_id, text, duration_sec, mode, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (directive_id, "default", directive, duration_sec, "auto", now),
        )
        items: list[dict[str, Any]] = []
        timeline_cursor = 0.0
        if context_aware:
            candidates = _planned_items(conn, edit_plan["selected_sequence"])
        else:
            candidates = _candidate_items(conn, search_query)
            if not candidates:
                candidates = _fallback_items(conn)

        cut_points_by_asset: dict[str, list[dict[str, Any]]] = {}
        for candidate in candidates:
            if timeline_cursor >= duration_sec:
                break
            cut_points = _asset_cut_points(conn, candidate["asset_id"], cut_points_by_asset)
            if snap_tolerance_sec > 0:
                _snap_candidate(candidate, cut_points, snap_tolerance_sec)
            item, clip_duration = _timeline_item_from_candidate(
                candidate,
                timeline_cursor=timeline_cursor,
                duration_sec=duration_sec,
                max_clip_sec=max_clip_sec,
                cut_points=cut_points,
                snap_tolerance_sec=snap_tolerance_sec,
            )
            items.append(item)
            timeline_cursor += clip_duration

        _assign_transitions(items)
        _assign_captions(items)
        timeline_json = {
            "timeline_id": timeline_id,
            "directive_id": directive_id,
            "directive": directive,
            "duration_target_sec": duration_sec,
            "context_aware": context_aware,
            "edit_plan": edit_plan,
            "tracks": [{"kind": "video", "name": "A-roll", "items": items}],
        }

        conn.execute(
            """
            insert into timelines (
                id, project_id, directive_id, name, duration_target_sec,
                timeline_json, status, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timeline_id,
                "default",
                directive_id,
                "Generated rough timeline",
                duration_sec,
                json.dumps(timeline_json, indent=2),
                "ready",
                now,
            ),
        )
        for item in items:
            conn.execute(
                """
                insert into timeline_items (
                    id, project_id, timeline_id, track_kind, track_name, asset_id,
                    segment_id, source_start_sec, source_end_sec, timeline_start_sec,
                    timeline_end_sec, role, reason, why_here, before_context,
                    after_context, caption_text, transition_note, continuity_score,
                    transition_json, caption_decision_json, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["id"],
                    "default",
                    timeline_id,
                    "video",
                    "A-roll",
                    item["asset_id"],
                    item["segment_id"],
                    item["source_start_sec"],
                    item["source_end_sec"],
                    item["timeline_start_sec"],
                    item["timeline_end_sec"],
                    item["role"],
                    item["reason"],
                    item.get("why_here"),
                    item.get("before_context"),
                    item.get("after_context"),
                    item.get("caption_text"),
                    item.get("transition_note"),
                    item.get("continuity_score"),
                    json.dumps(item.get("transition")) if item.get("transition") else None,
                    json.dumps(item.get("caption_decision")) if item.get("caption_decision") else None,
                    now,
                ),
            )

    return TimelineSummary(
        timeline_id=timeline_id,
        directive_id=directive_id,
        items_created=len(items),
        duration_sec=round(timeline_cursor, 3),
    )


def get_timeline(project: Project, timeline_id: str = "latest") -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        if timeline_id == "latest":
            row = conn.execute(
                "select id, timeline_json from timelines where project_id = ? order by created_at desc, rowid desc limit 1",
                ("default",),
            ).fetchone()
        else:
            row = conn.execute(
                "select id, timeline_json from timelines where project_id = ? and id = ?",
                ("default", timeline_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError("No timeline found")
    return json.loads(row["timeline_json"])


def timeline_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select id, directive_id, duration_target_sec, status, created_at
            from timelines
            where project_id = ?
            order by created_at desc
                , rowid desc
            """,
            ("default",),
        ).fetchall()
    return {"timelines": [dict(row) for row in rows]}


def _candidate_items(conn, query: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            """
            select
                selects.id as select_id,
                segments.id as segment_id,
                segments.asset_id,
                segments.start_sec,
                segments.end_sec,
                selects.trim_start_sec,
                selects.trim_end_sec,
                selects.suggested_role,
                selects.score,
                selects.reason,
                segments.summary,
                assets.fps,
                assets.duration_sec
            from segments_fts
            join segments on segments.id = segments_fts.segment_id
            join selects on selects.segment_id = segments.id
            join assets on assets.id = segments.asset_id
            where segments_fts match ?
              and segments.start_sec < coalesce(assets.duration_sec, segments.end_sec)
            order by coalesce(selects.score, 0) desc
            limit 20
            """,
            (_fts_query(query),),
        ).fetchall()
    except Exception:
        rows = []
    return [dict(row) for row in rows]


def _planned_items(conn, sequence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not sequence:
        return []
    fps_rows = conn.execute(
        "select id, fps, duration_sec from assets where project_id = ?",
        ("default",),
    ).fetchall()
    assets = {row["id"]: dict(row) for row in fps_rows}
    candidates = []
    for item in sequence:
        asset = assets.get(item["asset_id"], {})
        candidates.append(
            {
                "select_id": item.get("select_id"),
                "segment_id": item["segment_id"],
                "asset_id": item["asset_id"],
                "start_sec": item["source_start_sec"],
                "end_sec": item["source_end_sec"],
                "trim_start_sec": item["source_start_sec"],
                "trim_end_sec": item["source_end_sec"],
                "suggested_role": item["beat_role"],
                "score": item.get("continuity_score"),
                "reason": item["why_here"],
                "summary": item.get("source_evidence", {}).get("summary", item["why_here"]),
                "fps": asset.get("fps"),
                "duration_sec": asset.get("duration_sec"),
                "why_here": item["why_here"],
                "before_context": item.get("before_context"),
                "after_context": item.get("after_context"),
                "caption_text": item.get("caption_text"),
                "transition_note": item["transition_note"],
                "continuity_score": item.get("continuity_score"),
                "warnings": item.get("warnings") or [],
                "audio_affordance": item.get("source_evidence", {}).get("audio_affordance"),
                "word_units": item.get("source_evidence", {}).get("word_units") or [],
                "needs_caption": item.get("source_evidence", {}).get("needs_caption"),
                "max_duration_sec": item.get("max_duration_sec"),
                "trim_anchor": item.get("trim_anchor"),
            }
        )
    return candidates


def _timeline_item_from_candidate(
    candidate: dict[str, Any],
    *,
    timeline_cursor: float,
    duration_sec: float,
    max_clip_sec: float,
    cut_points: list[dict[str, Any]] | None = None,
    snap_tolerance_sec: float = 0.0,
) -> tuple[dict[str, Any], float]:
    source_start = float(candidate["trim_start_sec"] or candidate["start_sec"])
    source_end = float(candidate["trim_end_sec"] or candidate["end_sec"])
    asset_duration = candidate.get("duration_sec")
    if asset_duration is not None:
        asset_duration = float(asset_duration)
        source_start = min(source_start, max(0.0, asset_duration - 0.1))
        source_end = min(max(source_start + 0.1, source_end), asset_duration)
    effective_max = min(max_clip_sec, float(candidate.get("max_duration_sec") or max_clip_sec))
    clip_duration = max(0.1, min(effective_max, source_end - source_start, duration_sec - timeline_cursor))
    truncated = clip_duration < (source_end - source_start) - 0.01
    trim_anchor = candidate.get("trim_anchor")
    if truncated:
        source_start, source_end, anchor = anchor_trim(
            source_start,
            source_end,
            clip_duration,
            candidate.get("word_units") or [],
            hint=str(candidate.get("reason") or ""),
        )
        if anchor:
            trim_anchor = anchor
        if cut_points and snap_tolerance_sec > 0:
            snap = snap_range(cut_points, source_start, source_end, tolerance_sec=snap_tolerance_sec)
            if snap["start_snapped_to"] or snap["end_snapped_to"]:
                source_start = snap["start_sec"]
                source_end = snap["end_sec"]
                candidate["cut_snap_end"] = "truncation_snapped"
        # snapping may drift past the max shot length; that limit is hard
        source_end = min(source_end, source_start + effective_max)
        clip_duration = max(0.1, source_end - source_start)
    else:
        source_end = source_start + clip_duration
    item = {
        "id": f"item_{uuid.uuid4().hex[:16]}",
        "asset_id": candidate["asset_id"],
        "segment_id": candidate["segment_id"],
        "source_start_sec": round(source_start, 3),
        "source_end_sec": round(source_end, 3),
        "timeline_start_sec": round(timeline_cursor, 3),
        "timeline_end_sec": round(timeline_cursor + clip_duration, 3),
        "role": candidate["suggested_role"] or "candidate",
        "reason": candidate["reason"] or candidate["summary"],
        "why_here": candidate.get("why_here") or candidate["reason"] or candidate["summary"],
        "before_context": candidate.get("before_context"),
        "after_context": candidate.get("after_context"),
        "caption_text": candidate.get("caption_text"),
        "transition_note": candidate.get("transition_note"),
        "continuity_score": candidate.get("continuity_score"),
        "cut_snap": candidate.get("cut_snap"),
        "cut_snap_end": candidate.get("cut_snap_end"),
        "warnings": candidate.get("warnings") or [],
        "audio_affordance": candidate.get("audio_affordance"),
        "trim_anchor": trim_anchor,
        "needs_caption": candidate.get("needs_caption"),
    }
    item.update(_rational_time_fields(item, float(candidate["fps"] or 30.0)))
    return item, clip_duration


def _assign_transitions(items: list[dict[str, Any]]) -> None:
    """Decide per-join transitions from the evidence on each clip.

    The renderer executes these; a global --crossfade-sec flag only overrides.
    """
    for index, item in enumerate(items):
        if index == 0:
            item["transition"] = {"type": "cut", "why": "opening clip; establish the subject cleanly"}
        else:
            item["transition"] = _decide_transition(items[index - 1], item)


MUSIC_AFFORDANCES = {"music_bed", "abrupt_song_change"}


def _decide_transition(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    warnings = set(current.get("warnings") or []) | set(previous.get("warnings") or [])
    prev_audio = previous.get("audio_affordance")
    cur_audio = current.get("audio_affordance")

    if previous["asset_id"] == current["asset_id"]:
        return {
            "type": "crossfade",
            "duration_sec": 0.35,
            "why": "same-source jump cut; a short dissolve hides the time skip",
        }
    if "abrupt_audio_or_no_audio" in warnings:
        return {
            "type": "crossfade",
            "duration_sec": 0.3,
            "why": "audio bed drops or appears abruptly across the join",
        }
    if prev_audio in MUSIC_AFFORDANCES and cur_audio in MUSIC_AFFORDANCES:
        return {
            "type": "crossfade",
            "duration_sec": 0.4,
            "why": "music-to-music source change; blend the beds",
        }
    if "check_audio_transition" in warnings and cur_audio != "clean_dialogue":
        return {
            "type": "crossfade",
            "duration_sec": 0.4,
            "why": "flagged audio transition risk into a non-dialogue clip",
        }
    if cur_audio == "clean_dialogue" or prev_audio == "clean_dialogue":
        return {"type": "cut", "why": "dialogue boundary; hard cut keeps words crisp"}
    return {"type": "cut", "why": "no audio risk detected; hard cut preserves energy"}


def _asset_cut_points(
    conn,
    asset_id: str,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if asset_id not in cache:
        cache[asset_id] = load_cut_points(conn, asset_id)
    return cache[asset_id]


def _snap_candidate(
    candidate: dict[str, Any],
    cut_points: list[dict[str, Any]],
    tolerance_sec: float,
) -> None:
    if not cut_points:
        return
    start = float(candidate["trim_start_sec"] or candidate["start_sec"])
    end = float(candidate["trim_end_sec"] or candidate["end_sec"])
    snap = snap_range(cut_points, start, end, tolerance_sec=tolerance_sec)
    if snap["start_snapped_to"] or snap["end_snapped_to"]:
        candidate["trim_start_sec"] = snap["start_sec"]
        candidate["trim_end_sec"] = snap["end_sec"]
        candidate["cut_snap"] = snap


def _assign_captions(items: list[dict[str, Any]]) -> None:
    """Decide per item whether its caption should burn, with a why.

    Legacy segments have needs_caption unset, so context/archive roles keep
    their grounding overlays as a fallback.
    """
    for item in items:
        if not item.get("caption_text"):
            item["caption_decision"] = {"burn": False, "why": "no caption text available"}
        elif item.get("needs_caption"):
            item["caption_decision"] = {"burn": True, "why": "segment is marked needs_caption"}
        elif _norm_role(item.get("role")) in ("context", "archive"):
            item["caption_decision"] = {"burn": True, "why": "context/archive beat benefits from grounding text"}
        else:
            item["caption_decision"] = {
                "burn": False,
                "why": "clip reads without words on screen; keep the frame clean",
            }


def _norm_role(value: Any) -> str:
    return str(value or "").strip().lower()


def _fallback_items(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            selects.id as select_id,
            segments.id as segment_id,
            segments.asset_id,
            segments.start_sec,
            segments.end_sec,
            selects.trim_start_sec,
            selects.trim_end_sec,
            selects.suggested_role,
            selects.score,
            selects.reason,
            segments.summary,
            assets.fps,
            assets.duration_sec
        from selects
        join segments on segments.id = selects.segment_id
        join assets on assets.id = segments.asset_id
        where segments.start_sec < coalesce(assets.duration_sec, segments.end_sec)
        order by coalesce(selects.score, 0) desc, segments.start_sec
        limit 20
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _fts_query(query: str) -> str:
    terms = [term.replace('"', "") for term in query.split() if term.strip()]
    return " OR ".join(f'"{term}"' for term in terms) or '""'


def _rational_time_fields(item: dict[str, Any], fps: float) -> dict[str, Any]:
    rate = round(fps or 30.0, 6)
    return {
        "source_start_time": {"value": round(item["source_start_sec"] * rate, 3), "rate": rate},
        "source_duration_time": {
            "value": round((item["source_end_sec"] - item["source_start_sec"]) * rate, 3),
            "rate": rate,
        },
        "timeline_start_time": {"value": round(item["timeline_start_sec"] * rate, 3), "rate": rate},
        "timeline_duration_time": {
            "value": round((item["timeline_end_sec"] - item["timeline_start_sec"]) * rate, 3),
            "rate": rate,
        },
    }
