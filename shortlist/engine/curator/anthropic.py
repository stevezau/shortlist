"""Anthropic curator — web-search title discovery via Claude's web_search server tool."""

from __future__ import annotations

from loguru import logger

from shortlist.engine.curator.base import (
    ThreadLocalTokens,
    build_web_prompt,
    parse_web_titles,
)
from shortlist.engine.models import UserProfile

# Design doc §3: cheap tier is plenty for a web-search title lookup.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class AnthropicCurator:
    name = "anthropic"
    supports_native_web_search = True  # Claude's web_search server tool (see recommend_web)
    last_tokens = ThreadLocalTokens()  # per-thread, so parallel per-user web search doesn't race

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("Anthropic provider needs `pip install shortlist[anthropic]`") from e
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self._model = model

    def ping(self) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=32,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        )
        return next((b.text for b in response.content if b.type == "text"), "")

    def list_models(self) -> list[str]:
        """Model ids this key can use, newest first — populates the setup model picker."""
        return [m.id for m in self._client.models.list(limit=100).data]

    def recommend_web(self, profile: UserProfile, seeds: list, k: int) -> list[dict]:
        """Propose up to k titles to watch next via Claude's web-search tool (the ``llm_web`` source).

        Returns ``[{title, year, media}]`` for the caller to resolve against TMDB. Degrades to an
        empty list on a provider error; the source's own try/except in candidates.py is the backstop
        for any other failure (unexpected response shape, etc.), so a run never fails here.
        """
        import anthropic

        system, user = build_web_prompt(profile, seeds, k)
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
            )
        except anthropic.APIError as e:
            logger.warning("llm_web (anthropic): {}", e)
            return []
        self.last_tokens = response.usage.input_tokens + response.usage.output_tokens
        # The model may emit several text blocks around its searches; the JSON list is in the last one.
        text = "".join(b.text for b in response.content if b.type == "text")
        return parse_web_titles(text, k)

    def complete(self, system: str, user: str) -> str:
        """Plain completion (no tools) — the external-search ``llm_web`` path (see base.complete)."""
        import anthropic

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIError as e:
            logger.warning("complete (anthropic): {}", e)
            return ""
        self.last_tokens = response.usage.input_tokens + response.usage.output_tokens
        return "".join(b.text for b in response.content if b.type == "text")
