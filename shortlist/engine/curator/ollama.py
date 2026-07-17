"""Ollama curator — free and fully local; structured output via the `format` field."""

from __future__ import annotations

import json
import time

import httpx
from loguru import logger

from shortlist.engine.curator.base import (
    CuratorError,
    ThreadLocalTokens,
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
    # Local models have no internet, so no native web search — but they CAN power the llm_web source
    # via an external search provider (Exa) that feeds results into ``complete``.
    supports_native_web_search = False
    last_tokens = ThreadLocalTokens()  # per-thread, so parallel per-user curation doesn't race

    def __init__(self, base_url: str = "http://localhost:11434", model: str = DEFAULT_MODEL, timeout: float = 300.0):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def ping(self) -> str:
        r = httpx.get(f"{self._base_url}/api/tags", timeout=10)
        r.raise_for_status()
        return f"{len(r.json().get('models', []))} models available"

    def list_models(self) -> list[str]:
        """The models pulled on this Ollama server (its /api/tags), for the setup picker."""
        r = httpx.get(f"{self._base_url}/api/tags", timeout=10)
        r.raise_for_status()
        return sorted(m["name"] for m in r.json().get("models", []) if m.get("name"))

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

    def complete(self, system: str, user: str) -> str:
        """Plain completion (no schema) — the external-search ``llm_web`` path (see base.complete).

        This is how a local model gets web-grounded picks: Exa searches, we hand it the results.
        """
        try:
            r = httpx.post(
                f"{self._base_url}/api/chat",
                json={
                    "model": self._model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "stream": False,
                },
                timeout=self._timeout,
            )
            r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("complete (ollama): {}", e)
            return ""
        body = r.json()
        self.last_tokens = (body.get("prompt_eval_count") or 0) + (body.get("eval_count") or 0)
        return body.get("message", {}).get("content") or ""
