"""OpenAI curator — web-search title discovery via the Responses API web_search tool."""

from __future__ import annotations

from loguru import logger

from shortlist.engine.curator.base import (
    ThreadLocalTokens,
    build_web_prompt,
    parse_web_titles,
)
from shortlist.engine.models import UserProfile

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAICurator:
    name = "openai"
    supports_native_web_search = True  # Responses API web_search tool (see recommend_web)
    last_tokens = ThreadLocalTokens()  # per-thread, so parallel per-user web search doesn't race

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0, base_url: str | None = None):
        try:
            import openai
        except ImportError as e:
            raise ImportError("OpenAI provider needs `pip install shortlist[openai]`") from e
        # `base_url` points the same client at any server speaking the OpenAI API — llama.cpp,
        # LM Studio, vLLM, LocalAI, OpenRouter (issue #7). None keeps OpenAI's own endpoint.
        self._client = openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=2, base_url=base_url)
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

    def _send_model(self) -> str:
        """The model name to send on a request. Overridden by the compatible provider, which resolves
        a blank model against the local server's ``/models`` list."""
        return self._model

    def complete(self, system: str, user: str) -> str:
        """Plain completion (no tools) — the external-search ``llm_web`` path (see base.complete)."""
        import openai

        try:
            r = self._client.chat.completions.create(
                model=self._send_model(),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except openai.OpenAIError as e:
            logger.warning("complete (openai): {}", e)
            return ""
        usage = getattr(r, "usage", None)
        if usage is not None:
            self.last_tokens = getattr(usage, "total_tokens", 0) or 0
        return r.choices[0].message.content or ""
