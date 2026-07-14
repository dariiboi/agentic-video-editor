from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now


CRITIQUE_PROMPT = """
Review this rough cut for an autonomous video editor. Return JSON only:
{
  "overall": string,
  "scores": {
    "story_clarity": number,
    "pacing": number,
    "directive_fit": number,
    "audio_continuity": number,
    "visual_diversity": number,
    "ending_strength": number
  },
  "what_works": [string],
  "issues": [string],
  "patch_suggestions": [
    {
      "op": "replace_item" | "trim_item" | "reorder_item" | "add_context",
      "target": string,
      "reason": string,
      "replacement_query": string
    }
  ]
}

Rules:
- Be concrete and editorial.
- Do not transcribe copyrighted lyrics.
- Focus on pacing, clarity, visual repetition, audio continuity, and whether the ending works.
"""


@dataclass(frozen=True)
class CritiqueSummary:
    review_id: str
    render_id: str
    source: str


def critique_render(
    project: Project,
    *,
    render_id: str = "latest",
    directive: str | None = None,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
) -> CritiqueSummary:
    render = _load_render(project, render_id)
    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    prompt = CRITIQUE_PROMPT
    if directive:
        prompt += f"\nDirective:\n{directive}\n"
    payload = provider.generate_video_json(Path(str(render["path"])), prompt)
    review_id = f"review_{uuid.uuid4().hex[:16]}"
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            """
            insert into reviews (
                id, project_id, render_id, directive, review_json, source, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_id,
                "default",
                render["id"],
                directive,
                json.dumps(payload, indent=2),
                provider_name,
                utc_now(),
            ),
        )
    return CritiqueSummary(review_id=review_id, render_id=str(render["id"]), source=provider_name)


def review_summary(project: Project) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        rows = conn.execute(
            """
            select id, render_id, directive, review_json, source, created_at
            from reviews
            where project_id = ?
            order by created_at desc
            """,
            ("default",),
        ).fetchall()
    reviews = []
    for row in rows:
        review = dict(row)
        try:
            review["review"] = json.loads(str(review.pop("review_json")))
        except json.JSONDecodeError:
            review["review"] = {}
        reviews.append(review)
    return {"reviews": reviews}


def _load_render(project: Project, render_id: str) -> dict[str, Any]:
    with connect_db(project.db_path) as conn:
        migrate(conn)
        if render_id == "latest":
            row = conn.execute(
                "select * from renders where project_id = ? and status = ? order by created_at desc limit 1",
                ("default", "complete"),
            ).fetchone()
        else:
            row = conn.execute(
                "select * from renders where project_id = ? and id = ?",
                ("default", render_id),
            ).fetchone()
    if row is None:
        raise FileNotFoundError("No render found to critique")
    return dict(row)
