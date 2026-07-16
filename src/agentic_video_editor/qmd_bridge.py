from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .db import connect_db, migrate
from .project import Project, utc_now


QMD_SOURCE = "qmd"
CARDS_DIR_NAME = "qmd_cards"

QmdRunner = Callable[[list[str]], str]


@dataclass(frozen=True)
class ExportSummary:
    cards_written: int
    cards_dir: str


@dataclass(frozen=True)
class RelateSummary:
    segments_queried: int
    relationships_created: int


def export_cards(project: Project, *, out_dir: Path | None = None) -> ExportSummary:
    """Write one markdown card per segment for qmd to index.

    The card serializes everything the retrieval layer knows about a segment —
    summary, verbatim word units, affordances, context card, and overlapping
    verbatim ASR spans — so vector search over cards sees the same evidence the
    planner sees.
    """
    cards_dir = out_dir or (project.root / CARDS_DIR_NAME)
    cards_dir.mkdir(parents=True, exist_ok=True)

    with connect_db(project.db_path) as conn:
        migrate(conn)
        segments = _load_card_segments(conn)
        spans_by_asset = _load_spans_by_asset(conn)

    written = 0
    for segment in segments:
        card_path = cards_dir / f"{segment['id']}.md"
        card_path.write_text(_card_markdown(segment, spans_by_asset), encoding="utf-8")
        written += 1

    return ExportSummary(cards_written=written, cards_dir=str(cards_dir))


def relate_from_qmd(
    project: Project,
    *,
    collection: str,
    top_k: int = 8,
    min_score: float = 0.6,
    duplicate_score: float = 0.85,
    runner: QmdRunner | None = None,
) -> RelateSummary:
    """Mine cross-clip relationships by vector-searching each segment card.

    Requires the cards directory to be an embedded qmd collection:
        ave export-cards PROJECT
        qmd collection add PROJECT/qmd_cards --name COLLECTION
        qmd embed
    """
    if runner is None:
        runner = _qmd_runner

    with connect_db(project.db_path) as conn:
        migrate(conn)
        segments = _load_card_segments(conn)
        existing = _existing_qmd_pairs(conn)

    prefix = f"qmd://{collection}/"
    # qmd slugifies file names (underscores become hyphens), so match ids
    # through a normalized form instead of the raw stem.
    id_by_slug = {_slug(segment["id"]): segment["id"] for segment in segments}
    queried = 0
    created = 0
    for segment in segments:
        query = _relate_query(segment)
        if not query:
            continue
        stdout = runner(["vsearch", query, "-n", str(top_k), "--json"])
        queried += 1
        try:
            results = json.loads(stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(results, list):
            continue
        for result in results:
            stem = _segment_id_from_file(result.get("file"), prefix)
            other_id = id_by_slug.get(_slug(stem)) if stem else None
            score = _float(result.get("score"))
            if other_id is None or score is None or other_id == segment["id"]:
                continue
            if score < min_score:
                continue
            other = next((s for s in segments if s["id"] == other_id), None)
            if other is None:
                continue
            if other["asset_id"] == segment["asset_id"] and _overlaps(segment, other):
                continue
            pair = tuple(sorted([segment["id"], other_id]))
            if pair in existing:
                continue
            existing.add(pair)
            relationship_type = _relationship_type(segment, other, score, duplicate_score)
            _insert_relationship(project, segment["id"], other_id, relationship_type, score)
            created += 1

    return RelateSummary(segments_queried=queried, relationships_created=created)


def _relationship_type(
    segment: dict[str, Any],
    other: dict[str, Any],
    score: float,
    duplicate_score: float,
) -> str:
    if score >= duplicate_score:
        return "duplicates"
    if other["asset_id"] == segment["asset_id"]:
        return "echoes_same_source"
    return "echoes"


def _relate_query(segment: dict[str, Any]) -> str:
    parts = [
        str(segment.get("summary") or ""),
        str(segment.get("transcript_summary") or ""),
        " ".join(unit.get("text", "") for unit in segment.get("word_units") or []),
    ]
    query = " ".join(part for part in parts if part).strip()
    return query[:300]


def _segment_id_from_file(file_value: Any, prefix: str) -> str | None:
    file_str = str(file_value or "")
    if not file_str.startswith(prefix) or not file_str.endswith(".md"):
        return None
    return Path(file_str[len(prefix):]).stem


def _slug(value: str | None) -> str:
    return str(value or "").lower().replace("_", "-")


def _overlaps(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return a["start_sec"] < b["end_sec"] and b["start_sec"] < a["end_sec"]


def _insert_relationship(
    project: Project,
    from_id: str,
    to_id: str,
    relationship_type: str,
    score: float,
) -> None:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into relationships (
                id, project_id, from_entity_type, from_entity_id, to_entity_type,
                to_entity_id, relationship_type, confidence, evidence, source, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"rel_{uuid.uuid4().hex[:16]}",
                "default",
                "segment",
                from_id,
                "segment",
                to_id,
                relationship_type,
                score,
                f"qmd vector similarity {score:.2f}",
                QMD_SOURCE,
                utc_now(),
            ),
        )


def _existing_qmd_pairs(conn) -> set[tuple[str, str]]:
    rows = conn.execute(
        "select from_entity_id, to_entity_id from relationships where source = ?",
        (QMD_SOURCE,),
    ).fetchall()
    return {tuple(sorted([row["from_entity_id"], row["to_entity_id"]])) for row in rows}


def _qmd_runner(args: list[str]) -> str:
    completed = subprocess.run(
        ["qmd", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "qmd command failed")
    return completed.stdout


def _load_card_segments(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            segments.id,
            segments.asset_id,
            assets.file_name,
            segments.start_sec,
            segments.end_sec,
            segments.summary,
            segments.transcript_summary,
            segments.word_units_json,
            segments.story_function,
            segments.setup_questions_json,
            segments.payoff_answers_json,
            segments.audio_affordance,
            segments.visual_affordance,
            segments.cut_notes,
            segments.people_json,
            segments.actions_json,
            segments.moods_json,
            segments.story_roles_json,
            editorial_context_cards.local_meaning,
            editorial_context_cards.corpus_meaning,
            editorial_context_cards.editorial_use
        from segments
        join assets on assets.id = segments.asset_id
        left join editorial_context_cards
            on editorial_context_cards.segment_id = segments.id
        where segments.project_id = ? and segments.usable = 1
        order by assets.file_name, segments.start_sec
        """,
        ("default",),
    ).fetchall()
    segments = []
    for row in rows:
        item = dict(row)
        for key in ["people_json", "actions_json", "moods_json", "story_roles_json", "setup_questions_json", "payoff_answers_json"]:
            item[key.replace("_json", "")] = _json_list(item.pop(key))
        try:
            word_units = json.loads(item.pop("word_units_json") or "[]")
        except (TypeError, json.JSONDecodeError):
            word_units = []
        item["word_units"] = word_units if isinstance(word_units, list) else []
        segments.append(item)
    return segments


def _load_spans_by_asset(conn) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        select asset_id, start_sec, end_sec, text, kind, source
        from transcript_spans
        where project_id = ?
        order by asset_id, start_sec
        """,
        ("default",),
    ).fetchall()
    spans: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        spans.setdefault(row["asset_id"], []).append(dict(row))
    return spans


def _card_markdown(segment: dict[str, Any], spans_by_asset: dict[str, list[dict[str, Any]]]) -> str:
    lines = [
        f"# {segment['file_name']} {segment['start_sec']:.1f}-{segment['end_sec']:.1f}",
        "",
        f"- segment_id: {segment['id']}",
        f"- asset_id: {segment['asset_id']}",
        f"- time_range: {segment['start_sec']:.2f}-{segment['end_sec']:.2f}",
        f"- story_roles: {', '.join(segment['story_roles']) or 'none'}",
        f"- story_function: {segment.get('story_function') or 'unknown'}",
        f"- audio: {segment.get('audio_affordance') or 'unknown'}",
        f"- visual: {segment.get('visual_affordance') or 'unknown'}",
        "",
        "## Summary",
        "",
        str(segment.get("summary") or ""),
    ]
    if segment.get("transcript_summary"):
        lines += ["", "## Transcript summary", "", str(segment["transcript_summary"])]
    word_units = [u for u in segment.get("word_units") or [] if isinstance(u, dict) and u.get("text")]
    if word_units:
        lines += ["", "## Word units", ""]
        lines += [
            f"- \"{unit['text']}\" ({_float(unit.get('start_sec')) or 0:.2f}-{_float(unit.get('end_sec')) or 0:.2f}, {unit.get('kind', 'spoken')})"
            for unit in word_units
        ]
    overlapping = [
        span
        for span in spans_by_asset.get(segment["asset_id"], [])
        if span["start_sec"] < segment["end_sec"] and segment["start_sec"] < span["end_sec"]
    ]
    if overlapping:
        lines += ["", "## Verbatim spans", ""]
        lines += [
            f"- [{span['start_sec']:.2f}-{span['end_sec']:.2f}] ({span['kind']}, {span['source']}) {span['text']}"
            for span in overlapping
        ]
    if segment.get("setup_questions"):
        lines += ["", "## Raises", ""] + [f"- {q}" for q in segment["setup_questions"]]
    if segment.get("payoff_answers"):
        lines += ["", "## Answers", ""] + [f"- {q}" for q in segment["payoff_answers"]]
    for label, key in [("Local meaning", "local_meaning"), ("Corpus meaning", "corpus_meaning"), ("Editorial use", "editorial_use")]:
        if segment.get(key):
            lines += ["", f"## {label}", "", str(segment[key])]
    extras = []
    if segment.get("people"):
        extras.append("people: " + ", ".join(segment["people"]))
    if segment.get("actions"):
        extras.append("actions: " + ", ".join(segment["actions"]))
    if segment.get("moods"):
        extras.append("moods: " + ", ".join(segment["moods"]))
    if segment.get("cut_notes"):
        extras.append("cut notes: " + str(segment["cut_notes"]))
    if extras:
        lines += ["", "## Details", ""] + [f"- {extra}" for extra in extras]
    return "\n".join(lines) + "\n"


def _json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        value = raw
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if str(value or "").strip():
        return [str(value)]
    return []


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
