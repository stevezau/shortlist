"""Anthropic curator — structured output via output_config.format on the Messages API."""

from __future__ import annotations

import json

from loguru import logger

from shortlist.engine.curator.base import (
    CuratorError,
    build_prompts,
    build_web_prompt,
    parse_web_titles,
    picks_schema,
    validate_picks,
)
from shortlist.engine.models import Candidate, Pick, UserProfile

# Design doc §3: cheap tier is plenty for re-ranking ~40 owned titles.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


class AnthropicCurator:
    name = "anthropic"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("Anthropic provider needs `pip install shortlist[anthropic]`") from e
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self._model = model
        self.last_tokens = 0

    def ping(self) -> str:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=32,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        )
        return next((b.text for b in response.content if b.type == "text"), "")

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        import anthropic

        system, user = build_prompts(profile, candidates, k)
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": picks_schema()}},
            )
        except anthropic.RateLimitError as e:
            raise CuratorError(f"Anthropic rate limited: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise CuratorError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise CuratorError("Anthropic connection error") from e

        self.last_tokens = response.usage.input_tokens + response.usage.output_tokens
        if response.stop_reason == "refusal":
            raise CuratorError("Anthropic refused the request")
        if response.stop_reason == "max_tokens":
            logger.warning("anthropic: output truncated at max_tokens; picks may be partial")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CuratorError(f"Anthropic returned unparseable JSON: {text[:200]!r}") from e
        return validate_picks(data.get("picks", []), candidates, k, self.name)

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
