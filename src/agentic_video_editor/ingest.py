from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .db import connect_db, migrate
from .media import discover_media, probe_media, sha256_file, stable_asset_id
from .project import Project, utc_now


@dataclass(frozen=True)
class IngestSummary:
    run_id: str
    files_found: int
    assets_created: int
    assets_updated: int
    assets_unchanged: int


def ingest_paths(project: Project, paths: list[Path]) -> IngestSummary:
    files = discover_media(paths)
    run_id = f"ingest_{uuid.uuid4().hex[:16]}"
    source_paths_json = json.dumps([str(p.expanduser().resolve()) for p in paths])
    now = utc_now()

    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into ingest_runs
                (id, project_id, source_paths_json, status, files_found, started_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            (run_id, "default", source_paths_json, "running", len(files), now),
        )

        counts = {"created": 0, "updated": 0, "unchanged": 0}
        try:
            for media_path in files:
                outcome, asset_id = _ingest_one(conn, media_path)
                counts[outcome] += 1
                _event(
                    conn,
                    run_id=run_id,
                    asset_id=asset_id,
                    event_type=f"asset_{outcome}",
                    message=f"{outcome.title()} {media_path.name}",
                    payload={"path": str(media_path)},
                )

            conn.execute(
                """
                update ingest_runs set
                    status = ?,
                    assets_created = ?,
                    assets_updated = ?,
                    assets_unchanged = ?,
                    finished_at = ?
                where id = ?
                """,
                (
                    "complete",
                    counts["created"],
                    counts["updated"],
                    counts["unchanged"],
                    utc_now(),
                    run_id,
                ),
            )
        except Exception as exc:
            conn.execute(
                "update ingest_runs set status = ?, error = ?, finished_at = ? where id = ?",
                ("failed", str(exc), utc_now(), run_id),
            )
            raise

    return IngestSummary(
        run_id=run_id,
        files_found=len(files),
        assets_created=counts["created"],
        assets_updated=counts["updated"],
        assets_unchanged=counts["unchanged"],
    )


def list_assets(project: Project) -> list[dict[str, object]]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select
                id, path, file_name, extension, size_bytes, duration_sec, fps,
                width, height, video_codec, audio_codec, has_audio,
                ingest_status, probe_status, updated_at
            from assets
            where project_id = ?
            order by path
            """,
            ("default",),
        ).fetchall()

    return [_asset_row_to_dict(row) for row in rows]


def _ingest_one(conn: sqlite3.Connection, media_path: Path) -> tuple[str, str]:
    stat = media_path.stat()
    path = str(media_path)
    existing = conn.execute(
        """
        select id, size_bytes, mtime_ns, sha256
        from assets
        where project_id = ? and path = ?
        """,
        ("default", path),
    ).fetchone()

    if existing and existing["size_bytes"] == stat.st_size and existing["mtime_ns"] == stat.st_mtime_ns:
        return "unchanged", str(existing["id"])

    digest = sha256_file(media_path)
    if existing:
        outcome = "updated"
    else:
        outcome = "created"

    probe = probe_media(media_path)
    asset_id = str(existing["id"]) if existing else stable_asset_id(media_path)
    now = utc_now()
    metadata = probe.metadata
    ingest_status = "ready" if probe.status == "ok" else "probe_failed"

    conn.execute(
        """
        insert into assets (
            id, project_id, path, file_name, extension, size_bytes, mtime_ns,
            sha256, duration_sec, fps, width, height, video_codec, audio_codec,
            has_audio, created_at_source, ingest_status, probe_status,
            probe_error, probe_json, first_seen_at, updated_at
        )
        values (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        on conflict(project_id, path) do update set
            file_name = excluded.file_name,
            extension = excluded.extension,
            size_bytes = excluded.size_bytes,
            mtime_ns = excluded.mtime_ns,
            sha256 = excluded.sha256,
            duration_sec = excluded.duration_sec,
            fps = excluded.fps,
            width = excluded.width,
            height = excluded.height,
            video_codec = excluded.video_codec,
            audio_codec = excluded.audio_codec,
            has_audio = excluded.has_audio,
            created_at_source = excluded.created_at_source,
            ingest_status = excluded.ingest_status,
            probe_status = excluded.probe_status,
            probe_error = excluded.probe_error,
            probe_json = excluded.probe_json,
            updated_at = excluded.updated_at
        """,
        (
            asset_id,
            "default",
            path,
            media_path.name,
            media_path.suffix.lower(),
            stat.st_size,
            stat.st_mtime_ns,
            digest,
            metadata.get("duration_sec"),
            metadata.get("fps"),
            metadata.get("width"),
            metadata.get("height"),
            metadata.get("video_codec"),
            metadata.get("audio_codec"),
            1 if metadata.get("has_audio") else 0,
            metadata.get("created_at_source"),
            ingest_status,
            probe.status,
            probe.error,
            json.dumps(probe.raw_json) if probe.raw_json is not None else None,
            now,
            now,
        ),
    )
    return outcome, asset_id


def _event(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    asset_id: str | None,
    event_type: str,
    message: str,
    payload: dict[str, object] | None = None,
) -> None:
    conn.execute(
        """
        insert into run_events
            (id, project_id, run_id, asset_id, agent, event_type, message, payload_json, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"event_{uuid.uuid4().hex[:16]}",
            "default",
            run_id,
            asset_id,
            "IngestAgent",
            event_type,
            message,
            json.dumps(payload or {}, sort_keys=True),
            utc_now(),
        ),
    )


def _asset_row_to_dict(row: sqlite3.Row) -> dict[str, object]:
    resolution = None
    if row["width"] is not None and row["height"] is not None:
        resolution = f"{row['width']}x{row['height']}"

    return {
        "id": row["id"],
        "path": row["path"],
        "file_name": row["file_name"],
        "extension": row["extension"],
        "size_bytes": row["size_bytes"],
        "duration_sec": row["duration_sec"],
        "duration_seconds": row["duration_sec"],
        "fps": row["fps"],
        "width": row["width"],
        "height": row["height"],
        "resolution": resolution,
        "codec": row["video_codec"] or row["audio_codec"],
        "video_codec": row["video_codec"],
        "audio_codec": row["audio_codec"],
        "has_audio": bool(row["has_audio"]),
        "audio": "present" if row["has_audio"] else "none",
        "ingest_status": row["ingest_status"],
        "probe_status": row["probe_status"],
        "updated_at": row["updated_at"],
    }
