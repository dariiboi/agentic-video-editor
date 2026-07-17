from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from .corpus_profile import corpus_profile, corpus_profile_markdown
from .db import connect_db, migrate
from .gemini_provider import DEFAULT_MODEL, provider_for_name
from .project import Project, utc_now


INTENT_VERSION = "v1"

OPERATION_MODES = {"compose", "enumerate", "subtract", "transform"}
OPERATION_OUTPUTS = {"timeline", "revision", "report"}
PROVENANCES = {"user_explicit", "user_implicit", "agent"}
ANCHOR_POSITIONS = {"first", "last", "anywhere"}


INTENT_PROMPT_TEMPLATE = """
You are the intake producer of an edit suite, translating one client directive
into a machine-readable brief. Everything downstream trusts this brief.
INTENT_AGENT
DIRECTIVE: {directive}
TARGET DURATION_SEC: {duration_sec}

What the footage index actually holds:
{profile_markdown}

Return JSON only:
{{
  "operation": {{
    "sources": "corpus" | "subset:<filter description>" | "timeline:<id>",
    "output": "timeline" | "revision" | "report",
    "mode": "compose" | "enumerate" | "subtract" | "transform"
  }},
  "edit_type": string,
  "requirements": [
    {{"text": string, "provenance": "user_explicit" | "user_implicit" | "agent", "why": string}}
  ],
  "hard_constraints": {{
    "<name>": {{"value": number_or_string, "provenance": string, "why": string}}
  }},
  "anchors": [
    {{"description": string, "position": "first" | "last" | "after:<anchor>" | "anywhere", "provenance": string}}
  ],
  "conflicts": [
    {{"between": [string, string], "nature": string, "resolution": string, "resolution_why": string}}
  ],
  "evidence_attributes": [string],
  "success_rubric": {{"<criterion>": weight}}
}}

Rules:
- operation.mode is the only closed vocabulary that machinery executes:
  compose invents and casts a structure; enumerate builds from every match of a
  pattern ("every time X happens"); subtract keeps footage minus removals
  ("cut out the boring parts"); transform revises an existing cut.
- edit_type is free-form; describe THIS directive, never force a genre label.
- provenance is the spine of the brief: user_explicit only for what the
  directive literally states; user_implicit for what it clearly implies (state
  the inference in why); agent for every choice left open (state the reason in
  why, grounded in the footage index above). Never promote your own choice to
  user_explicit.
- anchors are content the user pinned to a position ("open on...", "end
  on...", "use the line about...").
- conflicts: name every internal contradiction between requirements and every
  infeasibility against the footage index (e.g. requested duration exceeds
  usable footage). Two conflicting user_explicit entries must be surfaced with
  resolution "surface_to_user", never silently traded off.
- evidence_attributes are the concrete attributes casting must find in the
  footage (e.g. "t-shirt color", "team membership", "spoken word 'love'").
- success_rubric weights should sum to roughly 1.0.
"""


def analyze_intent(
    project: Project,
    directive: str,
    *,
    duration_sec: float = 60.0,
    provider_name: str = "gemini",
    model: str = DEFAULT_MODEL,
    env_path: Path = Path(".gemini_api.env"),
    store: bool = True,
) -> dict[str, Any]:
    """Run the IntentAgent over a directive and the corpus profile.

    Always keeps the raw provider reply (in ``raw_response``) and never lets a
    malformed reply escape as a malformed brief: validation repairs what it can
    with warnings and otherwise falls back to a minimal compose brief.
    """
    profile = corpus_profile(project)
    prompt = INTENT_PROMPT_TEMPLATE.format(
        directive=" ".join(str(directive).split()),
        duration_sec=duration_sec,
        profile_markdown=corpus_profile_markdown(profile),
    )
    provider = provider_for_name(provider_name, model=model, env_path=env_path)
    raw = provider.generate_text_json(prompt)
    analysis = _validate_intent(raw, directive=directive, duration_sec=duration_sec)
    _append_corpus_conflicts(analysis, profile, duration_sec)
    analysis["source"] = f"{provider_name}:intent_agent:{INTENT_VERSION}"
    analysis["raw_response"] = raw
    if store:
        _store_intent(project, analysis)
    return analysis


def _validate_intent(raw: Any, *, directive: str, duration_sec: float) -> dict[str, Any]:
    warnings: list[str] = []
    if not isinstance(raw, dict) or not (
        isinstance(raw.get("operation"), dict)
        or isinstance(raw.get("requirements"), list)
        or raw.get("edit_type")
    ):
        return _fallback_intent(directive, duration_sec, "intent reply malformed; using minimal compose brief")

    analysis = {
        "directive": directive,
        "duration_sec": duration_sec,
        "operation": _norm_operation(raw.get("operation"), warnings),
        "edit_type": str(raw.get("edit_type") or "unspecified_edit"),
        "requirements": _norm_requirements(raw.get("requirements"), warnings),
        "hard_constraints": _norm_constraints(raw.get("hard_constraints"), warnings),
        "anchors": _norm_anchors(raw.get("anchors"), warnings),
        "conflicts": _norm_conflicts(raw.get("conflicts"), warnings),
        "evidence_attributes": _string_list(raw.get("evidence_attributes")),
        "success_rubric": _norm_rubric(raw.get("success_rubric"), warnings),
        "fallback": False,
    }
    if not analysis["requirements"]:
        warnings.append("no requirements returned; directive kept as the only explicit requirement")
        analysis["requirements"] = [_directive_requirement(directive)]
    if "duration_sec" not in analysis["hard_constraints"]:
        analysis["hard_constraints"]["duration_sec"] = {
            "value": duration_sec,
            "provenance": "user_explicit",
            "why": "target duration supplied with the request",
        }
    analysis["validation_warnings"] = warnings
    return analysis


def _fallback_intent(directive: str, duration_sec: float, warning: str) -> dict[str, Any]:
    return {
        "directive": directive,
        "duration_sec": duration_sec,
        "operation": {"sources": "corpus", "output": "timeline", "mode": "compose"},
        "edit_type": "unspecified_edit",
        "requirements": [_directive_requirement(directive)],
        "hard_constraints": {
            "duration_sec": {
                "value": duration_sec,
                "provenance": "user_explicit",
                "why": "target duration supplied with the request",
            }
        },
        "anchors": [],
        "conflicts": [],
        "evidence_attributes": [],
        "success_rubric": {},
        "fallback": True,
        "validation_warnings": [warning],
    }


def _directive_requirement(directive: str) -> dict[str, Any]:
    return {"text": directive, "provenance": "user_explicit", "why": None}


def _norm_operation(raw: Any, warnings: list[str]) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    mode = str(raw.get("mode") or "").strip().lower()
    if mode not in OPERATION_MODES:
        if mode:
            warnings.append(f"unknown operation mode {mode!r}; defaulting to compose")
        mode = "compose"
    output = str(raw.get("output") or "").strip().lower()
    if output not in OPERATION_OUTPUTS:
        if output:
            warnings.append(f"unknown operation output {output!r}; defaulting to timeline")
        output = "timeline"
    sources = str(raw.get("sources") or "corpus").strip()
    if sources != "corpus" and not sources.startswith(("subset:", "timeline:")):
        warnings.append(f"unknown operation sources {sources!r}; defaulting to corpus")
        sources = "corpus"
    return {"sources": sources, "output": output, "mode": mode}


def _norm_requirements(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    requirements = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            warnings.append("dropped a requirement without text")
            continue
        provenance = str(item.get("provenance") or "").strip().lower()
        if provenance not in PROVENANCES:
            warnings.append(
                f"requirement {str(item['text'])[:48]!r} had invalid provenance {provenance!r}; demoted to agent"
            )
            provenance = "agent"
        why = str(item.get("why") or "").strip() or None
        if provenance != "user_explicit" and not why:
            warnings.append(f"requirement {str(item['text'])[:48]!r} ({provenance}) is missing its why")
        requirements.append({"text": str(item["text"]).strip(), "provenance": provenance, "why": why})
    return requirements


def _norm_constraints(raw: Any, warnings: list[str]) -> dict[str, dict[str, Any]]:
    constraints: dict[str, dict[str, Any]] = {}
    for name, value in (raw.items() if isinstance(raw, dict) else []):
        key = str(name).strip()
        if not key:
            continue
        if isinstance(value, dict) and "value" in value:
            provenance = str(value.get("provenance") or "").strip().lower()
            if provenance not in PROVENANCES:
                warnings.append(f"constraint {key} had invalid provenance {provenance!r}; demoted to agent")
                provenance = "agent"
            constraints[key] = {
                "value": value["value"],
                "provenance": provenance,
                "why": str(value.get("why") or "").strip() or None,
            }
        else:
            warnings.append(f"constraint {key} arrived untagged; demoted to agent provenance")
            constraints[key] = {"value": value, "provenance": "agent", "why": None}
    return constraints


def _norm_anchors(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    anchors = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("description") or "").strip():
            warnings.append("dropped an anchor without a description")
            continue
        position = str(item.get("position") or "anywhere").strip().lower()
        if position not in ANCHOR_POSITIONS and not position.startswith("after:"):
            warnings.append(f"anchor {str(item['description'])[:48]!r} had invalid position {position!r}")
            position = "anywhere"
        provenance = str(item.get("provenance") or "").strip().lower()
        if provenance not in PROVENANCES:
            provenance = "user_explicit"
        anchors.append(
            {
                "description": str(item["description"]).strip(),
                "position": position,
                "provenance": provenance,
            }
        )
    return anchors


def _norm_conflicts(raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    conflicts = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("nature") or "").strip():
            warnings.append("dropped a conflict without a stated nature")
            continue
        between = item.get("between")
        between = [str(part) for part in between][:2] if isinstance(between, list) else []
        conflicts.append(
            {
                "between": between,
                "nature": str(item["nature"]).strip(),
                "resolution": str(item.get("resolution") or "surface_to_user").strip(),
                "resolution_why": str(item.get("resolution_why") or "").strip() or None,
            }
        )
    return conflicts


def _norm_rubric(raw: Any, warnings: list[str]) -> dict[str, float]:
    rubric: dict[str, float] = {}
    for name, weight in (raw.items() if isinstance(raw, dict) else []):
        try:
            rubric[str(name)] = float(weight)
        except (TypeError, ValueError):
            warnings.append(f"dropped rubric entry {name!r} with non-numeric weight")
    return rubric


def _string_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _append_corpus_conflicts(analysis: dict[str, Any], profile: dict[str, Any], duration_sec: float) -> None:
    """Deterministic infeasibility check: validation, not a creative choice."""
    total = float(profile.get("assets", {}).get("total_duration_sec") or 0.0)
    if total <= 0 or duration_sec <= total:
        return
    if any("duration" in " ".join(conflict.get("between", [])).lower() for conflict in analysis["conflicts"]):
        return
    analysis["conflicts"].append(
        {
            "between": ["hard_constraints.duration_sec", "corpus.total_duration_sec"],
            "nature": (
                f"requested {duration_sec}s exceeds the {total}s of ready footage; "
                "the target cannot be met without repetition"
            ),
            "resolution": "surface_to_user",
            "resolution_why": "duration is user_explicit; it must not be silently reduced",
        }
    )


def _store_intent(project: Project, analysis: dict[str, Any]) -> None:
    now = utc_now()
    directive_id = f"directive_{uuid.uuid4().hex[:16]}"
    intent_id = f"intent_{uuid.uuid4().hex[:16]}"
    with connect_db(project.db_path) as conn:
        migrate(conn)
        conn.execute(
            "insert into directives (id, project_id, text, duration_sec, mode, created_at) values (?, ?, ?, ?, ?, ?)",
            (directive_id, "default", analysis["directive"], analysis["duration_sec"], "intent", now),
        )
        conn.execute(
            """
            insert into intent_analyses (
                id, project_id, directive_id, directive_text, analysis_json, source, created_at
            )
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent_id,
                "default",
                directive_id,
                analysis["directive"],
                json.dumps(analysis, sort_keys=True),
                analysis["source"],
                now,
            ),
        )
        conn.commit()
    analysis["intent_id"] = intent_id
    analysis["directive_id"] = directive_id


def intent_markdown(analysis: dict[str, Any]) -> str:
    """Human-readable rendering for the CLI's default output."""
    operation = analysis["operation"]
    lines = [
        f"Directive: {analysis['directive']}",
        f"Operation: {operation['mode']} -> {operation['output']} (sources: {operation['sources']})",
        f"Edit type: {analysis['edit_type']}",
        "",
        "Requirements:",
    ]
    for requirement in analysis["requirements"]:
        why = f" — {requirement['why']}" if requirement.get("why") else ""
        lines.append(f"- [{requirement['provenance']}] {requirement['text']}{why}")
    lines.append("")
    lines.append("Hard constraints:")
    for name, constraint in analysis["hard_constraints"].items():
        lines.append(f"- {name} = {constraint['value']} [{constraint['provenance']}]")
    if analysis["anchors"]:
        lines.append("")
        lines.append("Anchors:")
        for anchor in analysis["anchors"]:
            lines.append(f"- ({anchor['position']}) {anchor['description']} [{anchor['provenance']}]")
    if analysis["conflicts"]:
        lines.append("")
        lines.append("Conflicts:")
        for conflict in analysis["conflicts"]:
            lines.append(f"- {conflict['nature']} -> {conflict['resolution']}")
    if analysis["evidence_attributes"]:
        lines.append("")
        lines.append(f"Evidence attributes: {', '.join(analysis['evidence_attributes'])}")
    if analysis["success_rubric"]:
        rubric = ", ".join(f"{name} {weight}" for name, weight in analysis["success_rubric"].items())
        lines.append(f"Success rubric: {rubric}")
    if analysis.get("validation_warnings"):
        lines.append("")
        lines.append("Validation warnings:")
        for warning in analysis["validation_warnings"]:
            lines.append(f"- {warning}")
    return "\n".join(lines)
