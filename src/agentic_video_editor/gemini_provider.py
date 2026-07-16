from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass(frozen=True)
class GeminiProvider:
    model: str = DEFAULT_MODEL
    env_path: Path = Path(".gemini_api.env")

    def generate_text_json(self, prompt: str) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        api_key = load_gemini_api_key(self.env_path)
        client = genai.Client(api_key=api_key)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=self.model,
                    contents=[prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        responseMimeType="application/json",
                        maxOutputTokens=8192,
                    ),
                )
                return parse_json_response(response.text or "{}")
            except Exception as exc:
                last_error = exc
                if not _is_retryable(exc) or attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
        raise RuntimeError("Gemini request failed") from last_error

    def generate_video_json(self, video_path: Path, prompt: str) -> dict[str, Any]:
        from google import genai
        from google.genai import types

        api_key = load_gemini_api_key(self.env_path)
        client = genai.Client(api_key=api_key)
        last_error: Exception | None = None
        for attempt in range(3):
            uploaded = client.files.upload(file=video_path)
            try:
                uploaded = _wait_for_file(client, uploaded)
                response = client.models.generate_content(
                    model=self.model,
                    contents=[uploaded, prompt],
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        responseMimeType="application/json",
                        maxOutputTokens=8192,
                    ),
                )
                return parse_json_response(response.text or "{}")
            except Exception as exc:
                last_error = exc
                if not _is_retryable(exc) or attempt == 2:
                    raise
                time.sleep(5 * (attempt + 1))
            finally:
                _delete_file_quietly(client, uploaded)
        raise RuntimeError("Gemini request failed") from last_error


class MockProvider:
    def generate_text_json(self, prompt: str) -> dict[str, Any]:
        del prompt
        return {}

    def generate_video_json(self, video_path: Path, prompt: str) -> dict[str, Any]:
        del prompt
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
                    "payoff_answers": [],
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
