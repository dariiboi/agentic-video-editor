from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .db import connect_db, migrate
from .facets import observation_text
from .project import Project


ROLE_ALIASES = {
    "archive": {"archive", "context", "character_detail"},
    "studio": {"process", "performance", "context"},
    "live": {"performance", "emotion", "payoff"},
    "emotional": {"emotion", "reaction", "payoff"},
    "payoff": {"payoff", "conclusion", "climax"},
    "ending": {"payoff", "conclusion", "climax"},
    "process": {"process", "context"},
    "story": {"hook", "context", "process", "emotion", "payoff"},
}

STOP_TERMS = {
    "a",
    "an",
    "and",
    "as",
    "for",
    "from",
    "in",
    "into",
    "make",
    "of",
    "on",
    "the",
    "to",
    "using",
    "use",
    "with",
}


def context_search(project: Project, query: str, *, limit: int = 10) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        segments = _load_search_segments(conn)
        relationships = _load_relationships(conn)

    intent = analyze_directive_intent(query)
    profile = _weight_profile(intent)
    scored = []
    for segment in segments:
        score, reasons = _score_segment(segment, query, intent, profile)
        if score > 0:
            scored.append((score, segment, reasons))
    if not scored:
        scored = [(_fallback_score(segment), segment, ["fallback high-quality select"]) for segment in segments]

    scored.sort(key=lambda item: item[0], reverse=True)
    packets = []
    used_assets: set[str] = set()
    used_roles: set[str] = set()
    for score, segment, reasons in scored[: max(limit * 2, limit)]:
        packet = _packet(segment, score, reasons, relationships, used_assets, used_roles)
        packets.append(packet)
        used_assets.add(segment["asset_id"])
        used_roles.update(_norm(role) for role in segment["story_roles"])
        if len(packets) >= limit:
            break

    return {
        "query": query,
        "intent": intent,
        "weight_profile": profile["name"],
        "packets": packets,
    }


def related_segments(project: Project, segment_id: str, *, limit: int = 10) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        segments = {segment["id"]: segment for segment in _load_search_segments(conn)}
        relationships_by_segment = _load_relationships(conn)

    related = []
    seen_relationships = set()
    for rel in relationships_by_segment.get(segment_id, []):
        key = (
            rel["from_entity_id"],
            rel["to_entity_id"],
            rel["relationship_type"],
            rel.get("evidence"),
        )
        if key in seen_relationships:
            continue
        seen_relationships.add(key)
        other_id = None
        direction = "outgoing"
        if rel["from_entity_id"] == segment_id:
            other_id = rel["to_entity_id"]
        elif rel["to_entity_id"] == segment_id:
            other_id = rel["from_entity_id"]
            direction = "incoming"
        if other_id and other_id in segments:
            segment = segments[other_id]
            related.append(
                {
                    "segment_id": other_id,
                    "asset_id": segment["asset_id"],
                    "select_id": segment.get("select_id"),
                    "file_name": segment["file_name"],
                    "time_range": [segment["start_sec"], segment["end_sec"]],
                    "relationship_type": rel["relationship_type"],
                    "direction": direction,
                    "confidence": rel.get("confidence"),
                    "evidence": rel.get("evidence"),
                    "summary": segment["summary"],
                    "why_related": _why_related(rel, segment),
                }
            )
    related.sort(key=lambda item: float(item["confidence"] or 0), reverse=True)
    return {"segment_id": segment_id, "related": related[:limit]}


def analyze_directive_intent(directive: str) -> dict[str, Any]:
    terms = set(_directive_terms(directive))
    desired_roles: list[str] = []
    for term in terms:
        desired_roles.extend(sorted(ROLE_ALIASES.get(term, set())))
    for role in ["hook", "context", "process", "performance", "emotion", "payoff"]:
        if role in terms:
            desired_roles.append(role)
    desired_roles = _dedupe(desired_roles)
    if not desired_roles:
        desired_roles = ["hook", "context", "performance", "emotion", "payoff"]

    tone = []
    for word in ["emotional", "high_energy", "intimate", "documentary", "archive", "musical"]:
        if word in terms or word.replace("_", "") in terms:
            tone.append(word)
    implicit = []
    if "documentary" in terms or "mini" in terms:
        implicit.extend(["establish context before performance", "avoid unexplained clip pileups"])
    if "payoff" in terms or "ending" in terms:
        implicit.append("reserve the strongest resolution for the final beat")
    if "archive" in terms and ("live" in terms or "studio" in terms):
        implicit.append("bridge eras with captions or transition notes")

    return {
        "explicit_terms": sorted(terms),
        "desired_story_roles": desired_roles,
        "tone": tone,
        "implicit_goals": _dedupe(implicit),
    }


def _load_search_segments(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            segments.id,
            segments.asset_id,
            assets.file_name,
            assets.has_audio,
            assets.duration_sec as asset_duration_sec,
            segments.start_sec,
            segments.end_sec,
            segments.summary,
            segments.transcript_summary,
            segments.people_json,
            segments.actions_json,
            segments.moods_json,
            segments.story_roles_json,
            segments.word_units_json,
            segments.story_function,
            segments.setup_questions_json,
            segments.payoff_answers_json,
            segments.audio_affordance,
            segments.visual_affordance,
            segments.needs_caption,
            segments.cut_notes,
            segments.quality_score,
            selects.id as select_id,
            selects.suggested_role,
            selects.score as select_score,
            selects.trim_start_sec,
            selects.trim_end_sec,
            selects.reason as select_reason,
            editorial_context_cards.id as context_card_id,
            editorial_context_cards.local_meaning,
            editorial_context_cards.corpus_meaning,
            editorial_context_cards.editorial_use,
            editorial_context_cards.avoid_pairing_notes,
            editorial_context_cards.relationship_notes,
            editorial_context_cards.warnings_json,
            captions.caption_text
        from segments
        join assets on assets.id = segments.asset_id
        left join selects on selects.segment_id = segments.id
        left join editorial_context_cards
            on editorial_context_cards.segment_id = segments.id
            and editorial_context_cards.source = (
                select source
                from editorial_context_cards latest
                where latest.project_id = segments.project_id
                order by latest.updated_at desc
                limit 1
            )
        left join caption_options captions
            on captions.segment_id = segments.id
            and captions.context_card_id = editorial_context_cards.id
            and captions.caption_type = 'context_overlay'
        where segments.project_id = ?
          and segments.usable = 1
          and segments.start_sec < coalesce(assets.duration_sec, segments.end_sec)
        order by coalesce(selects.score, segments.quality_score, 0) desc, segments.start_sec
        """,
        ("default",),
    ).fetchall()
    segments = [_row(row) for row in rows]
    _attach_facets(conn, segments)
    return segments


def _attach_facets(conn, segments: list[dict[str, Any]]) -> None:
    """Attach facet observations overlapping each segment's time range.

    Facet time ranges are approximate, so overlap matching rather than
    containment. The observation text joins the searchable haystack and is
    citable in packet evidence.
    """
    rows = conn.execute(
        """
        select asset_id, observation_type, start_sec, end_sec, value
        from observations
        where project_id = ? and start_sec is not null and end_sec is not null
        order by asset_id, start_sec
        """,
        ("default",),
    ).fetchall()
    by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        item = dict(row)
        try:
            value = json.loads(item["value"])
        except (TypeError, json.JSONDecodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        item["text"] = observation_text(value, include_evidence=False)
        item["evidence"] = str(value.get("evidence") or "")
        by_asset[item["asset_id"]].append(item)
    for segment in segments:
        segment["facets"] = [
            {
                "observation_type": obs["observation_type"],
                "time_range": [obs["start_sec"], obs["end_sec"]],
                "text": obs["text"],
                "evidence": obs["evidence"],
            }
            for obs in by_asset.get(segment["asset_id"], [])
            if obs["start_sec"] < segment["end_sec"] and segment["start_sec"] < obs["end_sec"]
        ]


def _load_relationships(conn) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """
        select from_entity_id, to_entity_id, relationship_type, confidence, evidence
        from relationships
        where project_id = ?
          and from_entity_type = 'segment'
          and to_entity_type = 'segment'
        """,
        ("default",),
    ).fetchall()
    relationships: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rel = dict(row)
        relationships[rel["from_entity_id"]].append(rel)
        relationships[rel["to_entity_id"]].append(rel)
    return relationships


def _row(row) -> dict[str, Any]:
    item = dict(row)
    for key in [
        "people_json",
        "actions_json",
        "moods_json",
        "story_roles_json",
        "warnings_json",
        "setup_questions_json",
        "payoff_answers_json",
    ]:
        clean_key = key.replace("_json", "")
        item[clean_key] = _json_list(item.pop(key))
    try:
        word_units = json.loads(item.pop("word_units_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        word_units = []
    item["word_units"] = word_units if isinstance(word_units, list) else []
    return item


WORD_DRIVEN_TERMS = {"words", "word", "story", "lyric", "lyrics", "spoken", "dialogue", "quote", "quotes", "talking", "says", "line", "lines", "interview"}

VISUAL_DRIVEN_TERMS = {"montage", "visual", "visuals", "cinematic", "energy", "broll", "b_roll"}

WEIGHT_PROFILES = {
    "default": {"name": "default", "term": 2.0, "role_base": 3.0, "card": 1.5, "quality_cap": 2.0, "transcript_bonus": 0.0},
    "word_driven": {"name": "word_driven", "term": 2.5, "role_base": 2.0, "card": 1.5, "quality_cap": 1.5, "transcript_bonus": 1.5},
    "visual_driven": {"name": "visual_driven", "term": 2.0, "role_base": 4.0, "card": 1.5, "quality_cap": 2.5, "transcript_bonus": 0.0},
}


def _weight_profile(intent: dict[str, Any]) -> dict[str, Any]:
    terms = {_norm(term) for term in intent.get("explicit_terms") or []}
    if terms & WORD_DRIVEN_TERMS:
        return WEIGHT_PROFILES["word_driven"]
    if terms & VISUAL_DRIVEN_TERMS:
        return WEIGHT_PROFILES["visual_driven"]
    return WEIGHT_PROFILES["default"]


def _score_segment(
    segment: dict[str, Any],
    query: str,
    intent: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> tuple[float, list[str]]:
    profile = profile or WEIGHT_PROFILES["default"]
    query_terms = _directive_terms(query)
    text = _haystack(segment)
    roles = {_norm(role) for role in segment["story_roles"]}
    score = 0.0
    reasons = []

    hits = [term for term in query_terms if term and term in text]
    if hits:
        score += len(hits) * profile["term"]
        reasons.append(f"matches directive terms: {', '.join(_dedupe(hits)[:5])}")

    if profile["transcript_bonus"]:
        transcript_text = _transcript_haystack(segment)
        transcript_hits = [term for term in query_terms if term and term in transcript_text]
        if transcript_hits:
            score += min(3.0, len(transcript_hits) * profile["transcript_bonus"])
            reasons.append(f"directive terms land in spoken words: {', '.join(_dedupe(transcript_hits)[:4])}")

    desired_roles = {_norm(role) for role in intent.get("desired_story_roles", [])}
    role_hits = sorted(roles & desired_roles)
    if role_hits:
        score += profile["role_base"] + len(role_hits)
        reasons.append(f"story-role fit: {', '.join(role_hits)}")

    facet_text = _facet_haystack(segment)
    facet_hits = [term for term in query_terms if term and term in facet_text]
    if facet_hits:
        # already scored through the haystack; the reason makes the facet
        # evidence citable in why_matches
        reasons.append(f"facet observations match: {', '.join(_dedupe(facet_hits)[:4])}")

    if segment.get("context_card_id"):
        score += profile["card"]
        reasons.append("has editorial context card")

    quality = segment.get("select_score") or segment.get("quality_score") or 0
    try:
        score += min(profile["quality_cap"], float(quality) / 5.0)
    except (TypeError, ValueError):
        pass

    if "payoff" in roles and any(goal.startswith("reserve") for goal in intent.get("implicit_goals", [])):
        score += 1.0
        reasons.append("supports requested ending payoff")

    return score, reasons


def _facet_haystack(segment: dict[str, Any]) -> str:
    parts = []
    for obs in segment.get("facets") or []:
        parts.append(str(obs.get("text") or ""))
        parts.append(str(obs.get("evidence") or ""))
    return " ".join(part.lower() for part in parts if part)


def _transcript_haystack(segment: dict[str, Any]) -> str:
    parts = [
        segment.get("transcript_summary"),
        " ".join(str(unit.get("text") or "") for unit in segment.get("word_units") or [] if isinstance(unit, dict)),
    ]
    return " ".join(str(part or "").lower() for part in parts)


def _fallback_score(segment: dict[str, Any]) -> float:
    value = segment.get("select_score") or segment.get("quality_score") or 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _packet(
    segment: dict[str, Any],
    score: float,
    reasons: list[str],
    relationships: dict[str, list[dict[str, Any]]],
    used_assets: set[str],
    used_roles: set[str],
) -> dict[str, Any]:
    rels = relationships.get(segment["id"], [])
    roles = [_norm(role) for role in segment["story_roles"]]
    warnings = _json_list(segment.get("warnings"))
    if segment["asset_id"] in used_assets:
        warnings.append("duplicate_source_asset")
    if not segment.get("has_audio"):
        warnings.append("abrupt_audio_or_no_audio")
    elif "performance" in roles:
        warnings.append("check_audio_transition")
    if not segment.get("context_card_id"):
        warnings.append("weak_context")
    if "payoff" not in roles and "payoff" not in used_roles:
        warnings.append("weak_ending_if_used_last")

    return {
        "segment_id": segment["id"],
        "select_id": segment.get("select_id"),
        "asset_id": segment["asset_id"],
        "file_name": segment["file_name"],
        "asset_duration_sec": segment.get("asset_duration_sec"),
        "time_range": [segment["start_sec"], segment["end_sec"]],
        "trim_range": [segment.get("trim_start_sec") or segment["start_sec"], segment.get("trim_end_sec") or segment["end_sec"]],
        "score": round(score, 3),
        "story_roles": segment["story_roles"],
        "source_evidence": {
            "summary": segment["summary"],
            "transcript_summary": segment.get("transcript_summary"),
            "select_reason": segment.get("select_reason"),
            "context": segment.get("corpus_meaning"),
            "word_units": segment.get("word_units") or [],
            "story_function": segment.get("story_function"),
            "audio_affordance": segment.get("audio_affordance"),
            "visual_affordance": segment.get("visual_affordance"),
            "needs_caption": segment.get("needs_caption"),
            "cut_notes": segment.get("cut_notes"),
            "facets": (segment.get("facets") or [])[:12],
        },
        "why_matches": reasons or ["high-scoring candidate from semantic analysis"],
        "why_belongs_before_after": _placement_hint(segment),
        "relationship_expansion": [_relationship_packet(segment["id"], rel) for rel in rels[:5]],
        "caption_text": segment.get("caption_text"),
        "continuity_compatibility": _continuity_score(segment, rels, used_assets, used_roles),
        "warnings": _dedupe(warnings),
    }


def _placement_hint(segment: dict[str, Any]) -> str:
    roles = {_norm(role) for role in segment["story_roles"]}
    if "payoff" in roles:
        return "Belongs late, ideally after a clip that establishes context or escalation."
    if "hook" in roles:
        return "Belongs before context or process clips as an opening signal."
    if "context" in roles or "archive" in roles:
        return "Belongs before emotional/performance payoff so the viewer understands the stakes."
    if "process" in roles:
        return "Belongs after context and before higher-energy performance clips."
    if "emotion" in roles:
        return "Belongs after setup and before the final payoff."
    return "Use between unlike clips when it improves visual or story variety."


def _relationship_packet(segment_id: str, rel: dict[str, Any]) -> dict[str, Any]:
    other_id = rel["to_entity_id"] if rel["from_entity_id"] == segment_id else rel["from_entity_id"]
    return {
        "segment_id": other_id,
        "relationship_type": rel["relationship_type"],
        "confidence": rel.get("confidence"),
        "evidence": rel.get("evidence"),
    }


def _continuity_score(
    segment: dict[str, Any],
    relationships: list[dict[str, Any]],
    used_assets: set[str],
    used_roles: set[str],
) -> float:
    score = 0.65
    roles = {_norm(role) for role in segment["story_roles"]}
    if relationships:
        score += 0.15
    if segment["asset_id"] in used_assets:
        score -= 0.2
    if roles & used_roles:
        score -= 0.1
    if "payoff" in roles:
        score += 0.1
    return round(max(0.0, min(1.0, score)), 3)


def _why_related(rel: dict[str, Any], segment: dict[str, Any]) -> str:
    evidence = rel.get("evidence") or segment["summary"]
    return f"{rel['relationship_type']}: {evidence}"


def _haystack(segment: dict[str, Any]) -> str:
    parts = [
        segment.get("file_name"),
        segment.get("summary"),
        segment.get("transcript_summary"),
        segment.get("local_meaning"),
        segment.get("corpus_meaning"),
        segment.get("editorial_use"),
        segment.get("select_reason"),
        segment.get("story_function"),
        segment.get("audio_affordance"),
        segment.get("visual_affordance"),
        " ".join(str(unit.get("text") or "") for unit in segment.get("word_units") or [] if isinstance(unit, dict)),
        " ".join(segment.get("setup_questions") or []),
        " ".join(segment.get("payoff_answers") or []),
        " ".join(segment.get("story_roles") or []),
        " ".join(segment.get("actions") or []),
        " ".join(segment.get("moods") or []),
        " ".join(segment.get("people") or []),
        _facet_haystack(segment),
    ]
    return " ".join(str(part or "").lower().replace(" ", "_") for part in parts)


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


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = _norm(value)
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _norm(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _directive_terms(text: str) -> list[str]:
    terms = []
    token = ""
    for char in text.lower():
        if char.isalnum():
            token += char
        elif token:
            if len(token) > 1 and token not in STOP_TERMS:
                terms.append(_norm(token))
            token = ""
    if token and len(token) > 1 and token not in STOP_TERMS:
        terms.append(_norm(token))
    return _dedupe(terms)
