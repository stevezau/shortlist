"""Google curator — web-search title discovery via Gemini's Google Search grounding tool."""

from __future__ import annotations

from loguru import logger

from shortlist.engine.curator.base import (
    ThreadLocalTokens,
    build_web_prompt,
    parse_web_titles,
)
from shortlist.engine.models import UserProfile

DEFAULT_MODEL = "gemini-2.5-flash"


class GoogleCurator:
    name = "google"
    supports_native_web_search = True  # Gemini's Google Search grounding tool (see recommend_web)
    last_tokens = ThreadLocalTokens()  # per-thread, so parallel per-user web search doesn't race

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL, timeout: float = 60.0):
        try:
            from google import genai
        except ImportError as e:
            raise ImportError("Google provider needs `pip install shortlist[google]`") from e
        # google-genai's HttpOptions.timeout is in MILLISECONDS; without this the constructor's
        # timeout was silently dropped, so a stalled Gemini call was bounded only by the SDK default.
        self._client = genai.Client(api_key=api_key, http_options={"timeout": int(timeout * 1000)})
        self._model = model

    def ping(self) -> str:
        r = self._client.models.generate_content(model=self._model, contents="Reply with the single word: ready")
        return r.text or ""

    def list_models(self) -> list[str]:
        """Gemini model ids that support content generation, for the setup picker. Names come back
        prefixed ('models/gemini-2.5-flash'); strip it so the id matches what the SDK is called with."""
        out: list[str] = []
        for m in self._client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            name = (getattr(m, "name", "") or "").removeprefix("models/")
            if name:
                out.append(name)
        return sorted(out)

    def recommend_web(self, profile: UserProfile, seeds: list, k: int) -> list[dict]:
        """Propose up to k titles via Gemini's Google Search grounding tool (the ``llm_web`` source).

        Grounding is incompatible with a response schema in the Gemini API, so this asks for plain
        JSON text and leans on the tolerant ``parse_web_titles`` — same shape as the other native
        providers. Degrades to an empty list on any provider error (the source's guard is the backstop).
        """
        from google.genai import types

        system, user = build_web_prompt(profile, seeds, k)
        try:
            r = self._client.models.generate_content(
                model=self._model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                ),
            )
        except Exception as e:  # google-genai raises provider-specific exceptions
            # Type only — the google-genai error text carries the API key (`?key=AIza…`).
            logger.warning("llm_web (google) failed ({})", type(e).__name__)
            return []
        usage = getattr(r, "usage_metadata", None)
        self.last_tokens = getattr(usage, "total_token_count", 0) or 0
        return parse_web_titles(r.text or "", k)

    def complete(self, system: str, user: str) -> str:
        """Plain completion (no tools) — the external-search ``llm_web`` path (see base.complete)."""
        try:
            r = self._client.models.generate_content(
                model=self._model, contents=user, config={"system_instruction": system}
            )
        except Exception as e:
            # Type only — the google-genai error text carries the API key (`?key=AIza…`).
            logger.warning("complete (google) failed ({})", type(e).__name__)
            return ""
        usage = getattr(r, "usage_metadata", None)
        self.last_tokens = getattr(usage, "total_token_count", 0) or 0
        return r.text or ""
