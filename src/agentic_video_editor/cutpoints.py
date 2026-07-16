from __future__ import annotations

import json
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .project import Project, utc_now


SCENE_DETECTOR = "ffmpeg_scdet"
AUDIO_GAP_DETECTOR = "audio_gap"

SCENE_TIME_RE = re.compile(r"pts_time:(?P<time>[0-9.]+)")
SCENE_SCORE_RE = re.compile(r"lavfi\.scene_score=(?P<score>[0-9.]+)")
SILENCE_START_RE = re.compile(r"silence_start:\s*(?P<time>[0-9.]+)")
SILENCE_END_RE = re.compile(r"silence_end:\s*(?P<time>[0-9.]+)")


@dataclass(frozen=True)
class CutPointSummary:
    assets_requested: int
    assets_completed: int
    scene_points: int
    audio_gap_points: int


def detect_cut_points(
    project: Project,
    *,
    scene_threshold: float = 0.3,
    gap_noise_db: float = -30.0,
    gap_min_sec: float = 0.12,
    limit: int | None = None,
) -> CutPointSummary:
    """Populate scene_boundaries with frame-accurate snap targets.

    Two detectors run per asset: video shot changes (ffmpeg scene score) and
    short audio gaps (pauses between phrases). Both produce timestamps that
    later snap the approximate LLM trim points onto clean cuts.
    """
    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = _ready_assets(conn, limit=limit)

    totals = {"scene": 0, "gaps": 0, "completed": 0}
    for asset in assets:
        source = Path(str(asset["path"]))
        duration = float(asset["duration_sec"] or 0.0)
        if duration <= 0 or not source.exists():
            continue
        scene_points = _detect_scene_changes(source, scene_threshold)
        gap_points = (
            _detect_audio_gaps(source, duration, gap_noise_db, gap_min_sec)
            if asset["has_audio"]
            else []
        )
        _store_points(project, str(asset["id"]), scene_points, gap_points, scene_threshold, gap_noise_db, gap_min_sec)
        totals["scene"] += len(scene_points)
        totals["gaps"] += len(gap_points)
        totals["completed"] += 1

    return CutPointSummary(
        assets_requested=len(assets),
        assets_completed=totals["completed"],
        scene_points=totals["scene"],
        audio_gap_points=totals["gaps"],
    )


def cut_point_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select detector, count(*) as count
            from scene_boundaries
            where project_id = ?
            group by detector
            order by detector
            """,
            ("default",),
        ).fetchall()
    return {"cut_points": {row["detector"]: row["count"] for row in rows}}


def load_cut_points(conn, asset_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select time_sec, detector, confidence, reason
        from scene_boundaries
        where asset_id = ?
        order by time_sec
        """,
        (asset_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def snap_time(
    cut_points: list[dict[str, Any]],
    target_sec: float,
    *,
    tolerance_sec: float,
) -> tuple[float, dict[str, Any] | None]:
    """Return the nearest cut point within tolerance, else the target unchanged."""
    if tolerance_sec <= 0 or not cut_points:
        return target_sec, None
    best: dict[str, Any] | None = None
    best_distance = tolerance_sec
    for point in cut_points:
        distance = abs(float(point["time_sec"]) - target_sec)
        if distance <= best_distance:
            best = point
            best_distance = distance
    if best is None:
        return target_sec, None
    return float(best["time_sec"]), best


def snap_range(
    cut_points: list[dict[str, Any]],
    start_sec: float,
    end_sec: float,
    *,
    tolerance_sec: float = 1.0,
    min_duration_sec: float = 0.5,
    margin_sec: float = 0.15,
) -> dict[str, Any]:
    """Snap a source in/out range onto detected cut points.

    The in-point prefers an audio-gap end (start of speech/music) or a shot
    change; the out-point prefers an audio-gap start (phrase just finished).
    Audio-gap snaps keep a small margin inside the silence so word attacks
    and decays are never clipped; shot changes are cut exactly on the frame.
    Snaps that would collapse the clip below min_duration_sec are discarded.
    """
    in_candidates = [p for p in cut_points if p["reason"] not in ("gap_start", "word_end")]
    out_candidates = [p for p in cut_points if p["reason"] not in ("gap_end", "word_start")]

    snapped_start, start_point = snap_time(in_candidates or cut_points, start_sec, tolerance_sec=tolerance_sec)
    snapped_end, end_point = snap_time(out_candidates or cut_points, end_sec, tolerance_sec=tolerance_sec)

    if start_point and start_point.get("reason") in ("gap_end", "word_start"):
        snapped_start = max(0.0, snapped_start - margin_sec)
    if end_point and end_point.get("reason") in ("gap_start", "word_end"):
        snapped_end += margin_sec

    if snapped_end - snapped_start < min_duration_sec:
        snapped_start, start_point = start_sec, None
        snapped_end, end_point = end_sec, None

    return {
        "start_sec": round(snapped_start, 3),
        "end_sec": round(snapped_end, 3),
        "start_snapped_to": start_point["detector"] if start_point else None,
        "end_snapped_to": end_point["detector"] if end_point else None,
        "start_moved_sec": round(snapped_start - start_sec, 3),
        "end_moved_sec": round(snapped_end - end_sec, 3),
    }


def _ready_assets(conn, *, limit: int | None) -> list[dict[str, Any]]:
    query = """
        select id, path, duration_sec, has_audio
        from assets
        where project_id = ? and ingest_status = ?
        order by path
    """
    params: list[Any] = ["default", "ready"]
    if limit is not None:
        query += " limit ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _detect_scene_changes(source: Path, threshold: float) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(source),
            "-vf",
            f"select='gt(scene,{threshold})',metadata=print",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    points: list[dict[str, Any]] = []
    pending_time: float | None = None
    for line in output.splitlines():
        time_match = SCENE_TIME_RE.search(line)
        if time_match:
            pending_time = float(time_match.group("time"))
            continue
        score_match = SCENE_SCORE_RE.search(line)
        if score_match and pending_time is not None:
            points.append(
                {
                    "time_sec": pending_time,
                    "confidence": min(1.0, float(score_match.group("score"))),
                    "reason": "shot_change",
                }
            )
            pending_time = None
    return points


def _detect_audio_gaps(
    source: Path,
    duration: float,
    noise_db: float,
    min_gap_sec: float,
) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "info",
            "-i",
            str(source),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_gap_sec}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    output = f"{completed.stdout}\n{completed.stderr}"
    points: list[dict[str, Any]] = []
    for line in output.splitlines():
        start_match = SILENCE_START_RE.search(line)
        if start_match:
            time_sec = float(start_match.group("time"))
            if 0.0 < time_sec < duration:
                points.append({"time_sec": time_sec, "confidence": None, "reason": "gap_start"})
            continue
        end_match = SILENCE_END_RE.search(line)
        if end_match:
            time_sec = float(end_match.group("time"))
            if 0.0 < time_sec < duration:
                points.append({"time_sec": time_sec, "confidence": None, "reason": "gap_end"})
    return points


def _store_points(
    project: Project,
    asset_id: str,
    scene_points: list[dict[str, Any]],
    gap_points: list[dict[str, Any]],
    scene_threshold: float,
    gap_noise_db: float,
    gap_min_sec: float,
) -> None:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            "delete from scene_boundaries where asset_id = ? and detector in (?, ?)",
            (asset_id, SCENE_DETECTOR, AUDIO_GAP_DETECTOR),
        )
        for detector, points, params in [
            (SCENE_DETECTOR, scene_points, {"scene_threshold": scene_threshold}),
            (AUDIO_GAP_DETECTOR, gap_points, {"noise_db": gap_noise_db, "min_gap_sec": gap_min_sec}),
        ]:
            for point in points:
                conn.execute(
                    """
                    insert into scene_boundaries (
                        id, project_id, asset_id, time_sec, detector,
                        confidence, reason, params_json, created_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"cut_{uuid.uuid4().hex[:16]}",
                        "default",
                        asset_id,
                        point["time_sec"],
                        detector,
                        point.get("confidence"),
                        point.get("reason"),
                        json.dumps(params, sort_keys=True),
                        utc_now(),
                    ),
                )
