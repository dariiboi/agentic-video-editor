from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now


CONTEXT_PROMPT = """
You are building editorial memory for an autonomous video editor.
Return JSON only:
{
  "collection_summary": {
    "summary": string,
    "themes": [string],
    "visual_styles": [string],
    "recurring_people": [string],
    "recurring_settings": [string],
    "story_arc": [string]
  },
  "cards": [
    {
      "segment_id": string,
      "local_meaning": string,
      "corpus_meaning": string,
      "editorial_use": string,
      "avoid_pairing_notes": string,
      "relationship_notes": string,
      "warnings": [string],
      "captions": [string]
    }
  ]
}

Rules:
- Do not quote song lyrics verbatim.
- Explain how each segment can work inside a cut, not just what it depicts.
- Mention redundancy, weak endings, abrupt audio, or missing context when relevant.
- Keep captions short, contextual, and suitable as documentary-style overlays.
"""


@dataclass(frozen=True)
class ContextBuildSummary:
    source: str
    segments_seen: int
    collection_summaries: int
    material_bank_items: int
    context_cards: int
    caption_options: int


def build_editorial_context(
    project: Project,
    *,
    provider_name: str = "mock",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
) -> ContextBuildSummary:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        segments = _load_segments(conn)
        relationships = _load_relationships(conn)

    model_payload: dict[str, Any] = {}
    if provider_name == "gemini" and segments:
        provider = provider_for_name(provider_name, model=model, env_path=env_path)
        model_payload = provider.generate_text_json(_context_prompt(segments, relationships))

    summary = _collection_summary(segments, model_payload.get("collection_summary"))
    material_items = _material_bank_items(segments, summary)
    cards = _context_cards(segments, relationships, summary, model_payload.get("cards"))

    with connect_db(project.db_path) as conn:
        migrate(conn)
        _replace_context(conn, provider_name, summary, material_items, cards)

    return ContextBuildSummary(
        source=provider_name,
        segments_seen=len(segments),
        collection_summaries=1 if segments else 0,
        material_bank_items=len(material_items),
        context_cards=len(cards),
        caption_options=sum(len(card["captions"]) for card in cards),
    )


def context_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        summary_rows = conn.execute(
            """
            select source, summary_json, updated_at
            from collection_summaries
            where project_id = ?
            order by updated_at desc
            """,
            ("default",),
        ).fetchall()
        source_rows = conn.execute(
            """
            select source, count(*) as cards
            from editorial_context_cards
            where project_id = ?
            group by source
            order by source
            """,
            ("default",),
        ).fetchall()
        material_rows = conn.execute(
            """
            select item_type, count(*) as count
            from material_bank_items
            where project_id = ?
            group by item_type
            order by item_type
            """,
            ("default",),
        ).fetchall()
        captions = conn.execute(
            "select count(*) as count from caption_options where project_id = ?",
            ("default",),
        ).fetchone()

    summaries = []
    for row in summary_rows:
        payload = _json_object(row["summary_json"])
        payload["source"] = row["source"]
        payload["updated_at"] = row["updated_at"]
        summaries.append(payload)
    return {
        "summaries": summaries,
        "context_cards_by_source": {row["source"]: row["cards"] for row in source_rows},
        "material_bank": {row["item_type"]: row["count"] for row in material_rows},
        "caption_options": captions["count"],
    }


def _load_segments(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            segments.id,
            segments.asset_id,
            assets.file_name,
            segments.start_sec,
            segments.end_sec,
            segments.kind,
            segments.summary,
            segments.transcript_summary,
            segments.people_json,
            segments.actions_json,
            segments.moods_json,
            segments.story_roles_json,
            segments.quality_score,
            segments.usable,
            selects.id as select_id,
            selects.suggested_role,
            selects.score as select_score,
            selects.reason as select_reason
        from segments
        join assets on assets.id = segments.asset_id
        left join selects on selects.segment_id = segments.id
        where segments.project_id = ?
        order by assets.file_name, segments.start_sec
        """,
        ("default",),
    ).fetchall()
    segments = []
    for row in rows:
        item = dict(row)
        item["people"] = _json_list(item.pop("people_json"))
        item["actions"] = _json_list(item.pop("actions_json"))
        item["moods"] = _json_list(item.pop("moods_json"))
        item["story_roles"] = _json_list(item.pop("story_roles_json"))
        segments.append(item)
    return segments


def _load_relationships(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            from_entity_id,
            to_entity_id,
            relationship_type,
            confidence,
            evidence
        from relationships
        where project_id = ?
          and from_entity_type = 'segment'
          and to_entity_type = 'segment'
        """,
        ("default",),
    ).fetchall()
    return [dict(row) for row in rows]


def _context_prompt(segments: list[dict[str, Any]], relationships: list[dict[str, Any]]) -> str:
    compact_segments = [
        {
            "segment_id": segment["id"],
            "file_name": segment["file_name"],
            "time": [segment["start_sec"], segment["end_sec"]],
            "summary": segment["summary"],
            "transcript_summary": segment.get("transcript_summary"),
            "roles": segment["story_roles"],
            "moods": segment["moods"],
            "select_reason": segment.get("select_reason"),
        }
        for segment in segments
    ]
    compact_relationships = relationships[:200]
    return (
        CONTEXT_PROMPT
        + "\nSegments:\n"
        + json.dumps(compact_segments, ensure_ascii=True)
        + "\nRelationships:\n"
        + json.dumps(compact_relationships, ensure_ascii=True)
    )


def _collection_summary(segments: list[dict[str, Any]], model_summary: Any) -> dict[str, Any]:
    role_counts = Counter(_norm(role) for segment in segments for role in segment["story_roles"])
    mood_counts = Counter(_norm(mood) for segment in segments for mood in segment["moods"])
    action_counts = Counter(_norm(action) for segment in segments for action in segment["actions"])
    people_counts = Counter(_norm(person) for segment in segments for person in segment["people"])
    file_terms = Counter()
    for segment in segments:
        for token in _tokens(segment["file_name"]):
            if token not in {"mp4", "official", "video"}:
                file_terms[token] += 1

    summary = {
        "summary": _summary_sentence(segments, role_counts, mood_counts),
        "themes": _top(role_counts + mood_counts + action_counts, 10),
        "visual_styles": _top(action_counts, 8),
        "recurring_people": _top(people_counts, 8),
        "recurring_settings": _top(file_terms, 8),
        "story_arc": _story_arc(role_counts),
        "segment_count": len(segments),
    }
    if isinstance(model_summary, dict):
        for key in ["summary", "themes", "visual_styles", "recurring_people", "recurring_settings", "story_arc"]:
            value = model_summary.get(key)
            if isinstance(value, str) and value.strip():
                summary[key] = value.strip()
            elif isinstance(value, list) and value:
                summary[key] = [str(item) for item in value if str(item).strip()]
    return summary


def _material_bank_items(segments: list[dict[str, Any]], summary: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item_type, names in [
        ("theme", summary.get("themes", [])),
        ("visual_style", summary.get("visual_styles", [])),
        ("person", summary.get("recurring_people", [])),
        ("setting", summary.get("recurring_settings", [])),
        ("story_arc", summary.get("story_arc", [])),
    ]:
        for name in names:
            evidence = _evidence_for_name(segments, str(name))
            items.append(
                {
                    "item_type": item_type,
                    "name": str(name),
                    "description": _material_description(item_type, str(name), evidence),
                    "evidence": evidence,
                    "confidence": min(1.0, 0.45 + 0.1 * len(evidence)),
                }
            )
    return items


def _context_cards(
    segments: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    summary: dict[str, Any],
    model_cards: Any,
) -> list[dict[str, Any]]:
    cards_by_id = {}
    if isinstance(model_cards, list):
        for card in model_cards:
            if isinstance(card, dict) and card.get("segment_id"):
                cards_by_id[str(card["segment_id"])] = card

    relationships_by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rel in relationships:
        relationships_by_segment[str(rel["from_entity_id"])].append(rel)
        relationships_by_segment[str(rel["to_entity_id"])].append(rel)

    cards = []
    for segment in segments:
        model_card = cards_by_id.get(segment["id"], {})
        related = relationships_by_segment.get(segment["id"], [])
        warnings = _warnings_for_segment(segment, related)
        captions = _captions_for_segment(segment, summary)
        card = {
            "segment_id": segment["id"],
            "local_meaning": _text_or(
                model_card.get("local_meaning"),
                segment["summary"],
            ),
            "corpus_meaning": _text_or(
                model_card.get("corpus_meaning"),
                _corpus_meaning(segment, summary),
            ),
            "editorial_use": _text_or(
                model_card.get("editorial_use"),
                _editorial_use(segment),
            ),
            "avoid_pairing_notes": _text_or(
                model_card.get("avoid_pairing_notes"),
                _avoid_pairing_notes(segment),
            ),
            "relationship_notes": _text_or(
                model_card.get("relationship_notes"),
                _relationship_notes(segment["id"], related),
            ),
            "warnings": _list_or(model_card.get("warnings"), warnings),
            "captions": _list_or(model_card.get("captions"), captions),
        }
        cards.append(card)
    return cards


def _replace_context(
    conn,
    source: str,
    summary: dict[str, Any],
    material_items: list[dict[str, Any]],
    cards: list[dict[str, Any]],
) -> None:
    now = utc_now()
    conn.execute("delete from caption_options where project_id = ? and source = ?", ("default", source))
    conn.execute("delete from editorial_context_cards where project_id = ? and source = ?", ("default", source))
    conn.execute("delete from material_bank_items where project_id = ? and source = ?", ("default", source))

    if cards:
        conn.execute(
            """
            insert into collection_summaries
                (id, project_id, source, summary_json, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(project_id, source) do update set
                summary_json = excluded.summary_json,
                updated_at = excluded.updated_at
            """,
            (
                f"collection_{uuid.uuid4().hex[:16]}",
                "default",
                source,
                json.dumps(summary, sort_keys=True),
                now,
                now,
            ),
        )

    for item in material_items:
        conn.execute(
            """
            insert into material_bank_items (
                id, project_id, item_type, name, description, evidence_json,
                confidence, source, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"material_{uuid.uuid4().hex[:16]}",
                "default",
                item["item_type"],
                item["name"],
                item["description"],
                json.dumps(item["evidence"], sort_keys=True),
                item["confidence"],
                source,
                now,
            ),
        )

    for card in cards:
        card_id = f"context_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """
            insert into editorial_context_cards (
                id, project_id, segment_id, local_meaning, corpus_meaning,
                editorial_use, avoid_pairing_notes, relationship_notes,
                warnings_json, source, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                card_id,
                "default",
                card["segment_id"],
                card["local_meaning"],
                card["corpus_meaning"],
                card["editorial_use"],
                card["avoid_pairing_notes"],
                card["relationship_notes"],
                json.dumps(card["warnings"]),
                source,
                now,
                now,
            ),
        )
        for index, caption in enumerate(card["captions"]):
            conn.execute(
                """
                insert into caption_options (
                    id, project_id, segment_id, context_card_id, caption_text,
                    caption_type, confidence, source, created_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"caption_{uuid.uuid4().hex[:16]}",
                    "default",
                    card["segment_id"],
                    card_id,
                    caption,
                    "context_overlay" if index == 0 else "alternate",
                    0.8 if index == 0 else 0.65,
                    source,
                    now,
                ),
            )


def _summary_sentence(segments: list[dict[str, Any]], roles: Counter, moods: Counter) -> str:
    if not segments:
        return "No semantic segments are available yet."
    role_text = ", ".join(_top(roles, 4)) or "general moments"
    mood_text = ", ".join(_top(moods, 3)) or "mixed moods"
    return f"The corpus contains {len(segments)} editorial segments centered on {role_text}, with recurring {mood_text} material."


def _story_arc(roles: Counter) -> list[str]:
    ordered = []
    for role in ["hook", "context", "archive", "process", "performance", "emotion", "reaction", "payoff"]:
        if roles.get(role):
            ordered.append(role)
    return ordered or _top(roles, 6)


def _corpus_meaning(segment: dict[str, Any], summary: dict[str, Any]) -> str:
    roles = ", ".join(segment["story_roles"] or [segment.get("suggested_role") or "candidate"])
    themes = ", ".join(summary.get("themes", [])[:3])
    if themes:
        return f"Within the corpus, this works as {roles} material connected to {themes}."
    return f"Within the corpus, this works as {roles} material."


def _editorial_use(segment: dict[str, Any]) -> str:
    roles = {_norm(role) for role in segment["story_roles"]}
    role = _norm(segment.get("suggested_role")) or "candidate"
    if "hook" in roles or role == "hook":
        return "Use early to orient the viewer and make the subject feel immediate."
    if "context" in roles or "archive" in roles:
        return "Use before performance-heavy clips to create documentary context."
    if "process" in roles:
        return "Use as connective tissue showing how the music or performance is made."
    if "payoff" in roles:
        return "Save for the final third or ending so the sequence lands with resolution."
    if "emotion" in roles:
        return "Use after context is established to deepen the emotional stakes."
    return "Use as supporting texture when it adds variety or clarifies the directive."


def _avoid_pairing_notes(segment: dict[str, Any]) -> str:
    roles = {_norm(role) for role in segment["story_roles"]}
    if "payoff" in roles:
        return "Avoid placing too early; it can flatten the ending if spent before the final beat."
    if "hook" in roles:
        return "Avoid stacking several similar hooks before the viewer gets new information."
    if "performance" in roles and "context" not in roles:
        return "Avoid pairing with another performance clip from the same asset unless the transition has a clear escalation."
    return "Avoid adjacent clips that repeat the same source, framing, or emotional function."


def _relationship_notes(segment_id: str, relationships: list[dict[str, Any]]) -> str:
    if not relationships:
        return "No explicit segment relationships are stored yet."
    descriptions = []
    for rel in relationships[:4]:
        other = rel["to_entity_id"] if rel["from_entity_id"] == segment_id else rel["from_entity_id"]
        descriptions.append(f"{rel['relationship_type']} with {other}: {rel.get('evidence') or 'stored relationship'}")
    return "; ".join(descriptions)


def _warnings_for_segment(segment: dict[str, Any], relationships: list[dict[str, Any]]) -> list[str]:
    warnings = []
    duration = float(segment["end_sec"] or 0) - float(segment["start_sec"] or 0)
    if duration > 30:
        warnings.append("long_segment_trim_needed")
    if segment.get("quality_score") is not None and float(segment["quality_score"]) < 0.5:
        warnings.append("weak_quality")
    if not relationships:
        warnings.append("weak_context_links")
    return warnings


def _captions_for_segment(segment: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    roles = segment["story_roles"] or [segment.get("suggested_role") or "moment"]
    clean_role = str(roles[0]).replace("_", " ").title()
    file_hint = _caption_file_hint(segment["file_name"])
    captions = [f"{clean_role}: {file_hint}"]
    if segment.get("transcript_summary"):
        captions.append(str(segment["transcript_summary"])[:96].rstrip("."))
    elif summary.get("story_arc"):
        captions.append(f"A {clean_role.lower()} beat in the larger arc")
    return captions[:2]


def _caption_file_hint(file_name: str) -> str:
    stem = Path(file_name).stem
    stem = stem.split("[")[0].strip()
    return stem[:70] or "source moment"


def _evidence_for_name(segments: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    needle = _norm(name)
    evidence = []
    for segment in segments:
        haystack = " ".join(
            [
                segment["summary"],
                segment.get("transcript_summary") or "",
                " ".join(segment["story_roles"]),
                " ".join(segment["moods"]),
                " ".join(segment["actions"]),
                segment["file_name"],
            ]
        ).lower()
        if needle and needle in haystack:
            evidence.append(
                {
                    "segment_id": segment["id"],
                    "asset_id": segment["asset_id"],
                    "time": [segment["start_sec"], segment["end_sec"]],
                    "summary": segment["summary"],
                }
            )
        if len(evidence) >= 5:
            break
    return evidence


def _material_description(item_type: str, name: str, evidence: list[dict[str, Any]]) -> str:
    if evidence:
        return f"{name} appears in {len(evidence)} representative segment(s) as {item_type} material."
    return f"{name} is inferred as {item_type} material from the collection summary."


def _json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item).strip()]
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        value = raw
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if str(value).strip():
        return [str(value)]
    return []


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        value = {}
    return value if isinstance(value, dict) else {}


def _list_or(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if cleaned:
            return cleaned
    return fallback


def _text_or(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _tokens(text: str) -> list[str]:
    token = ""
    tokens = []
    for char in text.lower():
        if char.isalnum():
            token += char
        elif token:
            tokens.append(token)
            token = ""
    if token:
        tokens.append(token)
    return [item for item in tokens if len(item) > 2]


def _top(counter: Counter, limit: int) -> list[str]:
    return [name for name, _count in counter.most_common(limit) if name]


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")
