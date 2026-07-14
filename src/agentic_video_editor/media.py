from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


SUPPORTED_MEDIA_EXTENSIONS = {".mp4", ".mov", ".m4v", ".wav", ".mp3"}


@dataclass(frozen=True)
class ProbeResult:
    status: str
    metadata: dict[str, Any]
    raw_json: dict[str, Any] | None
    error: str | None


def discover_media(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()

    for source in paths:
        resolved = source.expanduser().resolve()
        if resolved.is_dir():
            candidates = (p for p in resolved.rglob("*") if p.is_file())
        elif resolved.is_file():
            candidates = (resolved,)
        else:
            continue

        for candidate in candidates:
            if candidate.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            files.append(candidate)

    return sorted(files)


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_asset_id(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"asset_{digest}"


def probe_media(path: Path) -> ProbeResult:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]

    try:
        completed = subprocess.run(command, capture_output=True, check=False, text=True)
    except FileNotFoundError:
        return ProbeResult(
            status="missing_ffprobe",
            metadata={},
            raw_json=None,
            error="ffprobe executable was not found",
        )

    if completed.returncode != 0:
        error = completed.stderr.strip() or completed.stdout.strip() or "ffprobe failed"
        return ProbeResult(status="failed", metadata={}, raw_json=None, error=error)

    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return ProbeResult(status="failed", metadata={}, raw_json=None, error=str(exc))

    return ProbeResult(status="ok", metadata=_normalize_probe(raw), raw_json=raw, error=None)


def _normalize_probe(raw: dict[str, Any]) -> dict[str, Any]:
    streams = raw.get("streams") or []
    fmt = raw.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    metadata: dict[str, Any] = {
        "duration_sec": _float_or_none(fmt.get("duration")),
        "fps": None,
        "width": None,
        "height": None,
        "video_codec": None,
        "audio_codec": None,
        "has_audio": audio is not None,
        "created_at_source": None,
    }

    if video:
        metadata.update(
            {
                "fps": _fps_or_none(video.get("avg_frame_rate") or video.get("r_frame_rate")),
                "width": _int_or_none(video.get("width")),
                "height": _int_or_none(video.get("height")),
                "video_codec": video.get("codec_name"),
            }
        )

    if audio:
        metadata["audio_codec"] = audio.get("codec_name")

    tags = fmt.get("tags") or {}
    metadata["created_at_source"] = (
        tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate")
    )

    return metadata


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fps_or_none(value: Any) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        fps = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError):
        return None
    return round(fps, 6)
