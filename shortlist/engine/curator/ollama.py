"""Ollama curator — free and fully local; structured output via the `format` field."""

from __future__ import annotations

import json
import time

import httpx

from shortlist.engine.curator.base import (
    CuratorError,
    build_prompts,
    log_curate_request,
    log_curate_response,
    picks_schema,
    validate_picks,
)
from shortlist.engine.models import Candidate, Pick, UserProfile

DEFAULT_MODEL = "llama3.1"


class OllamaCurator:
    name = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434", model: str = DEFAULT_MODEL, timeout: float = 300.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self.last_tokens = 0

    def ping(self) -> str:
        r = httpx.get(f"{self._base_url}/api/tags", timeout=10)
        r.raise_for_status()
        return f"{len(r.json().get('models', []))} models available"

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        system, user = build_prompts(profile, candidates, k)
        log_curate_request(self.name, self._model, system, user, len(candidates), k)
        started = time.monotonic()
        try:
            r = httpx.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "format": picks_schema(),
                    "stream": False,
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise CuratorError(f"Ollama error: {e}") from e
        body = r.json()
        self.last_tokens = (body.get("prompt_eval_count") or 0) + (body.get("eval_count") or 0)
        text = body.get("message", {}).get("content") or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CuratorError("Ollama returned unparseable JSON") from e
        picks = validate_picks(data.get("picks", []), candidates, k, self.name)
        log_curate_response(self.name, self._model, len(picks), self.last_tokens, time.monotonic() - started, text)
        return picks
