from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .chunking import remap_flat_items, resolve_video_units
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now


TRANSCRIPT_PROMPT = """
You are producing the timestamped transcript log for a video editor. Editors will
later cut the video by QUOTING your spans back, so each span must be something a
cut can safely land on. Return JSON only:
{
  "spans": [
    {
      "start_sec": number,
      "end_sec": number,
      "kind": "speech" | "lyric_summary" | "music" | "visual_summary",
      "speaker": string | null,
      "text": string,
      "confidence": number
    }
  ]
}

Timing rules (most important):
- start_sec is the instant the FIRST word or note of the span begins - not the
  start of the surrounding scene. end_sec is when the LAST word or note finishes.
- Break speech at natural phrase boundaries (breaths, sentence ends), never
  mid-sentence. One complete utterance per span beats one long paragraph.
- If speech is continuous, split it into spans of at most ~8 seconds at breath points.

Content rules:
- For spoken dialogue, transcribe short phrases verbatim so they are quotable.
- For copyrighted songs, do not transcribe verses; give a 3-6 word identifying
  snippet plus a theme summary, and mark kind as lyric_summary.
- Note speaker changes; reuse consistent speaker labels across spans.
- Prefer 6-16 spans for a full video; more only if the video is dialogue-dense.
- Keep every text field concise and searchable.
"""


@dataclass(frozen=True)
class TranscribeSummary:
    assets_requested: int
    assets_completed: int
    spans_created: int


def transcribe_project(
    project: Project,
    *,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    limit: int | None = None,
    force: bool = False,
) -> TranscribeSummary:
    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = _assets_with_video_ref(conn, limit=limit, source=provider_name, force=force)
        units_by_asset = {
            str(asset["id"]): resolve_video_units(conn, str(asset["id"]), Path(str(asset["video_ref"])), None)
            for asset in assets
        }

    completed = 0
    spans_created = 0
    for asset in assets:
        asset_id = str(asset["id"])
        units = units_by_asset[asset_id]
        if len(units) == 1:
            # unchunked path: unchanged from before chunking existed
            video_path = Path(str(asset["video_ref"]))
            payload = provider.generate_video_json(video_path, TRANSCRIPT_PROMPT)
            spans = payload.get("spans") or []
            if not isinstance(spans, list):
                spans = []
        else:
            spans = []
            for unit in units:
                payload = provider.generate_video_json(unit.path, TRANSCRIPT_PROMPT)
                unit_spans = payload.get("spans") or []
                if not isinstance(unit_spans, list):
                    unit_spans = []
                spans.extend(remap_flat_items(unit_spans, unit))
        spans_created += _store_spans(project, asset_id, spans, provider_name)
        completed += 1

    return TranscribeSummary(
        assets_requested=len(assets),
        assets_completed=completed,
        spans_created=spans_created,
    )


def search_transcripts(project: Project, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select
                transcript_spans.id,
                transcript_spans.asset_id,
                assets.file_name,
                transcript_spans.start_sec,
                transcript_spans.end_sec,
                transcript_spans.kind,
                transcript_spans.speaker,
                transcript_spans.text,
                bm25(transcript_spans_fts) as rank
            from transcript_spans_fts
            join transcript_spans on transcript_spans.id = transcript_spans_fts.span_id
            join assets on assets.id = transcript_spans.asset_id
            where transcript_spans_fts match ?
            order by rank
            limit ?
            """,
            (_fts_query(query), limit),
        ).fetchall()
    return [dict(row) for row in rows]


def transcript_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select kind, count(*) as count
            from transcript_spans
            where project_id = ?
            group by kind
            order by kind
            """,
            ("default",),
        ).fetchall()
        total = conn.execute(
            "select count(*) as count from transcript_spans where project_id = ?",
            ("default",),
        ).fetchone()
    return {"spans": total["count"], "by_kind": {row["kind"]: row["count"] for row in rows}}


def _assets_with_video_ref(conn, *, limit: int | None, source: str, force: bool) -> list[dict[str, Any]]:
    query = """
        select
            assets.id,
            assets.path,
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
                from transcript_spans
                where transcript_spans.asset_id = assets.id
                  and transcript_spans.source = ?
            )
        """
        params.append(source)
    query += " order by assets.path"
    if limit is not None:
        query += " limit ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _store_spans(project: Project, asset_id: str, spans: list[Any], source: str) -> int:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute("delete from transcript_spans_fts where span_id in (select id from transcript_spans where asset_id = ?)", (asset_id,))
        conn.execute("delete from transcript_spans where asset_id = ? and source = ?", (asset_id, source))
        created = 0
        for raw in spans:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            start = _float(raw.get("start_sec"), 0.0)
            end = max(start + 0.1, _float(raw.get("end_sec"), start + 1.0))
            span_id = f"span_{uuid.uuid4().hex[:16]}"
            conn.execute(
                """
                insert into transcript_spans (
                    id, project_id, asset_id, start_sec, end_sec, speaker,
                    text, kind, confidence, source, source_run_id, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    span_id,
                    "default",
                    asset_id,
                    start,
                    end,
                    raw.get("speaker"),
                    text,
                    str(raw.get("kind") or "summary"),
                    _float(raw.get("confidence"), None),
                    source,
                    None,
                    utc_now(),
                ),
            )
            conn.execute(
                "insert into transcript_spans_fts (span_id, asset_id, text) values (?, ?, ?)",
                (span_id, asset_id, text),
            )
            created += 1
    return created


def _float(value: Any, default: float | None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fts_query(query: str) -> str:
    terms = [term.replace('"', "") for term in query.split() if term.strip()]
    return " OR ".join(f'"{term}"' for term in terms) or '""'
