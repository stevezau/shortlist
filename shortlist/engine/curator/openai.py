"""OpenAI curator — JSON-schema response_format on chat.completions."""

from __future__ import annotations

import json
import time

from loguru import logger

from shortlist.engine.curator.base import (
    CuratorError,
    ThreadLocalTokens,
    build_prompts,
    build_web_prompt,
    log_curate_request,
    log_curate_response,
    parse_web_titles,
    picks_schema,
    validate_picks,
)
from shortlist.engine.models import Candidate, Pick, UserProfile

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAICurator:
    name = "openai"
    supports_native_web_search = True  # Responses API web_search tool (see recommend_web)
    last_tokens = ThreadLocalTokens()  # per-thread, so parallel per-user curation doesn't race

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        try:
            import openai
        except ImportError as e:
            raise ImportError("OpenAI provider needs `pip install shortlist[openai]`") from e
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=2)
        self._model = model

    def ping(self) -> str:
        r = self._client.chat.completions.create(
            model=self._model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
        )
        return r.choices[0].message.content or ""

    def list_models(self) -> list[str]:
        """Chat-capable model ids for the setup picker. The account's model list also carries
        embeddings/tts/whisper/image models, so keep only the chat/reasoning families — falling back
        to the full list if that filter finds nothing (the free-text field still accepts anything)."""
        ids = sorted(m.id for m in self._client.models.list().data)
        chat = [m for m in ids if m.startswith(("gpt-", "chatgpt", "o1", "o3", "o4"))]
        return chat or ids

    def curate(self, profile: UserProfile, candidates: list[Candidate], k: int) -> list[Pick]:
        import openai

        system, user = build_prompts(profile, candidates, k)
        log_curate_request(self.name, self._model, system, user, len(candidates), k)
        started = time.monotonic()
        try:
            r = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "picks", "strict": True, "schema": picks_schema()},
                },
            )
        except openai.OpenAIError as e:
            raise CuratorError(f"OpenAI error: {e}") from e
        usage = getattr(r, "usage", None)
        self.last_tokens = (usage.total_tokens or 0) if usage else 0
        text = r.choices[0].message.content or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CuratorError("OpenAI returned unparseable JSON") from e
        picks = validate_picks(data.get("picks", []), candidates, k, self.name)
        log_curate_response(self.name, self._model, len(picks), self.last_tokens, time.monotonic() - started, text)
        return picks

    def recommend_web(self, profile: UserProfile, seeds: list, k: int) -> list[dict]:
        """Propose up to k titles to watch next via the Responses API web-search tool (``llm_web``).

        Returns ``[{title, year, media}]`` for the caller to resolve against TMDB. Degrades to an
        empty list on a provider error; the source's own try/except in candidates.py is the backstop
        for any other failure, so a run never fails here.
        """
        import openai

        system, user = build_web_prompt(profile, seeds, k)
        try:
            r = self._client.responses.create(
                model=self._model,
                instructions=system,
                input=user,
                tools=[{"type": "web_search"}],
            )
        except openai.OpenAIError as e:
            logger.warning("llm_web (openai): {}", e)
            return []
        usage = getattr(r, "usage", None)
        if usage is not None:
            self.last_tokens = getattr(usage, "total_tokens", 0) or 0
        return parse_web_titles(getattr(r, "output_text", "") or "", k)

    def complete(self, system: str, user: str) -> str:
        """Plain completion (no tools) — the external-search ``llm_web`` path (see base.complete)."""
        import openai

        try:
            r = self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except openai.OpenAIError as e:
            logger.warning("complete (openai): {}", e)
            return ""
        usage = getattr(r, "usage", None)
        if usage is not None:
            self.last_tokens = getattr(usage, "total_tokens", 0) or 0
        return r.choices[0].message.content or ""
