from __future__ import annotations

import json
import statistics
from collections import Counter
from typing import Any

from .db import connect_db, migrate
from .project import Project


TOP_TERMS_PER_FACET = 8
TOP_QUOTABLE_LINES = 8
TOP_CANDIDATES = 5
TOP_ASSETS_LISTED = 10
TOP_CLUSTERS = 3

OPENER_ROLES = {"hook"}
ENDER_ROLES = {"payoff", "conclusion", "climax"}
CLUSTER_RELATIONSHIP_TYPES = {"duplicates", "echoes", "echoes_same_source"}

_HISTOGRAM_SKIP_KEYS = {"start_sec", "end_sec", "confidence", "evidence"}


def corpus_profile(project: Project) -> dict[str, Any]:
    """Deterministic summary of what the index actually holds.

    Prompt input for the IntentAgent/StructureAgent so invented structures are
    grounded in the footage rather than genre priors. Descriptive only: it
    surfaces existing scores and stored evidence, it makes no creative choices.
    """
    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = [dict(row) for row in conn.execute(
            "select id, file_name, duration_sec, has_audio, ingest_status from assets where project_id = ? order by file_name",
            ("default",),
        ).fetchall()]
        word_rows = conn.execute(
            "select asset_id, start_sec, end_sec from word_alignments where project_id = ?",
            ("default",),
        ).fetchall()
        span_rows = conn.execute(
            "select asset_id, start_sec, end_sec from transcript_spans where project_id = ?",
            ("default",),
        ).fetchall()
        segment_rows = conn.execute(
            "select start_sec, end_sec, usable from segments where project_id = ?",
            ("default",),
        ).fetchall()
        quote_rows = conn.execute(
            """
            select assets.file_name, segments.word_units_json
            from segments
            join assets on assets.id = segments.asset_id
            where segments.project_id = ? and segments.usable = 1
            order by assets.file_name, segments.start_sec
            """,
            ("default",),
        ).fetchall()
        observation_rows = conn.execute(
            "select observation_type, value from observations where project_id = ?",
            ("default",),
        ).fetchall()
        candidate_rows = conn.execute(
            """
            select
                segments.id as segment_id,
                assets.file_name,
                segments.start_sec,
                segments.end_sec,
                segments.summary,
                segments.story_roles_json,
                coalesce(selects.score, segments.quality_score, 0) as score
            from segments
            join assets on assets.id = segments.asset_id
            left join selects on selects.segment_id = segments.id
            where segments.project_id = ? and segments.usable = 1
            order by score desc, segments.start_sec
            """,
            ("default",),
        ).fetchall()
        relationship_rows = conn.execute(
            """
            select from_entity_id, to_entity_id, relationship_type
            from relationships
            where project_id = ?
              and from_entity_type = 'segment' and to_entity_type = 'segment'
            """,
            ("default",),
        ).fetchall()

    return {
        "assets": _asset_inventory(assets),
        "speech": _speech_density(assets, word_rows, span_rows),
        "segments": _segment_inventory(segment_rows),
        "quotable_lines": _quotable_lines(quote_rows),
        "facets": _facet_histograms(observation_rows),
        "openers": _role_candidates(candidate_rows, OPENER_ROLES),
        "enders": _role_candidates(candidate_rows, ENDER_ROLES),
        "clusters": _cluster_hints(relationship_rows),
    }


def _asset_inventory(assets: list[dict[str, Any]]) -> dict[str, Any]:
    ready = [asset for asset in assets if asset["ingest_status"] == "ready"]
    by_status: Counter[str] = Counter(asset["ingest_status"] for asset in assets)
    return {
        "count": len(assets),
        "ready_count": len(ready),
        "total_duration_sec": round(sum(_num(asset["duration_sec"]) for asset in ready), 3),
        "with_audio": sum(1 for asset in ready if asset["has_audio"]),
        "by_status": dict(sorted(by_status.items())),
        "items": [
            {
                "file_name": asset["file_name"],
                "duration_sec": _num(asset["duration_sec"]) or None,
                "has_audio": bool(asset["has_audio"]),
            }
            for asset in ready
        ],
    }


def _speech_density(assets: list[dict[str, Any]], word_rows, span_rows) -> dict[str, Any]:
    durations = {asset["id"]: _num(asset["duration_sec"]) for asset in assets if asset["ingest_status"] == "ready"}
    total = sum(durations.values())
    for basis, rows in (("word_alignments", word_rows), ("transcript_spans", span_rows)):
        covered_by_asset = _merged_coverage(rows, durations)
        if covered_by_asset:
            covered = sum(covered_by_asset.values())
            per_asset = [
                {
                    "file_name": asset["file_name"],
                    "speech_ratio": _ratio(covered_by_asset.get(asset["id"], 0.0), durations.get(asset["id"], 0.0)),
                }
                for asset in assets
                if asset["id"] in durations
            ]
            return {
                "basis": basis,
                "speech_ratio": _ratio(covered, total),
                "per_asset": per_asset,
            }
    return {"basis": "none", "speech_ratio": None, "per_asset": []}


def _merged_coverage(rows, durations: dict[str, float]) -> dict[str, float]:
    intervals: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        asset_id = row["asset_id"]
        if asset_id not in durations:
            continue
        start = max(0.0, _num(row["start_sec"]))
        end = min(_num(row["end_sec"]), durations[asset_id] or _num(row["end_sec"]))
        if end > start:
            intervals.setdefault(asset_id, []).append((start, end))
    covered: dict[str, float] = {}
    for asset_id, spans in intervals.items():
        spans.sort()
        total = 0.0
        cursor_end = -1.0
        for start, end in spans:
            if start > cursor_end:
                total += end - start
                cursor_end = end
            elif end > cursor_end:
                total += end - cursor_end
                cursor_end = end
        covered[asset_id] = total
    return covered


def _segment_inventory(segment_rows) -> dict[str, Any]:
    durations = [
        max(0.0, _num(row["end_sec"]) - _num(row["start_sec"]))
        for row in segment_rows
        if row["usable"]
    ]
    distribution = None
    if durations:
        distribution = {
            "min_sec": round(min(durations), 3),
            "median_sec": round(statistics.median(durations), 3),
            "max_sec": round(max(durations), 3),
        }
    return {
        "count": len(segment_rows),
        "usable_count": len(durations),
        "duration_distribution": distribution,
    }


def _quotable_lines(quote_rows) -> list[dict[str, Any]]:
    """Verbatim word units with timestamps — the material a word-driven
    structure can quote back without ever inventing times."""
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in quote_rows:
        try:
            units = json.loads(row["word_units_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        for unit in units if isinstance(units, list) else []:
            if not isinstance(unit, dict):
                continue
            text = str(unit.get("text") or "").strip()
            try:
                start, end = float(unit["start_sec"]), float(unit["end_sec"])
            except (KeyError, TypeError, ValueError):
                continue
            key = text.lower()
            if not text or key in seen or end <= start:
                continue
            seen.add(key)
            lines.append(
                {
                    "text": text,
                    "file_name": row["file_name"],
                    "start_sec": start,
                    "end_sec": end,
                    "kind": str(unit.get("kind") or ""),
                }
            )
            if len(lines) >= TOP_QUOTABLE_LINES:
                return lines
    return lines


def _facet_histograms(observation_rows) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    terms_by_facet: dict[str, Counter[str]] = {}
    for row in observation_rows:
        facet = row["observation_type"]
        counts[facet] += 1
        try:
            value = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            continue
        histogram = terms_by_facet.setdefault(facet, Counter())
        for term in _histogram_terms(value):
            histogram[term] += 1
    return {
        "observation_count": sum(counts.values()),
        "by_type": {
            facet: {
                "count": counts[facet],
                "top_terms": [
                    {"term": term, "count": count}
                    for term, count in sorted(
                        terms_by_facet.get(facet, Counter()).items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:TOP_TERMS_PER_FACET]
                ],
            }
            for facet in sorted(counts)
        },
    }


def _histogram_terms(node: Any) -> list[str]:
    """Generic term extraction over any facet payload shape.

    String leaves become terms; a dict whose values are all short strings also
    contributes the joined phrase (sorted-key order), so `{"color": "green",
    "garment": "t-shirt"}` yields "green t-shirt" alongside its parts.
    """
    terms: list[str] = []

    def _walk(item: Any) -> None:
        if isinstance(item, dict):
            values = [item[key] for key in sorted(item) if key not in _HISTOGRAM_SKIP_KEYS]
            strings = [value for value in values if isinstance(value, str) and value.strip()]
            if strings and len(strings) == len(values) and len(strings) > 1:
                phrase = " ".join(value.strip().lower() for value in strings)
                if len(phrase) <= 48:
                    terms.append(phrase)
            for key, value in item.items():
                if key not in _HISTOGRAM_SKIP_KEYS:
                    _walk(value)
        elif isinstance(item, list):
            for value in item:
                _walk(value)
        elif isinstance(item, str):
            term = item.strip().lower()
            if term and len(term) <= 48 and not term.replace(".", "").isdigit():
                terms.append(term)

    _walk(node)
    return terms


def _role_candidates(candidate_rows, wanted_roles: set[str]) -> list[dict[str, Any]]:
    results = []
    for row in candidate_rows:
        try:
            roles = json.loads(row["story_roles_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            roles = []
        normalized = {str(role).strip().lower().replace("-", "_").replace(" ", "_") for role in roles}
        if not (normalized & wanted_roles):
            continue
        results.append(
            {
                "segment_id": row["segment_id"],
                "file_name": row["file_name"],
                "time_range": [_num(row["start_sec"]), _num(row["end_sec"])],
                "roles": sorted(normalized & wanted_roles),
                "score": round(_num(row["score"]), 3),
                "summary": str(row["summary"] or "")[:120],
            }
        )
        if len(results) >= TOP_CANDIDATES:
            break
    return results


def _cluster_hints(relationship_rows) -> dict[str, Any]:
    by_type: Counter[str] = Counter()
    parent: dict[str, str] = {}

    def _find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for row in relationship_rows:
        relationship_type = row["relationship_type"]
        if relationship_type not in CLUSTER_RELATIONSHIP_TYPES:
            continue
        by_type[relationship_type] += 1
        left, right = row["from_entity_id"], row["to_entity_id"]
        parent.setdefault(left, left)
        parent.setdefault(right, right)
        parent[_find(left)] = _find(right)

    members: dict[str, list[str]] = {}
    for node in parent:
        members.setdefault(_find(node), []).append(node)
    clusters = sorted(members.values(), key=lambda group: (-len(group), group[0]))
    return {
        "edge_counts": dict(sorted(by_type.items())),
        "cluster_count": len(clusters),
        "largest_clusters": [
            {"size": len(group), "segment_ids": sorted(group)}
            for group in clusters[:TOP_CLUSTERS]
        ],
    }


def corpus_profile_markdown(profile: dict[str, Any]) -> str:
    """Compact rendering embedded in agent prompts; keep it short and factual."""
    assets = profile["assets"]
    lines = ["# Corpus Profile", "", "## Assets"]
    if not assets["ready_count"]:
        lines.append("- Empty corpus: no ready assets.")
    else:
        lines.append(
            f"- {assets['ready_count']} ready asset(s), {assets['total_duration_sec']}s total, "
            f"{assets['with_audio']} with audio"
        )
        for item in assets["items"][:TOP_ASSETS_LISTED]:
            duration = f"{item['duration_sec']}s" if item["duration_sec"] else "unknown length"
            lines.append(f"- {item['file_name']} — {duration}, {'audio' if item['has_audio'] else 'no audio'}")
        hidden = len(assets["items"]) - TOP_ASSETS_LISTED
        if hidden > 0:
            lines.append(f"- (+{hidden} more assets)")

    speech = profile["speech"]
    lines += ["", "## Speech"]
    if speech["speech_ratio"] is None:
        lines.append("- No speech/transcript timing indexed yet.")
    else:
        lines.append(
            f"- ~{round(speech['speech_ratio'] * 100)}% of asset time carries speech/transcript "
            f"(basis: {speech['basis'].replace('_', ' ')})"
        )

    segments = profile["segments"]
    lines += ["", "## Segments"]
    if not segments["count"]:
        lines.append("- No segments indexed yet.")
    else:
        distribution = segments["duration_distribution"]
        lines.append(
            f"- {segments['usable_count']} usable of {segments['count']} total; durations "
            f"{distribution['min_sec']}s min / {distribution['median_sec']}s median / {distribution['max_sec']}s max"
        )

    quotes = profile["quotable_lines"]
    lines += ["", "## Quotable lines (verbatim)"]
    if not quotes:
        lines.append("- none indexed yet")
    for quote in quotes:
        lines.append(f'- "{quote["text"]}" [{quote["file_name"]} {quote["start_sec"]:.2f}-{quote["end_sec"]:.2f}]')

    facets = profile["facets"]
    lines += ["", f"## Facet observations ({facets['observation_count']} total)"]
    if not facets["by_type"]:
        lines.append("- No facet observations yet (run `ave facets`).")
    for facet, data in facets["by_type"].items():
        terms = ", ".join(f"{entry['term']} ({entry['count']})" for entry in data["top_terms"])
        lines.append(f"- {facet} ({data['count']}): {terms or 'no terms'}")

    for title, key, empty in (
        ("Strong openers (existing scores)", "openers", "none tagged hook"),
        ("Strong enders (existing scores)", "enders", "none tagged payoff/conclusion/climax"),
    ):
        lines += ["", f"## {title}"]
        candidates = profile[key]
        if not candidates:
            lines.append(f"- {empty}")
        for candidate in candidates:
            start, end = candidate["time_range"]
            lines.append(
                f"- {candidate['file_name']} {start:.1f}-{end:.1f} "
                f"[{', '.join(candidate['roles'])}, score {candidate['score']}]: {candidate['summary']}"
            )

    clusters = profile["clusters"]
    lines += ["", "## Repetition clusters"]
    if not clusters["cluster_count"]:
        lines.append("- none detected")
    else:
        edges = ", ".join(f"{name}: {count}" for name, count in clusters["edge_counts"].items())
        lines.append(f"- {clusters['cluster_count']} cluster(s); edges — {edges}")
        for cluster in clusters["largest_clusters"]:
            lines.append(f"- cluster of {cluster['size']}: {', '.join(cluster['segment_ids'][:6])}")

    return "\n".join(lines)


def _ratio(part: float, whole: float) -> float | None:
    if whole <= 0:
        return None
    return round(min(1.0, part / whole), 3)


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
