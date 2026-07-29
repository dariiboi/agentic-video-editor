from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .project import Project, utc_now


PROXY_ARTIFACT_TYPE = "proxy"
DEFAULT_MAX_HEIGHT = 480
DEFAULT_VIDEO_BITRATE = "1500k"
DEFAULT_AUDIO_BITRATE = "128k"
DEFAULT_MIN_SIZE_MB = 500.0
DEFAULT_MIN_DURATION_SEC = 120.0


@dataclass(frozen=True)
class ProxyResult:
    status: str  # "ok" | "failed" | "skipped_no_video"
    output_path: Path | None
    size_bytes: int | None
    duration_sec: float | None
    error: str | None


@dataclass(frozen=True)
class ProxySummary:
    assets_requested: int
    assets_completed: int
    assets_failed: int
    bytes_before: int
    bytes_after: int


def generate_proxy(
    source_path: Path,
    output_path: Path,
    *,
    max_height: int = DEFAULT_MAX_HEIGHT,
    video_bitrate: str = DEFAULT_VIDEO_BITRATE,
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE,
) -> ProxyResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # never upscale a source already smaller than max_height; -2 keeps width even for yuv420p
    scale = f"scale=-2:'min({max_height},ih)'"
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-vf",
        scale,
        "-c:v",
        "libx264",
        "-b:v",
        video_bitrate,
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        str(output_path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, check=False, text=True)
    except FileNotFoundError:
        return ProxyResult(status="failed", output_path=None, size_bytes=None, duration_sec=None, error="ffmpeg executable was not found")

    if completed.returncode != 0 or not output_path.exists():
        error = completed.stderr.strip() or completed.stdout.strip() or "ffmpeg failed"
        return ProxyResult(status="failed", output_path=None, size_bytes=None, duration_sec=None, error=error)

    from .media import probe_media

    probe = probe_media(output_path)
    duration = probe.metadata.get("duration_sec") if probe.status == "ok" else None
    return ProxyResult(
        status="ok",
        output_path=output_path,
        size_bytes=output_path.stat().st_size,
        duration_sec=duration,
        error=None,
    )


def generate_proxies_for_project(
    project: Project,
    *,
    max_height: int = DEFAULT_MAX_HEIGHT,
    video_bitrate: str = DEFAULT_VIDEO_BITRATE,
    audio_bitrate: str = DEFAULT_AUDIO_BITRATE,
    min_size_mb: float = DEFAULT_MIN_SIZE_MB,
    min_duration_sec: float = DEFAULT_MIN_DURATION_SEC,
    limit: int | None = None,
    force: bool = False,
    path_contains: str | None = None,
) -> ProxySummary:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        # Compute eligibility/pending over ALL qualifying assets before
        # --limit is applied (see facets.py commit 17b1bdf): otherwise a
        # limit smaller than the eligible count re-selects the same
        # already-proxied assets on every resumed run and never advances.
        eligible = _eligible_assets(
            conn, min_size_mb=min_size_mb, min_duration_sec=min_duration_sec, path_contains=path_contains
        )
        proxied_ids = _already_proxied_asset_ids(conn)

    if force:
        pending = eligible
    else:
        pending = [asset for asset in eligible if asset["id"] not in proxied_ids]
    if limit is not None:
        pending = pending[:limit]

    proxies_dir = project.root / "media" / "proxies"
    completed = 0
    failed = 0
    bytes_before = 0
    bytes_after = 0
    for asset in pending:
        source_path = Path(str(asset["path"]))
        output_path = proxies_dir / f"{asset['id']}_proxy.mp4"
        result = generate_proxy(
            source_path,
            output_path,
            max_height=max_height,
            video_bitrate=video_bitrate,
            audio_bitrate=audio_bitrate,
        )
        if result.status != "ok":
            failed += 1
            continue
        _store_proxy_row(project, str(asset["id"]), result)
        completed += 1
        bytes_before += int(asset.get("size_bytes") or 0)
        bytes_after += int(result.size_bytes or 0)

    return ProxySummary(
        assets_requested=len(pending),
        assets_completed=completed,
        assets_failed=failed,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
    )


def _eligible_assets(
    conn, *, min_size_mb: float, min_duration_sec: float, path_contains: str | None = None
) -> list[dict[str, Any]]:
    min_size_bytes = min_size_mb * 1024 * 1024
    query = """
        select id, path, size_bytes, duration_sec
        from assets
        where project_id = ?
          and ingest_status = ?
          and (coalesce(size_bytes, 0) >= ? or coalesce(duration_sec, 0) >= ?)
    """
    params: list[Any] = ["default", "ready", min_size_bytes, min_duration_sec]
    if path_contains:
        query += " and path like ?"
        params.append(f"%{path_contains}%")
    query += " order by path"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _already_proxied_asset_ids(conn) -> set[str]:
    rows = conn.execute(
        "select distinct asset_id from media_artifacts where artifact_type = ?",
        (PROXY_ARTIFACT_TYPE,),
    ).fetchall()
    return {str(row["asset_id"]) for row in rows}


def _store_proxy_row(project: Project, asset_id: str, result: ProxyResult) -> None:
    now = utc_now()
    metadata = json.dumps(
        {"size_bytes": result.size_bytes, "duration_sec": result.duration_sec},
        sort_keys=True,
    )
    with connect_db(project.db_path) as conn:
        migrate(conn)
        # Exactly one proxy row per asset: the plain LEFT JOIN in
        # transcript.py/semantics.py/facets.py's video_ref queries has no
        # ORDER BY/LIMIT, so a second row for the same asset would fan out
        # those joins into duplicate asset rows.
        conn.execute(
            "delete from media_artifacts where asset_id = ? and artifact_type = ?",
            (asset_id, PROXY_ARTIFACT_TYPE),
        )
        conn.execute(
            """
            insert into media_artifacts (id, project_id, asset_id, artifact_type, path, metadata_json, created_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"proxy_{uuid.uuid4().hex[:16]}",
                "default",
                asset_id,
                PROXY_ARTIFACT_TYPE,
                str(result.output_path),
                metadata,
                now,
            ),
        )
        conn.commit()


def proxy_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        row = conn.execute(
            "select count(*) as n from media_artifacts where artifact_type = ?",
            (PROXY_ARTIFACT_TYPE,),
        ).fetchone()
    return {"proxies": int(row["n"] or 0)}
