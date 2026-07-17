from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass(frozen=True)
class GeminiProvider:
    model: str = DEFAULT_MODEL
    env_path: Path = Path(".gemini_api.env")

    def generate_text_json(self, prompt: str) -> dict[str, Any]:
        client = _build_client(self.env_path)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=[prompt],
                    config=_generation_config(),
                )
                return parse_json_response(response.text or "{}")
            except Exception as exc:
                last_error = exc
                if not _is_retryable(exc) or attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        raise RuntimeError("Gemini request failed") from last_error

    @contextmanager
    def video_session(self, video_path: Path) -> Iterator[GeminiVideoSession]:
        """Upload the video once and run any number of prompts against it.

        The Files API keeps uploads for 48h; deleting only on session exit is
        what makes a multi-prompt analysis cost tokens instead of uploads.
        """
        session = GeminiVideoSession(_build_client(self.env_path), self.model, video_path)
        try:
            yield session
        finally:
            session.close()

    def generate_video_json(self, video_path: Path, prompt: str) -> dict[str, Any]:
        with self.video_session(video_path) as session:
            return session.generate_json(prompt)


class GeminiVideoSession:
    """One uploaded video file serving many generate calls."""

    def __init__(self, client, model: str, video_path: Path) -> None:
        self._client = client
        self._model = model
        self._video_path = video_path
        self._uploaded = None

    def generate_json(self, prompt: str) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                uploaded = self._ensure_uploaded()
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=[uploaded, prompt],
                    config=_generation_config(),
                )
                return parse_json_response(response.text or "{}")
            except Exception as exc:
                last_error = exc
                if not (_is_retryable(exc) or _is_file_failure(exc)) or attempt == 2:
                    raise
                if _is_file_failure(exc):
                    # only a dead handle justifies paying for a re-upload
                    self.close()
                time.sleep(5 * (attempt + 1))
        raise RuntimeError("Gemini request failed") from last_error

    def _ensure_uploaded(self):
        if self._uploaded is None:
            uploaded = self._client.files.upload(file=self._video_path)
            self._uploaded = _wait_for_file(self._client, uploaded)
        return self._uploaded

    def close(self) -> None:
        if self._uploaded is not None:
            _delete_file_quietly(self._client, self._uploaded)
            self._uploaded = None


class MockProvider:
    def generate_text_json(self, prompt: str) -> dict[str, Any]:
        intent_payload = _mock_intent_payload(prompt)
        if intent_payload is not None:
            return intent_payload
        structure_payload = _mock_structure_payload(prompt)
        if structure_payload is not None:
            return structure_payload
        casting_payload = _mock_casting_payload(prompt)
        if casting_payload is not None:
            return casting_payload
        return {}

    @contextmanager
    def video_session(self, video_path: Path) -> Iterator[MockVideoSession]:
        yield MockVideoSession(self, video_path)

    def generate_video_json(self, video_path: Path, prompt: str) -> dict[str, Any]:
        facet_payload = _mock_facet_payload(prompt)
        if facet_payload is not None:
            return facet_payload
        name = video_path.stem
        return {
            "spans": [
                {
                    "start_sec": 0,
                    "end_sec": 10,
                    "kind": "summary",
                    "speaker": None,
                    "text": f"Opening performance or archival moment from {name}.",
                    "confidence": 0.5,
                }
            ],
            "segments": [
                {
                    "start_sec": 0,
                    "end_sec": 12,
                    "kind": "candidate_moment",
                    "summary": f"Usable opening moment from {name}.",
                    "transcript_summary": "Short performance excerpt.",
                    "word_units": [
                        {"text": "mock opening line", "start_sec": 0.4, "end_sec": 2.1, "kind": "spoken"}
                    ],
                    "people": [],
                    "actions": ["performing"],
                    "moods": ["musical"],
                    "story_roles": ["hook"],
                    "story_function": "setup",
                    "setup_questions": ["who is performing"],
                    "payoff_answers": ["the performer from the opening is revealed"],
                    "audio_affordance": "music_bed",
                    "visual_affordance": "performance",
                    "needs_caption": False,
                    "cut_notes": "in: downbeat at 0.0; out: phrase completes before 12.0",
                    "quality_score": 0.6,
                    "usable": True,
                    "select": {
                        "suggested_role": "hook",
                        "score": 0.6,
                        "trim_start_sec": 0,
                        "trim_end_sec": 12,
                        "reason": "Mock select for offline tests.",
                    },
                }
            ],
        }


# Canned facet observations keyed by the FACET: marker each facet prompt carries.
# Shaped after the t-shirt battle acceptance corpus so downstream retrieval tests
# can find casting attributes like "green t-shirt".
_MOCK_FACET_OBSERVATIONS: dict[str, list[dict[str, Any]]] = {
    "people_appearance": [
        {
            "start_sec": 0.0,
            "end_sec": 6.0,
            "person": "P1",
            "clothing": [{"garment": "t-shirt", "color": "green"}],
            "hair": "short dark hair",
            "accessories": [],
            "distinguishing_features": ["tall"],
            "evidence": "A tall performer in a green t-shirt stands center frame.",
            "confidence": 0.8,
        },
        {
            "start_sec": 4.0,
            "end_sec": 10.0,
            "person": "P2",
            "clothing": [{"garment": "t-shirt", "color": "blue"}],
            "hair": "curly hair",
            "accessories": ["wristband"],
            "distinguishing_features": [],
            "evidence": "A second person in a blue t-shirt enters from the left.",
            "confidence": 0.7,
        },
    ],
    "groups_interactions": [
        {
            "start_sec": 2.0,
            "end_sec": 9.0,
            "group_label": "green team vs blue team",
            "members": ["P1", "P2"],
            "interaction": "square off across the room",
            "tone": "competitive",
            "evidence": "P1 in green and P2 in blue face each other with arms crossed.",
            "confidence": 0.7,
        }
    ],
    "actions_events": [
        {
            "start_sec": 1.0,
            "end_sec": 2.5,
            "action": "throws ball",
            "actors": ["P1"],
            "objects": ["ball"],
            "evidence": "P1 winds up and throws a ball across frame.",
            "confidence": 0.8,
        },
        {
            "start_sec": 5.0,
            "end_sec": 6.0,
            "action": "high-fives teammate",
            "actors": ["P2"],
            "objects": [],
            "evidence": "P2 high-fives someone off-screen right.",
            "confidence": 0.7,
        },
    ],
    "setting_context": [
        {
            "start_sec": 0.0,
            "end_sec": 10.0,
            "location_type": "rehearsal room",
            "indoor_outdoor": "indoor",
            "era_cues": ["modern LED lighting"],
            "weather": None,
            "time_of_day": "unclear",
            "evidence": "Bare-walled room with LED panels and a marked floor.",
            "confidence": 0.8,
        }
    ],
    "cinematography": [
        {
            "start_sec": 0.0,
            "end_sec": 4.0,
            "shot_size": "wide",
            "angle": "eye level",
            "camera_motion": "handheld follow right",
            "composition": "subject center, negative space left",
            "lighting": "even indoor key",
            "evidence": "Wide handheld shot tracking the performer rightward.",
            "confidence": 0.8,
        },
        {
            "start_sec": 4.0,
            "end_sec": 7.0,
            "shot_size": "close_up",
            "angle": "slightly low",
            "camera_motion": "static",
            "composition": "face fills right third",
            "lighting": "even indoor key",
            "evidence": "Static close-up on the performer's face.",
            "confidence": 0.7,
        },
    ],
    "emotion_tone": [
        {
            "start_sec": 3.0,
            "end_sec": 8.0,
            "subject": "P1",
            "expression": "grin",
            "body_language": "bounces on toes",
            "mood": "excited",
            "trajectory": "builds from focused to jubilant",
            "evidence": "P1 breaks into a grin and bounces on their toes.",
            "confidence": 0.7,
        }
    ],
    "objects_text": [
        {
            "start_sec": 0.5,
            "end_sec": 3.0,
            "kind": "signage",
            "name": "wall banner",
            "text": "FIELD DAY",
            "evidence": "A banner reading 'FIELD DAY' hangs on the back wall.",
            "confidence": 0.8,
        }
    ],
    "audio_character": [
        {
            "start_sec": 0.0,
            "end_sec": 10.0,
            "balance": "mixed",
            "energy": "rising",
            "notable_sounds": ["cheering", "clapping"],
            "evidence": "Music under crowd cheers that grow louder toward the end.",
            "confidence": 0.7,
        }
    ],
}


def _mock_facet_payload(prompt: str) -> dict[str, Any] | None:
    match = re.search(r"^FACET: ([a-z_]+)$", prompt, flags=re.M)
    if not match:
        return None
    facet = match.group(1)
    if facet == "all_facets_budget":
        return {"facets": {name: [dict(item) for item in items] for name, items in _MOCK_FACET_OBSERVATIONS.items()}}
    if facet in _MOCK_FACET_OBSERVATIONS:
        return {"observations": [dict(item) for item in _MOCK_FACET_OBSERVATIONS[facet]]}
    return None


def _mock_intent_payload(prompt: str) -> dict[str, Any] | None:
    """Deterministic canned IntentAgent replies keyed off the embedded directive.

    Simple string dispatch so acceptance tests can steer operation modes,
    provenance spreads, and anchors without a live model.
    """
    if "INTENT_AGENT" not in prompt:
        return None
    match = re.search(r"^DIRECTIVE: (.*)$", prompt, flags=re.M)
    directive = (match.group(1) if match else "").strip()
    lowered = directive.lower()

    anchors = []
    open_match = re.search(r"open (?:on|with) ([^,;.]+)", lowered)
    if open_match:
        anchors.append({"description": open_match.group(1).strip(), "position": "first", "provenance": "user_explicit"})
    end_match = re.search(r"end (?:on|with) ([^,;.]+)", lowered)
    if end_match:
        anchors.append({"description": end_match.group(1).strip(), "position": "last", "provenance": "user_explicit"})

    base: dict[str, Any] = {
        "operation": {"sources": "corpus", "output": "timeline", "mode": "compose"},
        "edit_type": "custom_edit",
        "requirements": [{"text": directive or "use the footage well", "provenance": "user_explicit", "why": None}],
        "hard_constraints": {},
        "anchors": anchors,
        "conflicts": [],
        "evidence_attributes": [],
        "success_rubric": {"directive_fit": 0.6, "watchability": 0.4},
    }

    if any(marker in lowered for marker in ("every time", "each time", "supercut")):
        base["operation"]["mode"] = "enumerate"
        base["edit_type"] = "supercut"
        quoted = re.search(r"[\"']([^\"']+)[\"']", directive)
        if quoted:
            base["evidence_attributes"] = [f"spoken word '{quoted.group(1)}'"]
        return base

    if any(marker in lowered for marker in ("cut out", "remove the", "strip out")):
        base["operation"]["mode"] = "subtract"
        base["edit_type"] = "cleanup"
        base["requirements"].append(
            {
                "text": "keep everything not matched by the removal description",
                "provenance": "user_implicit",
                "why": "a subtractive ask implies the remainder is the deliverable",
            }
        )
        base["evidence_attributes"] = _mock_removal_targets(lowered)
        return base

    if lowered.startswith("revise") or "make the middle" in lowered:
        base["operation"] = {"sources": "timeline:latest", "output": "revision", "mode": "transform"}
        base["edit_type"] = "revision"
        return base

    battle = re.search(r"battle between (.+?) and (.+?)(?:$|[,.;])", lowered)
    if battle:
        faction_a, faction_b = battle.group(1).strip(), battle.group(2).strip()
        base["edit_type"] = "faction_battle"
        attributes = [faction_a, faction_b]
        if "t-shirt" in lowered:
            attributes += ["t-shirt color", "team membership"]
        attributes.append("confrontational action")
        base["evidence_attributes"] = attributes
        base["requirements"] = [
            {"text": directive, "provenance": "user_explicit", "why": None},
            {
                "text": "escalate the exchanges between the factions toward a collision",
                "provenance": "user_implicit",
                "why": "a battle implies rising stakes, not a flat sequence of turns",
            },
            {
                "text": "close on an aftermath rather than a declared winner",
                "provenance": "agent",
                "why": "no footage shows a decisive outcome; an aftermath beat is castable",
            },
        ]
        base["success_rubric"] = {
            "faction_clarity": 0.3,
            "escalation": 0.3,
            "casting_accuracy": 0.2,
            "ending": 0.2,
        }
        return base

    if "trailer" in lowered or "teaser" in lowered:
        base["edit_type"] = "teaser_trailer"
        base["requirements"].append(
            {
                "text": "open hot and end on a sting; no documentary wind-down",
                "provenance": "user_implicit",
                "why": "a trailer sells the peak, it does not resolve it",
            }
        )
        return base

    if "story" in lowered and "word" in lowered:
        base["edit_type"] = "word_story"
        base["evidence_attributes"] = ["quotable spoken lines"]
        base["requirements"].append(
            {
                "text": "the storyline must ride verbatim spoken lines, quoted with their timestamps",
                "provenance": "user_implicit",
                "why": "a story told through words needs the words themselves as the spine",
            }
        )
        return base

    if "documentary" in lowered:
        base["edit_type"] = "mini_documentary"
        return base

    if "bookend" in lowered:
        base["edit_type"] = "bookend_piece"
        return base

    if any(marker in lowered for marker in ("memory", "fading", "elegy", "tribute")):
        base["edit_type"] = "elegy"
        return base

    if "reveal" in lowered:
        base["edit_type"] = "reveal_piece"
        return base

    if "tension" in lowered:
        base["edit_type"] = "tension_build"
        return base

    if len(lowered.split()) <= 6 and not anchors:
        base["edit_type"] = "footage_first_pitch"
        base["requirements"] = [
            {"text": directive or "make something from the footage", "provenance": "user_explicit", "why": None},
            {
                "text": "pitch the strongest storyline the index supports",
                "provenance": "agent",
                "why": "the directive pins nothing; the corpus profile is the only ground",
            },
            {
                "text": "favor high-scoring openers and enders already surfaced by the profile",
                "provenance": "agent",
                "why": "existing scores are the only quality evidence available",
            },
            {
                "text": "keep the cut coherent around one visual thread",
                "provenance": "agent",
                "why": "an unpinned brief still needs a spine to avoid a shuffle feel",
            },
        ]
        return base

    return base


def _mock_removal_targets(lowered_directive: str) -> list[str]:
    """Parse the object phrases of a subtractive directive into removal targets.

    Mechanical asks (silences, dead moments) are routed by subtract.py from the
    directive text itself, so they are excluded here; only semantic targets
    ("the throwing", "the arguments") become evidence attributes.
    """
    mechanical = (
        "silence", "silences", "silent", "quiet", "dead air", "pause", "pauses",
        "dead moment", "dead moments", "dead time", "boring", "dull", "filler",
        "nothing happens", "uneventful",
    )
    targets: list[str] = []
    for marker in ("cut out", "remove", "strip out"):
        match = re.search(rf"{marker} (.+?)(?:$|[.;])", lowered_directive)
        if not match:
            continue
        for part in re.split(r",| and ", match.group(1)):
            phrase = part.strip()
            for article in ("the ", "all ", "every ", "any "):
                if phrase.startswith(article):
                    phrase = phrase[len(article):]
            phrase = phrase.strip()
            if phrase and not any(term in phrase for term in mechanical):
                targets.append(phrase)
        break
    return targets


def _mock_structure_payload(prompt: str) -> dict[str, Any] | None:
    """Deterministic canned StructureAgent replies keyed off the embedded brief.

    Dispatches on the EDIT_TYPE/MODE marker lines the structure prompt carries
    so acceptance tests can steer lanes, patterns, ramps, and motifs offline.
    """
    if "STRUCTURE_AGENT" not in prompt:
        return None
    edit_type_match = re.search(r"^EDIT_TYPE: (.*)$", prompt, flags=re.M)
    mode_match = re.search(r"^MODE: (.*)$", prompt, flags=re.M)
    duration_match = re.search(r"^TARGET DURATION_SEC: (.*)$", prompt, flags=re.M)
    edit_type = (edit_type_match.group(1) if edit_type_match else "").strip()
    mode = (mode_match.group(1) if mode_match else "compose").strip()
    try:
        duration_sec = float((duration_match.group(1) if duration_match else "60").strip())
    except ValueError:
        duration_sec = 60.0

    duration_ack = {
        "duration_sec": {
            "value": duration_sec,
            "provenance": "user_explicit",
            "why": "target duration supplied with the request",
        }
    }

    faction_a, faction_b = _mock_faction_attributes(prompt) if edit_type == "faction_battle" else ("", "")
    if edit_type == "faction_battle" and "t-shirt" not in f"{faction_a} {faction_b}":
        # generic factions: lanes derived from the brief's evidence attributes,
        # so a "red hats vs top hats" ask casts against red hats, not t-shirts
        lane_a, lane_b = faction_a.split()[0], faction_b.split()[0]
        return {
            "logline": f"{faction_a} and {faction_b} trade escalating moves until they collide.",
            "constraints_ack": duration_ack,
            "lanes": [
                {"id": lane_a, "casting_filter": f"people_appearance: {faction_a}"},
                {"id": lane_b, "casting_filter": f"people_appearance: {faction_b}"},
            ],
            "beats": [
                {"id": "g1", "function": "meet_faction", "lane": lane_a, "intensity_target": 0.35},
                {"id": "g2", "function": "meet_faction", "lane": lane_b, "intensity_target": 0.35},
                {
                    "id": "g3-g6",
                    "pattern": "alternate",
                    "lanes": [lane_a, lane_b],
                    "function": "trade_blows",
                    "intensity_target": [0.45, 0.9],
                },
                {"id": "g7", "function": "collision", "lane": None, "intensity_target": 1.0},
                {"id": "g8", "function": "aftermath", "intensity_target": 0.25},
            ],
            "ordering_constraints": [{"type": "before", "a": "g1", "b": "g7"}],
            "juxtaposition_rules": [],
            "transition_policy_hints": {"trade_blows": "hard cuts only"},
            "ending_policy": {"intent": "aftermath, not a winner card", "reserve_ending": True},
        }

    if edit_type == "faction_battle":
        return {
            "logline": "Two crews trade escalating moves until one owns the floor.",
            "constraints_ack": duration_ack,
            "lanes": [
                {"id": "green", "casting_filter": "people_appearance: green t-shirt"},
                {"id": "blue", "casting_filter": "people_appearance: blue t-shirt"},
            ],
            "beats": [
                {
                    "id": "b1",
                    "function": "meet_faction",
                    "lane": "green",
                    "intensity_target": 0.3,
                    "motif": {"slot": "m1", "occurrence": 1},
                    "visual_need": "group identity shot, green shirts together",
                },
                {
                    "id": "b2",
                    "function": "meet_faction",
                    "lane": "blue",
                    "intensity_target": 0.3,
                    "visual_need": "group identity shot, blue shirts together",
                },
                {
                    "id": "b3-b6",
                    "pattern": "alternate",
                    "lanes": ["green", "blue"],
                    "function": "escalation",
                    "intensity_target": [0.4, 0.9],
                    "visual_need": "increasingly aggressive/energetic actions",
                },
                {
                    "id": "b7",
                    "function": "collision",
                    "lane": None,
                    "intensity_target": 1.0,
                    "fill": {"shots": [2, 4], "continuity": ["setting_context"]},
                    "visual_need": "both colors in frame OR fastest cut pair",
                },
                {
                    "id": "b8",
                    "function": "aftermath",
                    "intensity_target": 0.2,
                    "motif": {"slot": "m1", "occurrence": 2, "transform": "shorter, now reads as before-the-storm"},
                },
            ],
            "ordering_constraints": [{"type": "before", "a": "b1", "b": "b7"}],
            "juxtaposition_rules": ["adjacent lane shots should match action direction or contrast energy"],
            "transition_policy_hints": {"escalation": "hard cuts only", "aftermath": "allow dissolve"},
            "ending_policy": {"intent": "aftermath, not a winner card", "reserve_ending": True},
        }

    if mode == "enumerate":
        spoken = re.search(r"spoken word '([^']+)'", prompt)
        from_query = f"spoken word '{spoken.group(1)}'" if spoken else "every matching moment"
        return {
            "logline": "Every match, in order.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {
                    "id": "e1",
                    "pattern": "enumerate",
                    "function": "occurrence",
                    "from_query": from_query,
                    "order": "chronological",
                    "cap": 12,
                    "intensity_target": 0.6,
                }
            ],
            "ordering_constraints": [],
            "juxtaposition_rules": [],
            "transition_policy_hints": {},
            "ending_policy": {"intent": "end on the last occurrence", "reserve_ending": False},
        }

    if edit_type == "teaser_trailer":
        return {
            "logline": "Flashes, a promise, a title, a sting — sell it, don't resolve it.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {"id": "t1", "function": "cold_flash", "intensity_target": 0.9, "visual_need": "the single most arresting instant"},
                {"id": "t2", "function": "whisper_of_premise", "intensity_target": 0.45, "word_need": "one intriguing line, unfinished"},
                {"id": "t3", "pattern": "repeat", "count": 3, "function": "flash_cluster", "intensity_target": [0.7, 0.95]},
                {"id": "t4", "function": "title_hit", "intensity_target": 0.5, "visual_need": "on-screen text or logo moment"},
                {"id": "t5", "function": "sting", "intensity_target": 0.9},
            ],
            "ordering_constraints": [],
            "juxtaposition_rules": ["hard contrast between adjacent flashes"],
            "transition_policy_hints": {"flash_cluster": "hard cuts only", "sting": "hard out"},
            "ending_policy": {"intent": "end on the sting, no wind-down", "reserve_ending": False},
        }

    if edit_type == "word_story":
        quotes = _mock_quotable_lines(prompt)
        first = quotes[0]["text"] if quotes else "a spoken line"
        return {
            "logline": f'A story that rides the line "{first}".',
            "constraints_ack": duration_ack,
            "word_spine": quotes,
            "lanes": [],
            "beats": [
                {
                    "id": "w1",
                    "function": "declare_thesis",
                    "intensity_target": 0.45,
                    "word_need": f'the line "{first}", heard clean',
                    "motif": {"slot": "line", "occurrence": 1},
                },
                {"id": "w2", "function": "complicate", "intensity_target": 0.6, "word_need": "a line that pushes against the thesis"},
                {
                    "id": "w3",
                    "function": "echo_the_line",
                    "intensity_target": 0.5,
                    "word_need": f'the line "{first}" returning changed',
                    "motif": {"slot": "line", "occurrence": 2, "transform": "tighter"},
                },
                {"id": "w4", "function": "resolve_in_words", "intensity_target": 0.3, "word_need": "a line that lands the ending"},
            ],
            "ordering_constraints": [{"type": "before", "a": "w1", "b": "w4"}],
            "juxtaposition_rules": [],
            "transition_policy_hints": {},
            "ending_policy": {"intent": "end on the resolving line", "reserve_ending": True},
        }

    if edit_type == "mini_documentary":
        return {
            "logline": "Ground the place, meet the person, watch the work, learn the cost, land it.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {"id": "m1", "function": "arrive_in_the_place", "intensity_target": 0.35, "visual_need": "establishing setting"},
                {"id": "m2", "function": "meet_the_person", "intensity_target": 0.4},
                {"id": "m3", "function": "the_work_itself", "intensity_target": 0.55},
                {"id": "m4", "function": "what_it_costs", "intensity_target": 0.45},
                {"id": "m5", "function": "where_it_lands", "intensity_target": 0.25},
            ],
            "ordering_constraints": [{"type": "before", "a": "m1", "b": "m5"}],
            "juxtaposition_rules": [],
            "transition_policy_hints": {},
            "ending_policy": {"intent": "resolution earned by the grounding", "reserve_ending": True},
        }

    if edit_type == "bookend_piece":
        return {
            "logline": "Plant an image, wander away, return to it changed.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {"id": "o1", "function": "plant_image", "intensity_target": 0.35, "motif": {"slot": "frame", "occurrence": 1}},
                {"id": "o2", "function": "wander", "intensity_target": 0.55},
                {"id": "o3", "function": "gather", "intensity_target": 0.65},
                {
                    "id": "o4",
                    "function": "return_image",
                    "intensity_target": 0.85,
                    "motif": {"slot": "frame", "occurrence": 2, "transform": "shorter, now a farewell"},
                },
            ],
            "ordering_constraints": [{"type": "before", "a": "o1", "b": "o4"}],
            "juxtaposition_rules": [],
            "transition_policy_hints": {},
            "ending_policy": {"intent": "close on the returned image", "reserve_ending": False},
        }

    if edit_type == "elegy":
        return {
            "logline": "Hold the light, notice the small things, let them fade, let go.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {"id": "e1", "function": "hold_the_light", "intensity_target": 0.5},
                {"id": "e2", "function": "small_details", "intensity_target": 0.6},
                {"id": "e3", "pattern": "repeat", "count": 2, "function": "the_fade", "intensity_target": [0.5, 0.3]},
                {
                    "id": "e4",
                    "function": "let_go",
                    "intensity_target": 0.1,
                    "word_need": "a vulnerable line",
                    "casting_filter": "emotion_tone: grin",
                },
            ],
            "ordering_constraints": [],
            "juxtaposition_rules": ["soften contrasts as the piece fades"],
            "transition_policy_hints": {"the_fade": "allow dissolve", "let_go": "allow a long dissolve"},
            "ending_policy": {"intent": "a long held goodbye", "reserve_ending": True},
        }

    if edit_type == "reveal_piece":
        return {
            "logline": "Show it innocent, drift, then show what it really was.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {"id": "r1", "function": "innocent_open", "intensity_target": 0.4, "visual_need": "the moment as it first reads"},
                {"id": "r2", "function": "drift", "intensity_target": 0.5},
                {
                    "id": "r3",
                    "function": "the_reveal",
                    "intensity_target": 0.75,
                    "recontextualizes": "r1",
                    "visual_need": "the evidence that reframes the opening",
                },
                {"id": "r4", "function": "sit_with_it", "intensity_target": 0.2},
            ],
            "ordering_constraints": [{"type": "before", "a": "r1", "b": "r3"}],
            "juxtaposition_rules": [],
            "transition_policy_hints": {},
            "ending_policy": {"intent": "let the reframe settle", "reserve_ending": True},
        }

    if edit_type == "tension_build":
        return {
            "logline": "One continuous tightening, no relief until it breaks.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {"id": "t1", "function": "ignition", "intensity_target": 0.3},
                {"id": "t2", "pattern": "repeat", "count": 5, "function": "tighten", "intensity_target": [0.4, 0.95]},
                {"id": "t3", "function": "detonation", "intensity_target": 1.0},
            ],
            "ordering_constraints": [{"type": "before", "a": "t1", "b": "t3"}],
            "juxtaposition_rules": ["each cut should feel closer than the last"],
            "transition_policy_hints": {"tighten": "hard cuts only"},
            "ending_policy": {"intent": "end at the break", "reserve_ending": False},
        }

    if edit_type == "footage_first_pitch":
        assets_line = re.search(r"- (\d+) ready asset\(s\), ([\d.]+)s total", prompt)
        top_term = re.search(r"- people_appearance \(\d+\): ([^(]+?) \(", prompt)
        cited = []
        if assets_line:
            cited.append(f"{assets_line.group(1)} clip(s), {assets_line.group(2)}s of footage")
        if top_term:
            cited.append(f"strongest visual thread '{top_term.group(1).strip()}'")
        return {
            "logline": f"Pitched from the index ({'; '.join(cited) or 'profile empty'}): the strongest moments, threaded as one build.",
            "constraints_ack": duration_ack,
            "lanes": [],
            "beats": [
                {"id": "p1", "function": "establish", "intensity_target": 0.4, "visual_need": "strong opener from the profile"},
                {"id": "p2", "function": "texture", "intensity_target": 0.5},
                {"id": "p3", "function": "lift", "intensity_target": 0.7},
                {"id": "p4", "function": "settle", "intensity_target": 0.3},
            ],
            "ordering_constraints": [],
            "juxtaposition_rules": [],
            "transition_policy_hints": {},
            "ending_policy": {"intent": "settle rather than peak", "reserve_ending": True},
        }

    return {
        "logline": "A single build shaped for the directive.",
        "constraints_ack": duration_ack,
        "lanes": [],
        "beats": [
            {"id": "d1", "function": "opening", "intensity_target": 0.4},
            {"id": "d2", "function": "build", "intensity_target": 0.6},
            {"id": "d3", "function": "peak", "intensity_target": 0.85},
            {"id": "d4", "function": "landing", "intensity_target": 0.3},
        ],
        "ordering_constraints": [{"type": "before", "a": "d1", "b": "d4"}],
        "juxtaposition_rules": [],
        "transition_policy_hints": {},
        "ending_policy": {"intent": "land the ending", "reserve_ending": True},
    }


def _mock_faction_attributes(prompt: str) -> tuple[str, str]:
    """First two faction phrases from the brief's evidence_attributes JSON."""
    match = re.search(r'"evidence_attributes": \[(.*?)\]', prompt, flags=re.S)
    entries = re.findall(r'"([^"]+)"', match.group(1)) if match else []
    generic = {"t-shirt color", "team membership", "confrontational action"}
    factions = [entry for entry in entries if entry not in generic]
    factions += ["side a", "side b"]
    return factions[0], factions[1]


def _mock_quotable_lines(prompt: str) -> list[dict[str, Any]]:
    """Quote-spans-back: verbatim lines from the profile's Quotable lines section."""
    matches = re.findall(r'^- "(.+)" \[(\S+) ([\d.]+)-([\d.]+)\]$', prompt, flags=re.M)
    return [
        {"text": text, "file_name": file_name, "start_sec": float(start), "end_sec": float(end)}
        for text, file_name, start, end in matches[:4]
    ]


def _mock_casting_payload(prompt: str) -> dict[str, Any] | None:
    """Deterministic canned CastingAgent reply.

    Candidates arrive pre-filtered and pre-ranked by code (lane, withhold,
    novelty), so the mock simply picks the top-ranked candidate — the same
    behavior the deterministic bridge had, keeping offline plans sensible.
    """
    if "CASTING_AGENT" not in prompt:
        return None
    ids = re.findall(r"^- \[(\S+) \|", prompt, flags=re.M)
    if not ids:
        return {}
    function_match = re.search(r"function=(\S+)", prompt)
    function = function_match.group(1) if function_match else "the slot"
    return {
        "selected": ids[0],
        "alternates": ids[1:3],
        "why": f"top-ranked candidate satisfying the filters for {function}",
        "risks": [],
    }


class MockVideoSession:
    """Session-shaped wrapper so callers can treat mock and Gemini alike."""

    def __init__(self, provider: MockProvider, video_path: Path) -> None:
        self._provider = provider
        self._video_path = video_path

    def generate_json(self, prompt: str) -> dict[str, Any]:
        return self._provider.generate_video_json(self._video_path, prompt)


def load_gemini_api_key(env_path: Path = Path(".gemini_api.env")) -> str:
    path = env_path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Gemini env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() == "GEMINI_API_KEY":
            value = value.strip().strip("'\"")
            if value:
                return value
    raise ValueError(f"GEMINI_API_KEY was not found in {path}")


def provider_for_name(name: str, *, model: str = DEFAULT_MODEL, env_path: Path = Path(".gemini_api.env")):
    if name == "gemini":
        return GeminiProvider(model=model, env_path=env_path)
    if name == "mock":
        return MockProvider()
    raise ValueError(f"Unknown provider: {name}")


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(?P<body>.*?)```", cleaned, flags=re.S)
    if fence:
        cleaned = fence.group("body").strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model did not return valid JSON: {exc}") from exc
    if isinstance(value, list):
        return {"spans": value, "segments": value}
    if not isinstance(value, dict):
        raise ValueError("Model JSON response must be an object or list")
    return value


def _build_client(env_path: Path):
    from google import genai

    return genai.Client(api_key=load_gemini_api_key(env_path))


def _generation_config():
    from google.genai import types

    return types.GenerateContentConfig(
        temperature=0.2,
        responseMimeType="application/json",
        maxOutputTokens=8192,
    )


def _wait_for_file(client, uploaded):
    for _ in range(120):
        state = getattr(uploaded, "state", None)
        state_name = getattr(state, "name", str(state))
        if state_name in {"ACTIVE", "State.ACTIVE"}:
            return uploaded
        if state_name in {"FAILED", "State.FAILED"}:
            raise RuntimeError("Gemini file processing failed")
        if not getattr(uploaded, "name", None):
            return uploaded
        time.sleep(1)
        uploaded = client.files.get(name=uploaded.name)
    raise TimeoutError("Timed out waiting for Gemini file processing")


def _delete_file_quietly(client, uploaded) -> None:
    try:
        name = getattr(uploaded, "name", None)
        if name:
            client.files.delete(name=name)
    except Exception:
        pass


def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        fragment in text
        for fragment in [
            "503",
            "unavailable",
            "deadline",
            "temporarily",
            "did not return valid json",
        ]
    )


def _is_file_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        fragment in text
        for fragment in [
            "file processing failed",
            "not in an active state",
            "file has expired",
            "file not found",
        ]
    )
