from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .project import Project, utc_now


@dataclass(frozen=True)
class RenderSummary:
    render_id: str
    timeline_id: str
    path: str
    status: str
    clips_rendered: int


def render_timeline(project: Project, *, timeline_id: str = "latest") -> RenderSummary:
    render_id = f"render_{uuid.uuid4().hex[:16]}"
    timeline_row, items = _load_timeline_items(project, timeline_id)
    resolved_timeline_id = str(timeline_row["id"])
    output_path = project.root / "renders" / f"{render_id}.mp4"
    temp_dir = project.root / "renders" / f"{render_id}_clips"
    temp_dir.mkdir(parents=True, exist_ok=True)

    commands: list[list[str]] = []
    status = "complete"
    try:
        clip_paths: list[Path] = []
        for index, item in enumerate(items):
            clip_path = temp_dir / f"clip_{index:04d}.mp4"
            command = _clip_command(item, clip_path)
            _run(command)
            commands.append(command)
            clip_paths.append(clip_path)

        concat_file = temp_dir / "concat.txt"
        concat_file.write_text(
            "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
            encoding="utf-8",
        )
        concat_command = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_path),
        ]
        _run(concat_command)
        commands.append(concat_command)
    except Exception:
        status = "failed"
        raise
    finally:
        _store_render(
            project,
            render_id=render_id,
            timeline_id=resolved_timeline_id,
            output_path=output_path,
            status=status,
            commands=commands,
            duration_sec=_probe_duration(output_path) if output_path.exists() else None,
        )
        shutil.rmtree(temp_dir, ignore_errors=True)

    return RenderSummary(
        render_id=render_id,
        timeline_id=resolved_timeline_id,
        path=str(output_path),
        status=status,
        clips_rendered=len(items),
    )


def render_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select id, timeline_id, path, status, duration_sec, created_at
            from renders
            where project_id = ?
            order by created_at desc
            """,
            ("default",),
        ).fetchall()
    return {"renders": [dict(row) for row in rows]}


def _load_timeline_items(project: Project, timeline_id: str):
    with connect_db(project.db_path) as conn:
        migrate(conn)
        if timeline_id == "latest":
            timeline = conn.execute(
                "select id from timelines where project_id = ? order by created_at desc limit 1",
                ("default",),
            ).fetchone()
        else:
            timeline = conn.execute(
                "select id from timelines where project_id = ? and id = ?",
                ("default", timeline_id),
            ).fetchone()
        if timeline is None:
            raise FileNotFoundError("No timeline found to render")

        items = conn.execute(
            """
            select
                timeline_items.*,
                assets.path as asset_path,
                assets.duration_sec as asset_duration_sec
            from timeline_items
            join assets on assets.id = timeline_items.asset_id
            where timeline_items.project_id = ? and timeline_items.timeline_id = ?
            order by timeline_items.timeline_start_sec
            """,
            ("default", timeline["id"]),
        ).fetchall()

    if not items:
        raise ValueError("Timeline has no items")
    return timeline, [dict(row) for row in items]


def _clip_command(item: dict[str, Any], output_path: Path) -> list[str]:
    start = float(item["source_start_sec"])
    end = float(item["source_end_sec"])
    asset_duration = item.get("asset_duration_sec")
    if asset_duration is not None:
        asset_duration = float(asset_duration)
        start = min(start, max(0.0, asset_duration - 0.1))
        end = min(max(start + 0.1, end), asset_duration)
    duration = max(0.1, end - start)
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(item["asset_path"]),
        "-vf",
        "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
        "-af",
        "aresample=48000, loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "24",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or "ffmpeg command failed"
        raise RuntimeError(error)


def _probe_duration(path: Path) -> float | None:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _store_render(
    project: Project,
    *,
    render_id: str,
    timeline_id: str,
    output_path: Path,
    status: str,
    commands: list[list[str]],
    duration_sec: float | None,
) -> None:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into renders (
                id, project_id, timeline_id, path, status, command_json,
                duration_sec, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                render_id,
                "default",
                timeline_id,
                str(output_path),
                status,
                json.dumps(commands),
                duration_sec,
                utc_now(),
            ),
        )
