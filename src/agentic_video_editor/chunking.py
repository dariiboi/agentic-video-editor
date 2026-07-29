from __future__ import annotations

import json
import math
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .project import Project, utc_now


CHUNK_ARTIFACT_TYPE = "chunk"
DEFAULT_CHUNK_LENGTH_SEC = 900.0
DEFAULT_MIN_ASSET_SEC = 1200.0
# A trailing chunk shorter than this fraction of a full chunk is folded into
# the previous one instead of shipping a near-empty final upload.
MIN_TAIL_FRACTION = 0.15


@dataclass(frozen=True)
class ChunkSpec:
    index: int
    start_sec: float
    end_sec: float

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_sec - self.start_sec)


@dataclass(frozen=True)
class VideoUnit:
    """One file to upload for a pass, and the offset to re-base its timestamps.

    Every raw start_sec/end_sec Gemini returns for this unit is relative to
    THIS file, not the source asset - offset_sec must be added (after
    clamping to [0, duration_sec]) before storage.
    """

    path: Path
    index: int
    offset_sec: float
    duration_sec: float | None


def chunk_plan(
    duration_sec: float | None,
    *,
    chunk_length_sec: float = DEFAULT_CHUNK_LENGTH_SEC,
    min_asset_sec: float = DEFAULT_MIN_ASSET_SEC,
) -> list[ChunkSpec]:
    """Plan contiguous, non-overlapping chunks for one asset.

    Assets at or under min_asset_sec get a single implicit chunk covering the
    whole file (the no-op path most assets take). No overlap: Gemini already
    sees each chunk's boundaries clearly, and overlap would require de-duping
    duplicate evidence downstream for no real benefit here.
    """
    duration = float(duration_sec or 0.0)
    if duration <= 0 or duration <= min_asset_sec or chunk_length_sec <= 0:
        return [ChunkSpec(index=0, start_sec=0.0, end_sec=max(0.0, duration))]

    count = math.ceil(duration / chunk_length_sec)
    specs = [
        ChunkSpec(
            index=i,
            start_sec=i * chunk_length_sec,
            end_sec=min(duration, (i + 1) * chunk_length_sec),
        )
        for i in range(count)
    ]
    # A near-zero trailing chunk (duration not a clean multiple of
    # chunk_length_sec) is folded into the previous chunk rather than
    # shipped as its own tiny upload.
    if len(specs) >= 2 and specs[-1].duration_sec < chunk_length_sec * MIN_TAIL_FRACTION:
        merged = ChunkSpec(index=specs[-2].index, start_sec=specs[-2].start_sec, end_sec=specs[-1].end_sec)
        specs = specs[:-2] + [merged]
    return specs


def generate_chunks(source_path: Path, plan: list[ChunkSpec], output_dir: Path) -> list[Path]:
    """Extract each planned chunk as its own file, re-encoding for frame-accurate boundaries.

    Stream copy (-c copy) can only cut at keyframes, so a copied chunk's real
    start can land noticeably before spec.start_sec - which would silently
    poison the offset math (every timestamp Gemini returns would be re-based
    from the wrong zero point). Re-encoding lets ffmpeg seek+decode to the
    exact requested boundary, so offset_sec == spec.start_sec is trustworthy.
    Chunks are meant to run against the (already downscaled) proxy when one
    exists, keeping this re-encode cheap.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    if len(plan) == 1:
        # No real chunking: the single spec covers the whole file, so the
        # source itself IS the unit - no extraction needed.
        return [source_path]
    paths: list[Path] = []
    for spec in plan:
        out_path = output_dir / f"chunk_{spec.index:04d}.mp4"
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(spec.start_sec),
            "-i",
            str(source_path),
            "-t",
            str(spec.duration_sec),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            str(out_path),
        ]
        completed = subprocess.run(command, capture_output=True, check=False, text=True)
        if completed.returncode != 0 or not out_path.exists():
            error = completed.stderr.strip() or completed.stdout.strip() or "ffmpeg failed"
            raise RuntimeError(f"chunk {spec.index} extraction failed: {error}")
        paths.append(out_path)
    return paths


def generate_chunks_for_project(
    project: Project,
    *,
    chunk_length_sec: float = DEFAULT_CHUNK_LENGTH_SEC,
    min_asset_sec: float = DEFAULT_MIN_ASSET_SEC,
    limit: int | None = None,
    force: bool = False,
    path_contains: str | None = None,
) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        # Same lesson as facets.py (commit 17b1bdf) and proxy.py: compute
        # eligibility over ALL qualifying assets before --limit slices it, so
        # repeated bounded runs advance through the backlog instead of
        # re-selecting the same already-chunked assets forever.
        all_eligible = _eligible_assets(
            conn, chunk_length_sec=chunk_length_sec, min_asset_sec=min_asset_sec, path_contains=path_contains
        )
        chunked_ids = _already_chunked_asset_ids(conn)

    pending = all_eligible if force else [asset for asset in all_eligible if asset["id"] not in chunked_ids]
    if limit is not None:
        pending = pending[:limit]

    chunks_dir_root = project.root / "media" / "chunks"
    completed = 0
    failed = 0
    chunks_created = 0
    for asset in pending:
        asset_id = str(asset["id"])
        source_path = Path(str(asset["video_ref"]))
        plan = chunk_plan(asset.get("duration_sec"), chunk_length_sec=chunk_length_sec, min_asset_sec=min_asset_sec)
        if len(plan) <= 1:
            continue
        try:
            paths = generate_chunks(source_path, plan, chunks_dir_root / asset_id)
        except RuntimeError:
            failed += 1
            continue
        _store_chunk_rows(project, asset_id, plan, paths)
        completed += 1
        chunks_created += len(plan)

    return {
        "assets_requested": len(pending),
        "assets_completed": completed,
        "assets_failed": failed,
        "chunks_created": chunks_created,
    }


def resolve_video_units(conn, asset_id: str, video_ref: Path, asset_duration_sec: float | None) -> list[VideoUnit]:
    """The file(s) a pass should upload for this asset, with offsets to re-base timestamps.

    An asset with no chunk rows returns a single unit at offset 0 - this is
    the common, unchanged path every existing pass already takes.
    """
    rows = conn.execute(
        """
        select chunk_index, start_sec, end_sec, path
        from media_artifacts
        where asset_id = ? and artifact_type = ?
        order by chunk_index
        """,
        (asset_id, CHUNK_ARTIFACT_TYPE),
    ).fetchall()
    if not rows:
        return [VideoUnit(path=video_ref, index=0, offset_sec=0.0, duration_sec=asset_duration_sec)]
    return [
        VideoUnit(
            path=Path(str(row["path"])),
            index=int(row["chunk_index"]),
            offset_sec=float(row["start_sec"]),
            duration_sec=max(0.0, float(row["end_sec"]) - float(row["start_sec"])),
        )
        for row in rows
    ]


def remap_flat_items(items: list[dict[str, Any]], unit: VideoUnit) -> list[dict[str, Any]]:
    """Re-base a flat list of {start_sec, end_sec, ...} dicts onto the asset's absolute timeline."""
    ceiling = unit.duration_sec if unit.duration_sec and unit.duration_sec > 0 else None
    remapped = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        start = _safe_float(item.get("start_sec"), 0.0)
        end = _safe_float(item.get("end_sec"), start + 1.0)
        if ceiling is not None:
            start = min(max(0.0, start), ceiling)
            end = min(max(start + 0.05, end), ceiling)
        else:
            start = max(0.0, start)
            end = max(start + 0.05, end)
        item["start_sec"] = start + unit.offset_sec
        item["end_sec"] = end + unit.offset_sec
        remapped.append(item)
    return remapped


def remap_semantic_payload(
    payload: dict[str, Any], unit: VideoUnit, index_offset: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Re-base a semantics-pass payload's segments (incl. nested word_units) and
    relationships (which reference from_index/to_index local to this payload's
    own segment list) onto the asset's absolute timeline and a running index."""
    raw_segments = payload.get("segments") or []
    raw_relationships = payload.get("relationships") or []
    if not isinstance(raw_segments, list):
        raw_segments = []
    if not isinstance(raw_relationships, list):
        raw_relationships = []

    segments = remap_flat_items(raw_segments, unit)
    for segment, raw in zip(segments, raw_segments):
        word_units = raw.get("word_units") if isinstance(raw, dict) else None
        if isinstance(word_units, list):
            segment["word_units"] = remap_flat_items(word_units, unit)

    relationships = []
    for raw in raw_relationships:
        if not isinstance(raw, dict):
            continue
        rel = dict(raw)
        from_index = _safe_int(rel.get("from_index"))
        to_index = _safe_int(rel.get("to_index"))
        if from_index is None or to_index is None:
            continue
        if not (0 <= from_index < len(raw_segments)) or not (0 <= to_index < len(raw_segments)):
            continue
        rel["from_index"] = from_index + index_offset
        rel["to_index"] = to_index + index_offset
        relationships.append(rel)

    return segments, relationships, index_offset + len(segments)


def _eligible_assets(
    conn, *, chunk_length_sec: float, min_asset_sec: float, path_contains: str | None = None
) -> list[dict[str, Any]]:
    query = """
        select
            assets.id,
            assets.duration_sec,
            coalesce(proxy.path, assets.path) as video_ref
        from assets
        left join media_artifacts proxy
            on proxy.asset_id = assets.id and proxy.artifact_type = 'proxy'
        where assets.project_id = ?
          and assets.ingest_status = ?
          and coalesce(assets.duration_sec, 0) > ?
    """
    params: list[Any] = ["default", "ready", min_asset_sec]
    if path_contains:
        query += " and assets.path like ?"
        params.append(f"%{path_contains}%")
    query += " order by assets.path"
    rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _already_chunked_asset_ids(conn) -> set[str]:
    rows = conn.execute(
        "select distinct asset_id from media_artifacts where artifact_type = ?",
        (CHUNK_ARTIFACT_TYPE,),
    ).fetchall()
    return {str(row["asset_id"]) for row in rows}


def _store_chunk_rows(project: Project, asset_id: str, plan: list[ChunkSpec], paths: list[Path]) -> None:
    now = utc_now()
    with connect_db(project.db_path) as conn:
        migrate(conn)
        # Exactly one set of chunk rows per asset, mirroring proxy.py's
        # single-row-per-asset guarantee for the same reason: repeated joins
        # (chunk_index order by) must not fan out on a re-run.
        conn.execute(
            "delete from media_artifacts where asset_id = ? and artifact_type = ?",
            (asset_id, CHUNK_ARTIFACT_TYPE),
        )
        for spec, path in zip(plan, paths):
            conn.execute(
                """
                insert into media_artifacts (
                    id, project_id, asset_id, artifact_type, path, metadata_json,
                    created_at, chunk_index, start_sec, end_sec
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"chunk_{uuid.uuid4().hex[:16]}",
                    "default",
                    asset_id,
                    CHUNK_ARTIFACT_TYPE,
                    str(path),
                    json.dumps({"duration_sec": spec.duration_sec}),
                    now,
                    spec.index,
                    spec.start_sec,
                    spec.end_sec,
                ),
            )
        conn.commit()


def chunk_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        row = conn.execute(
            "select count(distinct asset_id) as assets, count(*) as chunks from media_artifacts where artifact_type = ?",
            (CHUNK_ARTIFACT_TYPE,),
        ).fetchone()
    return {"chunked_assets": int(row["assets"] or 0), "chunks": int(row["chunks"] or 0)}


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
