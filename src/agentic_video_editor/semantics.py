from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import remap_semantic_payload, resolve_video_units
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now
from .transcript import _fts_query


SEMANTIC_PROMPT = """
You are the logging department of a professional edit suite. Your notes are the ONLY
thing the editor will ever know about this footage: if a signal is not written down
here, the editor cannot cut on it. Watch the whole video, then return JSON only:
{
  "segments": [
    {
      "start_sec": number,
      "end_sec": number,
      "kind": "candidate_moment",
      "summary": string,
      "transcript_summary": string,
      "word_units": [
        {"text": string, "start_sec": number, "end_sec": number, "kind": "spoken" | "sung" | "on_screen_text"}
      ],
      "people": [string],
      "actions": [string],
      "moods": [string],
      "story_roles": [string],
      "story_function": "thesis" | "setup" | "complication" | "turn" | "proof" | "reflection" | "payoff",
      "setup_questions": [string],
      "payoff_answers": [string],
      "audio_affordance": "clean_dialogue" | "music_bed" | "abrupt_song_change" | "silence" | "noisy",
      "visual_affordance": "close_up" | "wide" | "reaction" | "process_detail" | "performance" | "archive",
      "needs_caption": boolean,
      "quality_score": number,
      "usable": boolean,
      "cut_notes": string,
      "select": {
        "suggested_role": string,
        "score": number,
        "trim_start_sec": number,
        "trim_end_sec": number,
        "reason": string
      }
    }
  ],
  "relationships": [
    {
      "from_index": number,
      "to_index": number,
      "relationship_type": "sets_up" | "answers" | "contradicts" | "echoes" | "escalates" | "resolves" | "duplicates" | "requires_context",
      "confidence": number,
      "evidence": string
    }
  ]
}

Timing rules (most important):
- trim_start_sec must be a clean entry point: a breath before a phrase, a settled
  camera after a move, or a musical downbeat. Never start mid-word or mid-gesture.
- trim_end_sec must let the phrase, note, or gesture COMPLETE before the cut.
- Prefer moments whose in/out points sit next to pauses or shot changes, so they
  survive a ±0.5s timing error. State in cut_notes exactly what the in-point and
  out-point land on (e.g. "in: breath before 'I only know'; out: end of guitar phrase").
- word_units are short verbatim spoken phrases or on-screen text with their own tight
  timestamps; start_sec is when the FIRST word begins. For copyrighted lyrics, give a
  3-6 word identifying snippet plus theme, never full verses.

Editorial rules:
- Pick 4-8 editorially meaningful moments; skip filler even if that means fewer.
- summary must state what a viewer SEES and HEARS, not an interpretation of it.
- setup_questions: questions a viewer would ask after seeing this clip.
- payoff_answers: questions from elsewhere in the footage this clip answers.
- story_roles: hook, context, performance, process, emotion, payoff, archive, reaction, beauty_shot.
- Every relationship needs concrete evidence quoting what links the two moments.
- quality_score and select.score are 0-1; reserve scores above 0.8 for moments you
  would fight to keep in the final cut.
"""


@dataclass(frozen=True)
class SemanticSummary:
    assets_requested: int
    assets_completed: int
    segments_created: int
    selects_created: int
    relationships_created: int


def semantic_analyze_project(
    project: Project,
    *,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    limit: int | None = None,
    force: bool = False,
) -> SemanticSummary:
    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = _assets_with_video_ref(conn, limit=limit, source=provider_name, force=force)
        units_by_asset = {
            str(asset["id"]): resolve_video_units(
                conn, str(asset["id"]), Path(str(asset["video_ref"])), _float(asset.get("duration_sec"), None)
            )
            for asset in assets
        }

    totals = {"segments": 0, "selects": 0, "relationships": 0, "completed": 0}
    for asset in assets:
        asset_id = str(asset["id"])
        asset_duration = _float(asset.get("duration_sec"), None)
        units = units_by_asset[asset_id]
        if len(units) == 1:
            # unchunked path: unchanged from before chunking existed
            prompt = _semantic_prompt(asset_duration)
            payload = provider.generate_video_json(Path(str(asset["video_ref"])), prompt)
        else:
            merged_segments: list[dict[str, Any]] = []
            merged_relationships: list[dict[str, Any]] = []
            index_offset = 0
            for unit in units:
                # scale the moment-count guidance to what THIS chunk covers,
                # not the whole asset - otherwise a short chunk of a very
                # long asset is told to invent moments to hit an inflated count
                prompt = _semantic_prompt(unit.duration_sec)
                unit_payload = provider.generate_video_json(unit.path, prompt)
                segments, relationships, index_offset = remap_semantic_payload(unit_payload, unit, index_offset)
                merged_segments.extend(segments)
                merged_relationships.extend(relationships)
            payload = {"segments": merged_segments, "relationships": merged_relationships}
        result = _store_semantics(
            project,
            asset_id,
            payload,
            provider_name,
            duration_sec=asset_duration,
        )
        totals["segments"] += result["segments"]
        totals["selects"] += result["selects"]
        totals["relationships"] += result["relationships"]
        totals["completed"] += 1

    return SemanticSummary(
        assets_requested=len(assets),
        assets_completed=totals["completed"],
        segments_created=totals["segments"],
        selects_created=totals["selects"],
        relationships_created=totals["relationships"],
    )


def _moment_range(duration_sec: float | None) -> tuple[int, int]:
    """Scale segment inventory with footage length: ~one moment per 20-30s."""
    if not duration_sec or duration_sec <= 0:
        return 4, 8
    low = min(16, max(4, round(duration_sec / 30)))
    high = min(16, max(low + 1, round(duration_sec / 20)))
    return low, high


def _semantic_prompt(duration_sec: float | None) -> str:
    low, high = _moment_range(duration_sec)
    return SEMANTIC_PROMPT.replace(
        "Pick 4-8 editorially meaningful moments",
        f"Pick {low}-{high} editorially meaningful moments (about one per 20-30 seconds of footage)",
    )


def search_segments(project: Project, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select
                segments.id,
                segments.asset_id,
                assets.file_name,
                segments.start_sec,
                segments.end_sec,
                segments.kind,
                segments.summary,
                segments.story_roles_json,
                segments.quality_score,
                bm25(segments_fts) as rank
            from segments_fts
            join segments on segments.id = segments_fts.segment_id
            join assets on assets.id = segments.asset_id
            where segments_fts match ?
            order by rank
            limit ?
            """,
            (_fts_query(query), limit),
        ).fetchall()
    return [_segment_row(row) for row in rows]


def semantic_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        segments = conn.execute("select count(*) as count from segments where project_id = ?", ("default",)).fetchone()
        selects = conn.execute("select count(*) as count from selects where project_id = ?", ("default",)).fetchone()
        relationships = conn.execute(
            "select count(*) as count from relationships where project_id = ?",
            ("default",),
        ).fetchone()
    return {
        "segments": segments["count"],
        "selects": selects["count"],
        "relationships": relationships["count"],
    }


def _assets_with_video_ref(conn, *, limit: int | None, source: str, force: bool) -> list[dict[str, Any]]:
    query = """
        select
            assets.id,
            assets.path,
            assets.duration_sec,
            coalesce(proxy.path, assets.path) as video_ref
        from assets
        left join media_artifacts proxy
            on proxy.asset_id = assets.id and proxy.artifact_type = 'proxy'
        where assets.project_id = ? and assets.ingest_status = ?
    """
    params: list[Any] = ["default", "ready"]
    if not force:
        query += """
            and not exists (
                select 1
                from segments
                where segments.asset_id = assets.id
                  and segments.source = ?
            )
        """
        params.append(source)
    query += " order by assets.path"
    if limit is not None:
        query += " limit ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _store_semantics(
    project: Project,
    asset_id: str,
    payload: dict[str, Any],
    source: str,
    *,
    duration_sec: float | None,
) -> dict[str, int]:
    raw_segments = payload.get("segments") or []
    raw_relationships = payload.get("relationships") or []
    if not isinstance(raw_segments, list):
        raw_segments = []
    if not isinstance(raw_relationships, list):
        raw_relationships = []

    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute("delete from relationships where source = ? and (from_entity_id in (select id from segments where asset_id = ?) or to_entity_id in (select id from segments where asset_id = ?))", (source, asset_id, asset_id))
        conn.execute("delete from selects where source = ? and segment_id in (select id from segments where asset_id = ?)", (source, asset_id))
        conn.execute("delete from segments_fts where segment_id in (select id from segments where asset_id = ? and source = ?)", (asset_id, source))
        conn.execute("delete from segments where asset_id = ? and source = ?", (asset_id, source))

        segment_ids: list[str] = []
        selects_created = 0
        for raw in raw_segments:
            if not isinstance(raw, dict):
                continue
            summary = str(raw.get("summary") or "").strip()
            if not summary:
                continue
            segment_id = f"seg_{uuid.uuid4().hex[:16]}"
            segment_ids.append(segment_id)
            start = max(0.0, _float(raw.get("start_sec"), 0.0) or 0.0)
            end = max(start + 0.1, _float(raw.get("end_sec"), start + 8.0) or start + 8.0)
            if duration_sec is not None and duration_sec > 0:
                start = min(start, max(0.0, duration_sec - 0.1))
                end = min(max(start + 0.1, end), duration_sec)
            transcript_summary = str(raw.get("transcript_summary") or "").strip() or None
            people = _list(raw.get("people"))
            actions = _list(raw.get("actions"))
            moods = _list(raw.get("moods"))
            story_roles = _list(raw.get("story_roles"))
            word_units = _word_units(raw.get("word_units"), start, end)
            setup_questions = _list(raw.get("setup_questions"))
            payoff_answers = _list(raw.get("payoff_answers"))
            conn.execute(
                """
                insert into segments (
                    id, project_id, asset_id, start_sec, end_sec, kind,
                    summary, transcript_summary, people_json, actions_json,
                    moods_json, story_roles_json, quality_score, usable,
                    source, source_run_id, created_at,
                    word_units_json, story_function, setup_questions_json,
                    payoff_answers_json, audio_affordance, visual_affordance,
                    needs_caption, cut_notes
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    segment_id,
                    "default",
                    asset_id,
                    start,
                    end,
                    str(raw.get("kind") or "candidate_moment"),
                    summary,
                    transcript_summary,
                    json.dumps(people),
                    json.dumps(actions),
                    json.dumps(moods),
                    json.dumps(story_roles),
                    _float(raw.get("quality_score"), None),
                    1 if raw.get("usable", True) else 0,
                    source,
                    None,
                    utc_now(),
                    json.dumps(word_units),
                    _text_or_none(raw.get("story_function")),
                    json.dumps(setup_questions),
                    json.dumps(payoff_answers),
                    _text_or_none(raw.get("audio_affordance")),
                    _text_or_none(raw.get("visual_affordance")),
                    1 if raw.get("needs_caption") else 0,
                    _text_or_none(raw.get("cut_notes")),
                ),
            )
            fts_text = " ".join(
                [
                    summary,
                    transcript_summary or "",
                    " ".join(unit["text"] for unit in word_units),
                    " ".join(people),
                    " ".join(actions),
                    " ".join(moods),
                    " ".join(story_roles),
                    " ".join(setup_questions),
                    " ".join(payoff_answers),
                    str(raw.get("story_function") or ""),
                ]
            )
            conn.execute(
                "insert into segments_fts (segment_id, asset_id, text) values (?, ?, ?)",
                (segment_id, asset_id, fts_text),
            )
            select = raw.get("select")
            if isinstance(select, dict):
                _insert_select(conn, segment_id, start, end, select, source)
                selects_created += 1

        relationships_created = 0
        for raw in raw_relationships:
            if not isinstance(raw, dict):
                continue
            from_index = _int(raw.get("from_index"))
            to_index = _int(raw.get("to_index"))
            if from_index is None or to_index is None:
                continue
            if from_index < 0 or to_index < 0 or from_index >= len(segment_ids) or to_index >= len(segment_ids):
                continue
            conn.execute(
                """
                insert into relationships (
                    id, project_id, from_entity_type, from_entity_id, to_entity_type,
                    to_entity_id, relationship_type, confidence, evidence, source, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"rel_{uuid.uuid4().hex[:16]}",
                    "default",
                    "segment",
                    segment_ids[from_index],
                    "segment",
                    segment_ids[to_index],
                    str(raw.get("relationship_type") or "related"),
                    _float(raw.get("confidence"), None),
                    str(raw.get("evidence") or ""),
                    source,
                    utc_now(),
                ),
            )
            relationships_created += 1

    return {
        "segments": len(segment_ids),
        "selects": selects_created,
        "relationships": relationships_created,
    }


def _insert_select(conn, segment_id: str, start: float, end: float, select: dict[str, Any], source: str) -> None:
    trim_start = _float(select.get("trim_start_sec"), start) or start
    trim_end = _float(select.get("trim_end_sec"), end) or end
    conn.execute(
        """
        insert into selects (
            id, project_id, segment_id, directive_id, suggested_role, score,
            trim_start_sec, trim_end_sec, reason, source, created_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"select_{uuid.uuid4().hex[:16]}",
            "default",
            segment_id,
            None,
            str(select.get("suggested_role") or "candidate"),
            _float(select.get("score"), None),
            trim_start,
            max(trim_start + 0.1, trim_end),
            str(select.get("reason") or "Selected by semantic analysis."),
            source,
            utc_now(),
        ),
    )


def _segment_row(row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["story_roles"] = json.loads(data.pop("story_roles_json") or "[]")
    except json.JSONDecodeError:
        data["story_roles"] = []
    return data


def _word_units(value: Any, segment_start: float, segment_end: float) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    units: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        start = _float(raw.get("start_sec"), segment_start)
        end = _float(raw.get("end_sec"), segment_end)
        units.append(
            {
                "text": text,
                "start_sec": max(0.0, start if start is not None else segment_start),
                "end_sec": max(0.0, end if end is not None else segment_end),
                "kind": str(raw.get("kind") or "spoken"),
            }
        )
    return units


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value is None:
        return []
    return [str(value)]


def _float(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
