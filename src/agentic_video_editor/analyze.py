from __future__ import annotations

import json
import math
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .project import Project, utc_now


SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<time>[0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(?P<time>[0-9.]+)")


@dataclass(frozen=True)
class AnalyzeSummary:
    run_id: str
    assets_requested: int
    assets_completed: int
    windows_created: int
    labels_created: int
    artifacts_created: int


def analyze_project(
    project: Project,
    *,
    window_sec: float = 30.0,
    silence_noise_db: float = -35.0,
    silence_duration_sec: float = 0.5,
    limit: int | None = None,
) -> AnalyzeSummary:
    run_id = f"analysis_{uuid.uuid4().hex[:16]}"
    params = {
        "window_sec": window_sec,
        "silence_noise_db": silence_noise_db,
        "silence_duration_sec": silence_duration_sec,
        "limit": limit,
    }

    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = _load_assets(conn, limit=limit)
        conn.execute(
            """
            insert into analysis_runs
                (id, project_id, kind, status, params_json, assets_requested, started_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, "default", "local_windows", "running", json.dumps(params), len(assets), utc_now()),
        )

    totals = {"windows": 0, "labels": 0, "artifacts": 0, "completed": 0}
    try:
        for asset in assets:
            result = analyze_asset(
                project,
                asset,
                window_sec=window_sec,
                silence_noise_db=silence_noise_db,
                silence_duration_sec=silence_duration_sec,
            )
            totals["windows"] += result["windows"]
            totals["labels"] += result["labels"]
            totals["artifacts"] += result["artifacts"]
            totals["completed"] += 1

        with connect_db(project.db_path) as conn:
            conn.execute(
                """
                update analysis_runs set
                    status = ?,
                    assets_completed = ?,
                    finished_at = ?
                where id = ?
                """,
                ("complete", totals["completed"], utc_now(), run_id),
            )
    except Exception as exc:
        with connect_db(project.db_path) as conn:
            conn.execute(
                "update analysis_runs set status = ?, error = ?, finished_at = ? where id = ?",
                ("failed", str(exc), utc_now(), run_id),
            )
        raise

    return AnalyzeSummary(
        run_id=run_id,
        assets_requested=len(assets),
        assets_completed=totals["completed"],
        windows_created=totals["windows"],
        labels_created=totals["labels"],
        artifacts_created=totals["artifacts"],
    )


def analyze_asset(
    project: Project,
    asset: dict[str, Any],
    *,
    window_sec: float,
    silence_noise_db: float,
    silence_duration_sec: float,
) -> dict[str, int]:
    asset_id = str(asset["id"])
    source = Path(str(asset["path"]))
    duration = float(asset["duration_sec"] or 0.0)
    width = int(asset["width"] or 0)
    has_audio = bool(asset["has_audio"])
    if duration <= 0:
        return {"windows": 0, "labels": 0, "artifacts": 0}

    artifacts = _create_artifacts(project, asset_id, source, duration, width)
    silence_ranges = (
        _detect_silence(source, duration, silence_noise_db, silence_duration_sec) if has_audio else []
    )

    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute("delete from activity_labels where asset_id = ?", (asset_id,))
        conn.execute("delete from windows where asset_id = ?", (asset_id,))

        for artifact in artifacts:
            _upsert_artifact(conn, asset_id, artifact)

        windows_created = 0
        labels_created = 0
        for start in _window_starts(duration, window_sec):
            end = min(duration, start + window_sec)
            window_id = f"window_{uuid.uuid4().hex[:16]}"
            silence_ratio = _coverage_ratio(silence_ranges, start, end)
            active_ratio = 1.0 - silence_ratio if has_audio else None
            priority_score = active_ratio if active_ratio is not None else 0.0

            conn.execute(
                """
                insert into windows (
                    id, project_id, asset_id, start_sec, end_sec, duration_sec,
                    silence_ratio, audio_active_ratio, motion_score, quality_score,
                    priority_score, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    window_id,
                    "default",
                    asset_id,
                    start,
                    end,
                    end - start,
                    silence_ratio if has_audio else None,
                    active_ratio,
                    None,
                    None,
                    priority_score,
                    utc_now(),
                ),
            )
            windows_created += 1

            label_specs = _labels_for_window(
                start,
                end,
                window_id,
                has_audio=has_audio,
                silence_ratio=silence_ratio,
                active_ratio=active_ratio,
                silence_noise_db=silence_noise_db,
                silence_duration_sec=silence_duration_sec,
            )
            for label in label_specs:
                _insert_label(conn, asset_id, label)
                labels_created += 1

    return {"windows": windows_created, "labels": labels_created, "artifacts": len(artifacts)}


def summarize_local_analysis(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = conn.execute("select count(*) as count from assets where project_id = ?", ("default",)).fetchone()
        windows = conn.execute("select count(*) as count from windows where project_id = ?", ("default",)).fetchone()
        labels = conn.execute(
            """
            select label, count(*) as count
            from activity_labels
            where project_id = ?
            group by label
            order by label
            """,
            ("default",),
        ).fetchall()
        artifacts = conn.execute(
            """
            select artifact_type, count(*) as count
            from media_artifacts
            where project_id = ?
            group by artifact_type
            order by artifact_type
            """,
            ("default",),
        ).fetchall()

    return {
        "assets": assets["count"],
        "windows": windows["count"],
        "labels": {row["label"]: row["count"] for row in labels},
        "artifacts": {row["artifact_type"]: row["count"] for row in artifacts},
    }


def _load_assets(conn, *, limit: int | None) -> list[dict[str, Any]]:
    query = """
        select id, path, duration_sec, width, height, has_audio
        from assets
        where project_id = ? and ingest_status = ?
        order by path
    """
    params: list[Any] = ["default", "ready"]
    if limit is not None:
        query += " limit ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _create_artifacts(project: Project, asset_id: str, source: Path, duration: float, width: int) -> list[dict[str, Any]]:
    proxy_path = project.root / "media" / "proxies" / f"{asset_id}.mp4"
    thumbnail_path = project.root / "media" / "thumbnails" / f"{asset_id}.jpg"
    strip_path = project.root / "media" / "frame_strips" / f"{asset_id}.jpg"

    target_width = _even(max(160, min(width or 360, 360)))
    artifacts: list[dict[str, Any]] = []

    if _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vf",
            f"scale={target_width}:-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            str(proxy_path),
        ]
    ):
        artifacts.append(
            {
                "artifact_type": "proxy",
                "path": str(proxy_path),
                "metadata": {"target_width": target_width},
            }
        )

    midpoint = max(0.0, min(duration / 2, duration - 0.1))
    if _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{midpoint:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            "scale=320:-2",
            str(thumbnail_path),
        ]
    ):
        artifacts.append(
            {
                "artifact_type": "thumbnail",
                "path": str(thumbnail_path),
                "metadata": {"source_time_sec": midpoint},
            }
        )

    interval = max(1.0, duration / 25.0)
    if _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            f"fps=1/{interval:.3f},scale=160:-2,tile=5x5",
            str(strip_path),
        ]
    ):
        artifacts.append(
            {
                "artifact_type": "frame_strip",
                "path": str(strip_path),
                "metadata": {"sample_interval_sec": interval, "tile": "5x5"},
            }
        )

    return artifacts


def _detect_silence(
    source: Path,
    duration: float,
    silence_noise_db: float,
    silence_duration_sec: float,
) -> list[tuple[float, float]]:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(source),
            "-af",
            f"silencedetect=noise={silence_noise_db}dB:d={silence_duration_sec}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    ranges: list[tuple[float, float]] = []
    open_start: float | None = None

    for line in output.splitlines():
        start_match = SILENCE_START_RE.search(line)
        if start_match:
            open_start = float(start_match.group("time"))
            continue

        end_match = SILENCE_END_RE.search(line)
        if end_match and open_start is not None:
            ranges.append((max(0.0, open_start), min(duration, float(end_match.group("time")))))
            open_start = None

    if open_start is not None:
        ranges.append((open_start, duration))

    return [(start, end) for start, end in ranges if end > start]


def _window_starts(duration: float, window_sec: float) -> list[float]:
    if window_sec <= 0:
        raise ValueError("window_sec must be greater than zero")
    count = max(1, math.ceil(duration / window_sec))
    return [round(index * window_sec, 6) for index in range(count)]


def _coverage_ratio(ranges: list[tuple[float, float]], start: float, end: float) -> float:
    duration = end - start
    if duration <= 0:
        return 0.0
    covered = 0.0
    for range_start, range_end in ranges:
        overlap_start = max(start, range_start)
        overlap_end = min(end, range_end)
        if overlap_end > overlap_start:
            covered += overlap_end - overlap_start
    return round(min(1.0, max(0.0, covered / duration)), 4)


def _labels_for_window(
    start: float,
    end: float,
    window_id: str,
    *,
    has_audio: bool,
    silence_ratio: float,
    active_ratio: float | None,
    silence_noise_db: float,
    silence_duration_sec: float,
) -> list[dict[str, Any]]:
    if not has_audio:
        return [
            {
                "window_id": window_id,
                "start_sec": start,
                "end_sec": end,
                "label": "no_audio",
                "score": 1.0,
                "source": "ffprobe",
                "params": {},
                "suggested_action": "review",
            }
        ]

    params = {
        "silence_noise_db": silence_noise_db,
        "silence_duration_sec": silence_duration_sec,
    }
    labels: list[dict[str, Any]] = []
    if silence_ratio >= 0.8:
        labels.append(
            {
                "window_id": window_id,
                "start_sec": start,
                "end_sec": end,
                "label": "silence",
                "score": silence_ratio,
                "source": "ffmpeg_silencedetect",
                "params": params,
                "suggested_action": "cut_candidate",
            }
        )
    if active_ratio is not None and active_ratio >= 0.2:
        labels.append(
            {
                "window_id": window_id,
                "start_sec": start,
                "end_sec": end,
                "label": "audio_active",
                "score": active_ratio,
                "source": "ffmpeg_silencedetect",
                "params": params,
                "suggested_action": "keep_candidate",
            }
        )
    if active_ratio is not None and active_ratio >= 0.95:
        labels.append(
            {
                "window_id": window_id,
                "start_sec": start,
                "end_sec": end,
                "label": "high_energy",
                "score": active_ratio,
                "source": "ffmpeg_silencedetect",
                "params": params,
                "suggested_action": "review",
            }
        )
    return labels


def _insert_label(conn, asset_id: str, label: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into activity_labels (
            id, project_id, asset_id, window_id, start_sec, end_sec, label,
            score, source, params_json, suggested_action, created_at
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"label_{uuid.uuid4().hex[:16]}",
            "default",
            asset_id,
            label["window_id"],
            label["start_sec"],
            label["end_sec"],
            label["label"],
            label["score"],
            label["source"],
            json.dumps(label["params"], sort_keys=True),
            label["suggested_action"],
            utc_now(),
        ),
    )


def _upsert_artifact(conn, asset_id: str, artifact: dict[str, Any]) -> None:
    conn.execute(
        """
        insert into media_artifacts (
            id, project_id, asset_id, artifact_type, path, metadata_json, created_at
        )
        values (?, ?, ?, ?, ?, ?, ?)
        on conflict(asset_id, artifact_type, path) do update set
            metadata_json = excluded.metadata_json
        """,
        (
            f"artifact_{uuid.uuid4().hex[:16]}",
            "default",
            asset_id,
            artifact["artifact_type"],
            artifact["path"],
            json.dumps(artifact["metadata"], sort_keys=True),
            utc_now(),
        ),
    )


def _run_ffmpeg(command: list[str]) -> bool:
    completed = subprocess.run(command, capture_output=True, check=False, text=True)
    return completed.returncode == 0


def _even(value: int) -> int:
    return value if value % 2 == 0 else value - 1
