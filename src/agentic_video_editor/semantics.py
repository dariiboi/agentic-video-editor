from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now
from .transcript import _fts_query


SEMANTIC_PROMPT = """
Analyze this video for an autonomous video editor. Return JSON only:
{
  "segments": [
    {
      "start_sec": number,
      "end_sec": number,
      "kind": "candidate_moment",
      "summary": string,
      "transcript_summary": string,
      "people": [string],
      "actions": [string],
      "moods": [string],
      "story_roles": [string],
      "quality_score": number,
      "usable": boolean,
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
      "relationship_type": string,
      "confidence": number,
      "evidence": string
    }
  ]
}

Rules:
- Use approximate timestamps in seconds.
- Pick 4-8 editorially meaningful moments.
- Do not transcribe long song lyrics verbatim; summarize musical/lyrical themes.
- Include story roles such as hook, context, performance, process, emotion, payoff, archive, reaction, beauty_shot.
- Prefer moments that could help build a short documentary/performance montage.
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

    totals = {"segments": 0, "selects": 0, "relationships": 0, "completed": 0}
    for asset in assets:
        payload = provider.generate_video_json(Path(str(asset["video_ref"])), SEMANTIC_PROMPT)
        result = _store_semantics(
            project,
            str(asset["id"]),
            payload,
            provider_name,
            duration_sec=_float(asset.get("duration_sec"), None),
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
            conn.execute(
                """
                insert into segments (
                    id, project_id, asset_id, start_sec, end_sec, kind,
                    summary, transcript_summary, people_json, actions_json,
                    moods_json, story_roles_json, quality_score, usable,
                    source, source_run_id, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            fts_text = " ".join(
                [
                    summary,
                    transcript_summary or "",
                    " ".join(people),
                    " ".join(actions),
                    " ".join(moods),
                    " ".join(story_roles),
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
