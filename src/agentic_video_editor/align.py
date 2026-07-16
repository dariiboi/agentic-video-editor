from __future__ import annotations

import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import connect_db, migrate
from .project import Project, utc_now


ASR_SOURCE = "local_asr"
ASR_WORD_DETECTOR = "asr_word"

# A word boundary is only a useful snap target when there is an audible pause
# around it; boundaries inside continuous speech would invite mid-sentence cuts.
MIN_PAUSE_SEC = 0.12

Transcriber = Callable[[Path], list[dict[str, Any]]]


@dataclass(frozen=True)
class AlignSummary:
    assets_requested: int
    assets_completed: int
    spans_created: int
    words_created: int
    cut_points_created: int


def align_project(
    project: Project,
    *,
    model_size: str = "small.en",
    limit: int | None = None,
    force: bool = False,
    transcriber: Transcriber | None = None,
) -> AlignSummary:
    """Word-align speech locally and turn word boundaries into cut points.

    Uses faster-whisper (CTranslate2 Whisper) with word_timestamps=True, so the
    resulting transcript spans are verbatim and quotable, and every pause-adjacent
    word boundary becomes a frame-accurate snap target in scene_boundaries.
    """
    if transcriber is None:
        transcriber = _faster_whisper_transcriber(model_size)

    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = _assets_with_audio(conn, limit=limit, force=force)

    totals = {"spans": 0, "words": 0, "points": 0, "completed": 0}
    for asset in assets:
        source_path = Path(str(asset["path"]))
        if not source_path.exists():
            continue
        with tempfile.TemporaryDirectory(prefix="ave_align_") as temp_dir:
            wav_path = Path(temp_dir) / "audio.wav"
            _extract_wav(source_path, wav_path)
            segments = transcriber(wav_path)
        result = _store_alignment(
            project,
            str(asset["id"]),
            segments,
            duration_sec=_float(asset.get("duration_sec")),
        )
        totals["spans"] += result["spans"]
        totals["words"] += result["words"]
        totals["points"] += result["points"]
        totals["completed"] += 1

    return AlignSummary(
        assets_requested=len(assets),
        assets_completed=totals["completed"],
        spans_created=totals["spans"],
        words_created=totals["words"],
        cut_points_created=totals["points"],
    )


def align_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        spans = conn.execute(
            "select count(*) as count from transcript_spans where project_id = ? and source = ?",
            ("default", ASR_SOURCE),
        ).fetchone()
        words = conn.execute(
            "select count(*) as count from word_alignments where project_id = ?",
            ("default",),
        ).fetchone()
        points = conn.execute(
            "select count(*) as count from scene_boundaries where project_id = ? and detector = ?",
            ("default", ASR_WORD_DETECTOR),
        ).fetchone()
    return {"spans": spans["count"], "words": words["count"], "cut_points": points["count"]}


def _faster_whisper_transcriber(model_size: str) -> Transcriber:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "faster-whisper is not installed in this Python environment; "
            "install it or pass --provider mock for tests"
        ) from exc

    model = WhisperModel(model_size, compute_type="int8")

    def _transcribe(wav_path: Path) -> list[dict[str, Any]]:
        segments, _info = model.transcribe(
            str(wav_path),
            word_timestamps=True,
            vad_filter=True,
        )
        results = []
        for segment in segments:
            words = [
                {
                    "text": word.word.strip(),
                    "start_sec": float(word.start),
                    "end_sec": float(word.end),
                    "confidence": float(word.probability),
                }
                for word in (segment.words or [])
                if word.word and word.word.strip()
            ]
            results.append(
                {
                    "start_sec": float(segment.start),
                    "end_sec": float(segment.end),
                    "text": segment.text.strip(),
                    "confidence": _avg([w["confidence"] for w in words]),
                    "words": words,
                }
            )
        return results

    return _transcribe


def _assets_with_audio(conn, *, limit: int | None, force: bool) -> list[dict[str, Any]]:
    query = """
        select id, path, duration_sec
        from assets
        where project_id = ? and ingest_status = ? and has_audio = 1
    """
    params: list[Any] = ["default", "ready"]
    if not force:
        query += """
            and not exists (
                select 1 from transcript_spans
                where transcript_spans.asset_id = assets.id
                  and transcript_spans.source = ?
            )
        """
        params.append(ASR_SOURCE)
    query += " order by path"
    if limit is not None:
        query += " limit ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _extract_wav(source: Path, wav_path: Path) -> None:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(wav_path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffmpeg audio extraction failed")


def _store_alignment(
    project: Project,
    asset_id: str,
    segments: list[dict[str, Any]],
    *,
    duration_sec: float | None,
) -> dict[str, int]:
    now = utc_now()
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            "delete from transcript_spans_fts where span_id in (select id from transcript_spans where asset_id = ? and source = ?)",
            (asset_id, ASR_SOURCE),
        )
        conn.execute(
            "delete from transcript_spans where asset_id = ? and source = ?",
            (asset_id, ASR_SOURCE),
        )
        conn.execute("delete from word_alignments where asset_id = ?", (asset_id,))
        conn.execute(
            "delete from scene_boundaries where asset_id = ? and detector = ?",
            (asset_id, ASR_WORD_DETECTOR),
        )

        spans = 0
        words_created = 0
        all_words: list[dict[str, Any]] = []
        for segment in segments:
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            span_id = f"span_{uuid.uuid4().hex[:16]}"
            start = max(0.0, _float(segment.get("start_sec")) or 0.0)
            end = max(start + 0.05, _float(segment.get("end_sec")) or start + 0.05)
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
                    None,
                    text,
                    "speech",
                    _float(segment.get("confidence")),
                    ASR_SOURCE,
                    None,
                    now,
                ),
            )
            conn.execute(
                "insert into transcript_spans_fts (span_id, asset_id, text) values (?, ?, ?)",
                (span_id, asset_id, text),
            )
            spans += 1

            for word in segment.get("words") or []:
                word_text = str(word.get("text") or "").strip()
                word_start = _float(word.get("start_sec"))
                word_end = _float(word.get("end_sec"))
                if not word_text or word_start is None or word_end is None:
                    continue
                conn.execute(
                    """
                    insert into word_alignments (
                        id, project_id, asset_id, span_id, start_sec, end_sec,
                        text, confidence, source, created_at
                    )
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"word_{uuid.uuid4().hex[:16]}",
                        "default",
                        asset_id,
                        span_id,
                        word_start,
                        word_end,
                        word_text,
                        _float(word.get("confidence")),
                        ASR_SOURCE,
                        now,
                    ),
                )
                all_words.append({"start_sec": word_start, "end_sec": word_end})
                words_created += 1

        points = _word_boundary_points(all_words, duration_sec)
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
                    ASR_WORD_DETECTOR,
                    None,
                    point["reason"],
                    None,
                    now,
                ),
            )

    return {"spans": spans, "words": words_created, "points": len(points)}


def _word_boundary_points(
    words: list[dict[str, Any]],
    duration_sec: float | None,
) -> list[dict[str, Any]]:
    """Keep only pause-adjacent word boundaries as snap targets.

    word_start marks a clean in-point (speech begins after a pause);
    word_end marks a clean out-point (a phrase just finished).
    """
    ordered = sorted(words, key=lambda w: w["start_sec"])
    points: list[dict[str, Any]] = []
    for index, word in enumerate(ordered):
        previous_end = ordered[index - 1]["end_sec"] if index > 0 else None
        next_start = ordered[index + 1]["start_sec"] if index + 1 < len(ordered) else None

        pause_before = previous_end is None or word["start_sec"] - previous_end >= MIN_PAUSE_SEC
        pause_after = next_start is None or next_start - word["end_sec"] >= MIN_PAUSE_SEC

        if pause_before and _within(word["start_sec"], duration_sec):
            points.append({"time_sec": round(word["start_sec"], 3), "reason": "word_start"})
        if pause_after and _within(word["end_sec"], duration_sec):
            points.append({"time_sec": round(word["end_sec"], 3), "reason": "word_end"})
    return points


def _within(time_sec: float, duration_sec: float | None) -> bool:
    if time_sec <= 0:
        return False
    return duration_sec is None or time_sec < duration_sec


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _float(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
