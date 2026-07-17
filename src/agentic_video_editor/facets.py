from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now


FACET_VERSION = "v1"

BUDGET_FACET = "all_facets_budget"

# Each facet is one focused observation pass; focused prompts have far better
# per-facet recall than a kitchen-sink prompt. The upload-once session makes
# every extra facet cost tokens only.
FACET_SPECS: dict[str, dict[str, str]] = {
    "people_appearance": {
        "fields": (
            '"person": string,\n'
            '      "clothing": [{"garment": string, "color": string}],\n'
            '      "hair": string,\n'
            '      "accessories": [string],\n'
            '      "distinguishing_features": [string],'
        ),
        "rules": (
            "- Assign every distinct person a stable label (P1, P2, ...) and reuse the SAME\n"
            "  label every time that person reappears anywhere in this video.\n"
            "- One observation per person per continuous span they are visible; start a new\n"
            "  observation when they leave and re-enter frame.\n"
            "- Name every visible garment with its dominant color (e.g. green t-shirt,\n"
            "  blue jeans); colors are casting attributes, never skip them."
        ),
    },
    "groups_interactions": {
        "fields": (
            '"group_label": string,\n'
            '      "members": [string],\n'
            '      "interaction": string,\n'
            '      "tone": "cooperative" | "competitive" | "neutral",'
        ),
        "rules": (
            "- Log groupings (teams, uniforms, crowds) and what visually binds them\n"
            "  (matching shirt colors, formation, shared activity).\n"
            "- Log who interacts with whom and the concrete interaction; use person labels\n"
            "  (P1, P2, ...) when identity is obvious, otherwise describe the person.\n"
            "- tone states whether the interaction reads cooperative, competitive, or neutral."
        ),
    },
    "actions_events": {
        "fields": (
            '"action": string,\n'
            '      "actors": [string],\n'
            '      "objects": [string],'
        ),
        "rules": (
            "- One observation per granular action with a tight time range: \"throws ball\",\n"
            "  \"high-fives\", \"walks off\". Prefer many small precisely-timed actions over one\n"
            "  vague span.\n"
            "- action is a short present-tense verb phrase describing exactly what happens."
        ),
    },
    "setting_context": {
        "fields": (
            '"location_type": string,\n'
            '      "indoor_outdoor": "indoor" | "outdoor" | "mixed",\n'
            '      "era_cues": [string],\n'
            '      "weather": string | null,\n'
            '      "time_of_day": string | null,'
        ),
        "rules": (
            "- One observation per distinct setting; start a new observation when the\n"
            "  location changes.\n"
            "- era_cues are concrete visible details that date the footage (film grain,\n"
            "  clothing styles, devices, signage)."
        ),
    },
    "cinematography": {
        "fields": (
            '"shot_size": "extreme_close_up" | "close_up" | "medium" | "wide" | "extreme_wide",\n'
            '      "angle": string,\n'
            '      "camera_motion": string,\n'
            '      "composition": string,\n'
            '      "lighting": string,'
        ),
        "rules": (
            "- One observation per shot or per sustained camera state.\n"
            "- camera_motion must include direction when the camera moves (\"pans left\",\n"
            "  \"handheld follow right\", \"static\"); direction drives juxtaposition decisions."
        ),
    },
    "emotion_tone": {
        "fields": (
            '"subject": string,\n'
            '      "expression": string,\n'
            '      "body_language": string,\n'
            '      "mood": string,\n'
            '      "trajectory": string,'
        ),
        "rules": (
            "- Log visible expressions and body language per subject (person label or\n"
            "  \"crowd\") and the mood they read as.\n"
            "- trajectory states how the mood moves WITHIN the span (\"builds from focused\n"
            "  to jubilant\"); mood shifts start a new observation."
        ),
    },
    "objects_text": {
        "fields": (
            '"kind": "prop" | "on_screen_text" | "logo" | "signage",\n'
            '      "name": string,\n'
            '      "text": string | null,'
        ),
        "rules": (
            "- Log notable props, logos, signage, and every piece of legible on-screen text.\n"
            "- Quote visible text VERBATIM in \"text\"; use null when the object carries no text."
        ),
    },
    "audio_character": {
        "fields": (
            '"balance": "speech" | "music" | "ambient" | "mixed",\n'
            '      "energy": "rising" | "falling" | "steady" | "spiky",\n'
            '      "notable_sounds": [string],'
        ),
        "rules": (
            "- Describe the speech/music/ambient balance and the energy curve per span;\n"
            "  start a new observation when either changes.\n"
            "- notable_sounds are concrete audible events (cheer, whistle, crash, silence)."
        ),
    },
}

FACETS = list(FACET_SPECS)


FACET_PROMPT_TEMPLATE = """
You are the logging department of a professional edit suite, running one focused
observation pass over this footage.
FACET: {facet}
Your notes are the ONLY thing the editor will ever know about this facet: if a
signal is not written down here, the editor cannot cut on it. Watch the whole
video, log every occurrence relevant to this facet, then return JSON only:
{{
  "observations": [
    {{
      "start_sec": number,
      "end_sec": number,
      {fields}
      "evidence": string,
      "confidence": number
    }}
  ]
}}

Timing rules (most important):
- start_sec/end_sec bound the span where the observation holds, with timestamps
  at natural boundaries (pauses, shot changes, entries/exits). Never mid-word or
  mid-gesture.
- Prefer several tightly-timed observations over one vague whole-video row.

Facet rules:
{rules}

General rules:
- evidence states what a viewer literally SEES or HEARS (verbatim quotes for any
  speech or on-screen text), never an interpretation of it.
- confidence is 0-1; go below 0.5 when unsure.
- Log exhaustively for THIS facet only; ignore everything outside it.
"""


BUDGET_PROMPT_HEADER = """
You are the logging department of a professional edit suite, running a single
budget observation pass over this footage.
FACET: all_facets_budget
Your notes are the ONLY thing the editor will ever know about this footage: if a
signal is not written down here, the editor cannot cut on it. Watch the whole
video once and log observations for EVERY facet below, then return JSON only:
{
  "facets": {
"""

BUDGET_PROMPT_FOOTER = """  }
}

Every observation object needs "start_sec", "end_sec", "evidence", and
"confidence" plus the facet-specific fields shown above.

Timing rules (most important):
- start_sec/end_sec bound the span where the observation holds, with timestamps
  at natural boundaries (pauses, shot changes, entries/exits). Never mid-word or
  mid-gesture.

General rules:
- evidence states what a viewer literally SEES or HEARS (verbatim quotes for any
  speech or on-screen text), never an interpretation of it.
- confidence is 0-1; go below 0.5 when unsure.
- Fewer observations per facet than a dedicated pass is acceptable; an empty
  facet list is not, unless the footage truly has nothing for that facet.
- Assign every distinct person a stable label (P1, P2, ...) and reuse it across
  ALL facets in this response.
"""


@dataclass(frozen=True)
class FacetSummary:
    assets_requested: int
    assets_completed: int
    facets_run: int
    facets_skipped: int
    observations_created: int


def facet_analyze_project(
    project: Project,
    *,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    limit: int | None = None,
    force: bool = False,
    only: list[str] | None = None,
    budget: bool = False,
) -> FacetSummary:
    requested_facets = _validate_only(only)
    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    with connect_db(project.db_path) as conn:
        migrate(conn)
        assets = _assets_with_video_ref(conn, limit=limit)
        pending_by_asset = {
            str(asset["id"]): _pending_facets(
                conn,
                str(asset["id"]),
                requested_facets,
                provider_name=provider_name,
                budget=budget,
                force=force,
            )
            for asset in assets
        }

    totals = {"completed": 0, "run": 0, "skipped": 0, "observations": 0}
    for asset in assets:
        asset_id = str(asset["id"])
        pending = pending_by_asset[asset_id]
        skipped = (1 if budget else len(requested_facets)) - len(pending)
        totals["skipped"] += skipped
        if not pending:
            continue
        duration_sec = _float(asset.get("duration_sec"))
        # one upload serves every facet prompt for this asset
        with provider.video_session(Path(str(asset["video_ref"]))) as session:
            for facet in pending:
                if facet == BUDGET_FACET:
                    payload = session.generate_json(_budget_prompt())
                    created = _store_budget(project, asset_id, payload, _source(provider_name, BUDGET_FACET), duration_sec)
                else:
                    payload = session.generate_json(facet_prompt(facet))
                    created = _store_facet(
                        project,
                        asset_id,
                        facet,
                        _observation_items(payload),
                        _source(provider_name, facet),
                        duration_sec,
                    )
                totals["run"] += 1
                totals["observations"] += created
        totals["completed"] += 1

    return FacetSummary(
        assets_requested=len(assets),
        assets_completed=totals["completed"],
        facets_run=totals["run"],
        facets_skipped=totals["skipped"],
        observations_created=totals["observations"],
    )


def facet_prompt(facet: str) -> str:
    spec = FACET_SPECS[facet]
    return FACET_PROMPT_TEMPLATE.format(facet=facet, fields=spec["fields"], rules=spec["rules"])


def _budget_prompt() -> str:
    sections = []
    for facet, spec in FACET_SPECS.items():
        sections.append(
            f'    "{facet}": [\n'
            "      {\n"
            '        "start_sec": number,\n'
            '        "end_sec": number,\n'
            f"        {spec['fields']}\n"
            '        "evidence": string,\n'
            '        "confidence": number\n'
            "      }\n"
            "    ],"
        )
    return BUDGET_PROMPT_HEADER + "\n".join(sections) + "\n" + BUDGET_PROMPT_FOOTER


def _validate_only(only: list[str] | None) -> list[str]:
    if not only:
        return list(FACETS)
    unknown = [facet for facet in only if facet not in FACET_SPECS]
    if unknown:
        raise ValueError(f"Unknown facets: {', '.join(unknown)}. Known: {', '.join(FACETS)}")
    seen: list[str] = []
    for facet in only:
        if facet not in seen:
            seen.append(facet)
    return seen


def _source(provider_name: str, facet: str) -> str:
    return f"{provider_name}:{facet}:{FACET_VERSION}"


def _pending_facets(
    conn,
    asset_id: str,
    requested: list[str],
    *,
    provider_name: str,
    budget: bool,
    force: bool,
) -> list[str]:
    candidates = [BUDGET_FACET] if budget else requested
    if force:
        return list(candidates)
    pending = []
    for facet in candidates:
        row = conn.execute(
            "select 1 from observations where asset_id = ? and source = ? limit 1",
            (asset_id, _source(provider_name, facet)),
        ).fetchone()
        if row is None:
            pending.append(facet)
    return pending


def _assets_with_video_ref(conn, *, limit: int | None) -> list[dict[str, Any]]:
    query = """
        select
            assets.id,
            assets.path,
            assets.duration_sec,
            coalesce(proxy.path, assets.path) as video_ref
        from assets
        left join media_artifacts proxy
            on proxy.asset_id = assets.id and proxy.artifact_type = 'proxy'
        where assets.project_id = ? and assets.ingest_status = ?
        order by assets.path
    """
    params: list[Any] = ["default", "ready"]
    if limit is not None:
        query += " limit ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _observation_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("observations")
    if not isinstance(items, list):
        # parse_json_response wraps a bare top-level list as spans/segments
        items = payload.get("segments")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def observation_text(value: dict[str, Any], *, include_evidence: bool = True) -> str:
    """Flatten an observation payload to searchable text.

    Retrieval, FTS, and qmd cards all see facet evidence through this one
    rendering, so a casting attribute like "green t-shirt" is findable the
    same way everywhere.
    """
    skip = {"start_sec", "end_sec", "confidence"}
    if not include_evidence:
        skip = skip | {"evidence"}
    parts: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in skip:
                    continue
                _walk(item)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        elif isinstance(node, str):
            if node.strip():
                parts.append(node.strip())

    _walk(value)
    return " ".join(parts)


def facet_search(project: Project, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    from .transcript import _fts_query

    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select
                observations.id,
                observations.asset_id,
                assets.file_name,
                observations.observation_type,
                observations.start_sec,
                observations.end_sec,
                observations.value,
                observations.confidence,
                observations.source,
                bm25(observations_fts) as rank
            from observations_fts
            join observations on observations.id = observations_fts.observation_id
            join assets on assets.id = observations.asset_id
            where observations_fts match ?
            order by rank
            limit ?
            """,
            (_fts_query(query), limit),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        try:
            item["value"] = json.loads(item["value"])
        except (TypeError, json.JSONDecodeError):
            pass
        results.append(item)
    return results


def _delete_observations(conn, asset_id: str, source: str) -> None:
    conn.execute(
        "delete from observations_fts where observation_id in (select id from observations where asset_id = ? and source = ?)",
        (asset_id, source),
    )
    conn.execute("delete from observations where asset_id = ? and source = ?", (asset_id, source))


def _store_facet(
    project: Project,
    asset_id: str,
    facet: str,
    items: list[dict[str, Any]],
    source: str,
    duration_sec: float | None,
) -> int:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        _delete_observations(conn, asset_id, source)
        return _insert_observations(conn, asset_id, facet, items, source, duration_sec)


def _store_budget(
    project: Project,
    asset_id: str,
    payload: dict[str, Any],
    source: str,
    duration_sec: float | None,
) -> int:
    facets = payload.get("facets")
    if not isinstance(facets, dict):
        facets = {}
    with connect_db(project.db_path) as conn:
        migrate(conn)
        _delete_observations(conn, asset_id, source)
        created = 0
        for facet in FACETS:
            items = facets.get(facet)
            if not isinstance(items, list):
                continue
            created += _insert_observations(
                conn,
                asset_id,
                facet,
                [item for item in items if isinstance(item, dict)],
                source,
                duration_sec,
            )
        return created


def _insert_observations(
    conn,
    asset_id: str,
    facet: str,
    items: list[dict[str, Any]],
    source: str,
    duration_sec: float | None,
) -> int:
    created = 0
    for item in items:
        start = max(0.0, _float(item.get("start_sec")) or 0.0)
        end = max(start + 0.1, _float(item.get("end_sec")) or start + 1.0)
        if duration_sec is not None and duration_sec > 0:
            start = min(start, max(0.0, duration_sec - 0.1))
            end = min(max(start + 0.1, end), duration_sec)
        value = dict(item)
        value["start_sec"] = round(start, 3)
        value["end_sec"] = round(end, 3)
        observation_id = f"obs_{uuid.uuid4().hex[:16]}"
        conn.execute(
            """
            insert into observations (
                id, project_id, asset_id, segment_id, observation_type,
                value, confidence, source, created_at, start_sec, end_sec
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id,
                "default",
                asset_id,
                None,
                facet,
                json.dumps(value, sort_keys=True),
                _float(item.get("confidence")),
                source,
                utc_now(),
                start,
                end,
            ),
        )
        conn.execute(
            "insert into observations_fts (observation_id, asset_id, observation_type, text) values (?, ?, ?, ?)",
            (observation_id, asset_id, facet, f"{facet} {observation_text(value)}"),
        )
        created += 1
    return created


def facet_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select observation_type, source, count(*) as count
            from observations
            where project_id = ?
            group by observation_type, source
            order by observation_type, source
            """,
            ("default",),
        ).fetchall()
    by_facet: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for row in rows:
        by_facet[row["observation_type"]] = by_facet.get(row["observation_type"], 0) + row["count"]
        by_source[row["source"]] = by_source.get(row["source"], 0) + row["count"]
    return {"observations": sum(by_facet.values()), "by_facet": by_facet, "by_source": by_source}


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
