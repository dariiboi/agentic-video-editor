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


AUDIO_MICRO_FADE_SEC = 0.05

# A crossfade that clamps below this floor becomes a hard cut instead.
CROSSFADE_FLOOR_SEC = 0.08
# Un-faded content every clip must keep between its two join fades.
MIN_SOLO_SEC = 0.1
# Chained xfade+acrossfade graphs deadlock ffmpeg's scheduler when many short
# clips are almost entirely consumed by fades (measured: 0.5s clips fail from
# 4 chained joins even at 0.1s fades, while <=3 joins survive fades that leave
# 0.02s solo, and clips >=1.5s survive deep chains). Outside that proven-safe
# envelope the audio side is built flat (afade+adelay+amix) instead of chained.
LEGACY_CHAIN_MAX_JOINS = 3
LEGACY_CHAIN_MIN_CLIP_SEC = 1.5


@dataclass(frozen=True)
class RenderSummary:
    render_id: str
    timeline_id: str
    path: str
    status: str
    clips_rendered: int


def render_timeline(
    project: Project,
    *,
    timeline_id: str = "latest",
    crossfade_sec: float = 0.0,
    burn_captions: bool = False,
    normalize_loudness: bool = True,
    loudness_range: float = 11.0,
) -> RenderSummary:
    render_id = f"render_{uuid.uuid4().hex[:16]}"
    timeline_row, items = _load_timeline_items(project, timeline_id)
    resolved_timeline_id = str(timeline_row["id"])
    output_path = project.root / "renders" / f"{render_id}.mp4"
    temp_dir = project.root / "renders" / f"{render_id}_clips"
    temp_dir.mkdir(parents=True, exist_ok=True)

    commands: list[list[str]] = []
    join_decisions: list[dict[str, Any]] = []
    status = "complete"
    try:
        ranges = [_clamped_range(item) for item in items]
        clip_durations = [end - start for start, end in ranges]
        joins, join_decisions = _resolve_joins(
            _planned_joins(items, override_crossfade_sec=crossfade_sec),
            clip_durations,
        )
        clip_paths: list[Path] = []
        for index, item in enumerate(items):
            clip_path = temp_dir / f"clip_{index:04d}.mp4"
            start, end = ranges[index]
            # Micro fades only guard hard-cut sides; crossfaded sides get
            # their fade from acrossfade itself.
            fade_in = index == 0 or joins[index - 1]["type"] != "crossfade"
            fade_out = index == len(items) - 1 or joins[index]["type"] != "crossfade"
            command = _clip_command(
                item,
                clip_path,
                start=start,
                end=end,
                micro_fade_in=fade_in,
                micro_fade_out=fade_out,
                burn_captions=burn_captions,
            )
            try:
                _run(command)
            except RuntimeError:
                if not (burn_captions and item.get("caption_text")):
                    raise
                # drawtext is unavailable in some ffmpeg builds; render the
                # clip without the caption rather than failing the timeline.
                command = _clip_command(
                    clip_path=clip_path,
                    item=item,
                    start=start,
                    end=end,
                    micro_fade_in=fade_in,
                    micro_fade_out=fade_out,
                    burn_captions=False,
                )
                _run(command)
            commands.append(command)
            clip_paths.append(clip_path)

        combined_path = temp_dir / "combined.mp4"
        if len(clip_paths) > 1 and any(join["type"] == "crossfade" for join in joins):
            if _legacy_chain_safe(clip_durations, joins):
                command = _joined_command(clip_paths, clip_durations, joins, combined_path)
            else:
                command = _flat_joined_command(clip_paths, clip_durations, joins, combined_path)
        else:
            command = _concat_command(clip_paths, temp_dir, combined_path)
        _run(command)
        commands.append(command)

        if normalize_loudness:
            command = _loudnorm_command(combined_path, output_path, loudness_range)
            _run(command)
            commands.append(command)
        else:
            shutil.move(str(combined_path), str(output_path))
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
            join_decisions=join_decisions,
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
    return timeline, _pair_overlays([dict(row) for row in items])


def _pair_overlays(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold broll-track rows onto their primary items as render overlays.

    A broll row that names a missing primary is skipped (rendering the audio
    of nothing is not an option); primaries without overlays are untouched.
    """
    primaries = [row for row in rows if row.get("track_kind") != "broll"]
    by_id = {row["id"]: row for row in primaries}
    for row in rows:
        if row.get("track_kind") != "broll":
            continue
        try:
            meta = json.loads(row.get("overlay_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            meta = {}
        primary = by_id.get(meta.get("overlay_of"))
        if primary is None:
            continue
        primary["overlay_render"] = {
            "video_path": row["asset_path"],
            "video_start_sec": float(row["source_start_sec"]),
            "video_end_sec": float(row["source_end_sec"]),
        }
    return primaries


def _clamped_range(item: dict[str, Any]) -> tuple[float, float]:
    start = float(item["source_start_sec"])
    end = float(item["source_end_sec"])
    asset_duration = item.get("asset_duration_sec")
    if asset_duration is not None:
        asset_duration = float(asset_duration)
        start = min(start, max(0.0, asset_duration - 0.1))
        end = min(max(start + 0.1, end), asset_duration)
    if end - start < 0.1:
        end = start + 0.1
    return start, end


def _clip_command(
    item: dict[str, Any],
    clip_path: Path,
    *,
    start: float,
    end: float,
    micro_fade_in: bool,
    micro_fade_out: bool,
    burn_captions: bool,
) -> list[str]:
    duration = max(0.1, end - start)
    video_filters = [
        "scale=640:360:force_original_aspect_ratio=decrease",
        "pad=640:360:(ow-iw)/2:(oh-ih)/2",
        "fps=30",
        "format=yuv420p",
    ]
    caption = item.get("caption_text") if burn_captions and _caption_allowed(item) else None
    if caption:
        video_filters.append(_drawtext_filter(str(caption)))

    audio_filters = ["aresample=48000"]
    fade = min(AUDIO_MICRO_FADE_SEC, duration / 4)
    if micro_fade_in:
        audio_filters.append(f"afade=t=in:st=0:d={fade:.3f}")
    if micro_fade_out:
        audio_filters.append(f"afade=t=out:st={max(0.0, duration - fade):.3f}:d={fade:.3f}")

    codec_args = [
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
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(clip_path),
    ]

    overlay = item.get("overlay_render")
    if overlay:
        # J/L-style cutaway: video from the b-roll input, audio from the
        # primary's continuing range; the mux happens per segment so the
        # concat/transition/loudness pipeline downstream stays unchanged.
        video_start = float(overlay["video_start_sec"])
        return [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-ss",
            f"{video_start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(overlay["video_path"]),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-i",
            str(item["asset_path"]),
            "-filter_complex",
            f"[0:v]{','.join(video_filters)}[v];[1:a]{','.join(audio_filters)}[a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            *codec_args,
        ]

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
        ",".join(video_filters),
        "-af",
        ",".join(audio_filters),
        *codec_args,
    ]


def _caption_allowed(item: dict[str, Any]) -> bool:
    """Honor the timeline's per-item caption decision; legacy rows without one
    keep the old burn-everything behavior."""
    decision = item.get("caption_decision")
    if decision is None and item.get("caption_decision_json"):
        try:
            decision = json.loads(item["caption_decision_json"])
        except (TypeError, json.JSONDecodeError):
            decision = None
    if not isinstance(decision, dict):
        return True
    return bool(decision.get("burn"))


def _drawtext_filter(text: str) -> str:
    safe = (
        text.replace("\\", "\\\\")
        .replace("'", "’")
        .replace(":", "\\:")
        .replace(",", "\\,")
        .replace("[", "(")
        .replace("]", ")")
    )
    return (
        f"drawtext=text='{safe}'"
        ":fontcolor=white:fontsize=18:borderw=2:bordercolor=black@0.8"
        ":x=(w-text_w)/2:y=h-text_h-18"
    )


def _concat_command(clip_paths: list[Path], temp_dir: Path, output_path: Path) -> list[str]:
    concat_file = temp_dir / "concat.txt"
    concat_file.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in clip_paths),
        encoding="utf-8",
    )
    return [
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


def _planned_joins(
    items: list[dict[str, Any]],
    *,
    override_crossfade_sec: float,
) -> list[dict[str, Any]]:
    """One join decision per adjacent clip pair.

    Joins come from the timeline's per-item transition decisions; a positive
    --crossfade-sec forces every join to crossfade (legacy/manual override).
    """
    joins: list[dict[str, Any]] = []
    for item in items[1:]:
        if override_crossfade_sec > 0:
            joins.append({"type": "crossfade", "duration_sec": override_crossfade_sec})
            continue
        transition = item.get("transition")
        if transition is None and item.get("transition_json"):
            try:
                transition = json.loads(item["transition_json"])
            except (TypeError, json.JSONDecodeError):
                transition = None
        if isinstance(transition, dict) and transition.get("type") == "crossfade":
            joins.append(
                {
                    "type": "crossfade",
                    "duration_sec": float(transition.get("duration_sec") or 0.35),
                }
            )
        else:
            joins.append({"type": "cut"})
    return joins


def _resolve_joins(
    joins: list[dict[str, Any]],
    clip_durations: list[float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Clamp each crossfade to what its neighbor clips can actually afford.

    A fade consumes its duration from the tail of the left clip and the head
    of the right clip; two fades on one short clip can eat it entirely. Every
    clip keeps at least MIN_SOLO_SEC un-faded, fades that clamp below
    CROSSFADE_FLOOR_SEC become hard cuts, and every downgrade is recorded so
    the render report says what changed and why. Timeline length is preserved:
    shortening a fade shrinks overlap, never clip content.
    """
    resolved = [dict(join) for join in joins]
    decisions: list[dict[str, Any]] = []
    consumed_in = [0.0] * len(clip_durations)
    elapsed = clip_durations[0]
    for index, join in enumerate(resolved):
        left = clip_durations[index]
        right = clip_durations[index + 1]
        if join["type"] != "crossfade":
            elapsed += right
            continue
        requested = float(join.get("duration_sec") or 0.35)
        affordable = min(
            requested,
            right / 2 - 0.01,
            elapsed / 2 - 0.01,
            left - consumed_in[index] - MIN_SOLO_SEC,
        )
        if affordable < CROSSFADE_FLOOR_SEC:
            resolved[index] = {"type": "cut"}
            decisions.append(
                {
                    "join_index": index,
                    "requested_sec": round(requested, 3),
                    "applied_sec": 0.0,
                    "action": "downgraded_to_cut",
                    "why": (
                        f"clips of {left:.2f}s/{right:.2f}s cannot afford a "
                        f"{requested:.2f}s crossfade; hard cut with micro fades instead"
                    ),
                }
            )
            elapsed += right
            continue
        if affordable < requested - 0.005:
            decisions.append(
                {
                    "join_index": index,
                    "requested_sec": round(requested, 3),
                    "applied_sec": round(affordable, 3),
                    "action": "shortened",
                    "why": (
                        f"fade shortened so the {left:.2f}s/{right:.2f}s clips keep "
                        f"at least {MIN_SOLO_SEC:.2f}s of un-faded content"
                    ),
                }
            )
        resolved[index]["duration_sec"] = round(affordable, 3)
        consumed_in[index + 1] = affordable
        elapsed = elapsed - affordable + right
    return resolved, decisions


def _legacy_chain_safe(clip_durations: list[float], joins: list[dict[str, Any]]) -> bool:
    """Whether the chained xfade+acrossfade graph is in its proven-safe envelope.

    Measured behavior (see module constants): short chains never deadlock, and
    deep chains only deadlock when crossfade participants are short clips.
    """
    crossfade_indices = [index for index, join in enumerate(joins) if join["type"] == "crossfade"]
    if len(crossfade_indices) <= LEGACY_CHAIN_MAX_JOINS:
        return True
    participants = set()
    for index in crossfade_indices:
        participants.add(index)
        participants.add(index + 1)
    return min(clip_durations[index] for index in participants) >= LEGACY_CHAIN_MIN_CLIP_SEC


def _flat_joined_command(
    clip_paths: list[Path],
    clip_durations: list[float],
    joins: list[dict[str, Any]],
    output_path: Path,
) -> list[str]:
    """Join clips with the audio graph built flat instead of chained.

    The video side is the same xfade/concat chain as _joined_command (video
    chains do not deadlock at depth). The audio side gives every clip its own
    afade in/out at crossfaded joins, delays it to its timeline position, and
    sums everything with one amix — no cross-input chaining, so the ffmpeg
    scheduler starvation that kills deep acrossfade chains over short clips
    cannot occur. Linear afades summed over the overlap match acrossfade's
    default triangular curve.
    """
    filters: list[str] = [f"[{index}:v]settb=AVTB[vin{index}]" for index in range(len(clip_paths))]
    video_in = "[vin0]"
    starts = [0.0]
    elapsed = clip_durations[0]
    for index in range(1, len(clip_paths)):
        join = joins[index - 1]
        fade = float(join.get("duration_sec") or 0.0) if join["type"] == "crossfade" else 0.0
        video_out = f"[v{index}]" if index < len(clip_paths) - 1 else "[vout]"
        if fade >= 0.03:
            offset = elapsed - fade
            filters.append(
                f"{video_in}[vin{index}]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}{video_out}"
            )
            starts.append(offset)
            elapsed = offset + clip_durations[index]
        else:
            filters.append(f"{video_in}[vin{index}]concat=n=2:v=1:a=0{video_out}")
            starts.append(elapsed)
            elapsed += clip_durations[index]
        video_in = video_out

    audio_labels = []
    for index in range(len(clip_paths)):
        audio_filters = []
        fade_in = 0.0
        if index > 0 and joins[index - 1]["type"] == "crossfade":
            fade_in = float(joins[index - 1].get("duration_sec") or 0.0)
        fade_out = 0.0
        if index < len(clip_paths) - 1 and joins[index]["type"] == "crossfade":
            fade_out = float(joins[index].get("duration_sec") or 0.0)
        if fade_in >= 0.03:
            audio_filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out >= 0.03:
            start_at = max(0.0, clip_durations[index] - fade_out)
            audio_filters.append(f"afade=t=out:st={start_at:.3f}:d={fade_out:.3f}")
        delay_ms = int(round(starts[index] * 1000))
        if delay_ms > 0:
            audio_filters.append(f"adelay={delay_ms}:all=1")
        if not audio_filters:
            audio_filters.append("anull")
        filters.append(f"[{index}:a]{','.join(audio_filters)}[fa{index}]")
        audio_labels.append(f"[fa{index}]")
    filters.append(f"{''.join(audio_labels)}amix=inputs={len(clip_paths)}:normalize=0[aout]")

    command = ["ffmpeg", "-y", "-v", "error"]
    for path in clip_paths:
        command.extend(["-i", str(path)])
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
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
    )
    return command


def _joined_command(
    clip_paths: list[Path],
    clip_durations: list[float],
    joins: list[dict[str, Any]],
    output_path: Path,
) -> list[str]:
    """Compose clips left to right, hard-cutting or crossfading per join.

    xfade/acrossfade consume the fade duration as overlap, so each fade is
    clamped to half of either neighbor; a fade that clamps below 30ms falls
    back to a hard cut.
    """
    # concat and xfade outputs disagree on timebase unless every video input
    # is normalized first; without settb, an xfade after a concat fails with
    # a timebase-mismatch error.
    filters: list[str] = [f"[{index}:v]settb=AVTB[vin{index}]" for index in range(len(clip_paths))]
    video_in = "[vin0]"
    audio_in = "[0:a]"
    elapsed = clip_durations[0]
    for index in range(1, len(clip_paths)):
        join = joins[index - 1]
        fade = 0.0
        if join["type"] == "crossfade":
            fade = min(
                float(join.get("duration_sec") or 0.0),
                clip_durations[index] / 2 - 0.01,
                elapsed / 2 - 0.01,
            )
        video_out = f"[v{index}]" if index < len(clip_paths) - 1 else "[vout]"
        audio_out = f"[a{index}]" if index < len(clip_paths) - 1 else "[aout]"
        if fade >= 0.03:
            offset = elapsed - fade
            filters.append(
                f"{video_in}[vin{index}]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}{video_out}"
            )
            filters.append(f"{audio_in}[{index}:a]acrossfade=d={fade:.3f}{audio_out}")
            elapsed = offset + clip_durations[index]
        else:
            filters.append(f"{video_in}[vin{index}]concat=n=2:v=1:a=0{video_out}")
            filters.append(f"{audio_in}[{index}:a]concat=n=2:v=0:a=1{audio_out}")
            elapsed += clip_durations[index]
        video_in = video_out
        audio_in = audio_out

    command = ["ffmpeg", "-y", "-v", "error"]
    for path in clip_paths:
        command.extend(["-i", str(path)])
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
            "-map",
            "[aout]",
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
    )
    return command


def _loudnorm_command(input_path: Path, output_path: Path, loudness_range: float) -> list[str]:
    # One loudness pass over the whole timeline; per-clip normalization made
    # music jump in level at every cut. A wider LRA preserves a planned
    # quiet-to-loud emotional arc instead of compressing it flat.
    return [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(input_path),
        "-c:v",
        "copy",
        "-af",
        f"loudnorm=I=-16:TP=-1.5:LRA={loudness_range:g}",
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
    join_decisions: list[dict[str, Any]],
    duration_sec: float | None,
) -> None:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into renders (
                id, project_id, timeline_id, path, status, command_json,
                report_json, duration_sec, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                render_id,
                "default",
                timeline_id,
                str(output_path),
                status,
                json.dumps(commands),
                json.dumps({"join_decisions": join_decisions}),
                duration_sec,
                utc_now(),
            ),
        )
