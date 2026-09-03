"""Google curator — web-search title discovery via Gemini's Google Search grounding tool."""

from __future__ import annotations

from loguru import logger

from shortlist.engine.curator.base import (
    ThreadLocalTokens,
    build_web_prompt,
    parse_web_titles,
)
from shortlist.engine.models import UserProfile

# An ALIAS, not a pinned model, and that is the whole point. This was `gemini-2.5-flash` until it
# started answering `404 NOT_FOUND: This model is no longer available to new users` — so every new
# install choosing Google got a 404 on its first run, with nothing in the UI saying why. A pinned
# model id is a dated bug waiting for Google's retirement schedule; `gemini-flash-latest` is
# maintained by Google and cannot rot the same way. (`gemini-2.5-flash-lite` is equally dead.)
DEFAULT_MODEL = "gemini-flash-latest"

# Gemini's schema dialect is NOT JSON Schema: a union type like {"type": ["integer", "null"]} is
# rejected by google-genai's own validator before the request is even sent. Single type, plus
# `nullable`. Structured output does combine with grounding on Gemini 3 (docs say so and a live run
# raised no error), so this is sent alongside the search tool rather than instead of it.
_TITLES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": "integer", "nullable": True},
                    "media": {"type": "string", "enum": ["movie", "show"]},
                },
                "required": ["title", "media"],
            },
        }
    },
    "required": ["titles"],
}


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
        # Whether this model takes a response schema alongside grounding. Gemini 3 does; 2.5 does not.
        # The owner picks the model, so this is learned from the first call and then remembered — a
        # 2.5 user would otherwise pay a rejected request before every single run, seen only at DEBUG.
        self._schema_supported = True

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

        **Gemini does not actually search for this task — but its answers are still good.**
        Measured against the live API: the tool is attached and
        `grounding_metadata.web_search_queries` comes back empty. Not a reporting artefact — a
        control question that cannot be answered from memory ("shows that premiered in 2026") made
        the same client issue three real queries and cite its sources. Nor can it be forced: an
        explicit "you MUST search", a retrieval-shaped prompt, and `tool_config` with `mode="ANY"`
        all left it at zero searches (and `mode="ANY"` returned an empty response after 47s, so it
        must never be sent).

        The conclusion originally drawn from that was wrong, and is corrected here. Under the old
        prompt Gemini returned 0 of 12 titles from 2025 or later, which read as staleness. Under the
        year-anchored prompt (see ``_WEB_SYSTEM``) it returns **12 of 12 from 2024 or later**,
        overlapping the titles the searching control found. Its training data is simply recent
        enough. So this logs at INFO, not WARNING: the one real cost is that it cannot refresh
        itself as its cutoff recedes, where Claude and GPT can. Degrades to an empty list on any
        provider error.
        """
        system, user = build_web_prompt(profile, seeds, k)
        try:
            r = self._grounded_call(system, user, with_schema=self._schema_supported)
        except Exception as e:  # google-genai raises provider-specific exceptions
            # Type only, never the message — the google-genai error text carries the API key.
            if not self._schema_supported:  # already schema-less, so the fault is not the format
                logger.warning("llm_web (google) failed ({})", type(e).__name__)
                return []
            # Gemini 3 takes a schema alongside grounding; 2.5 does not. The owner picks the model,
            # so it is learned once and remembered rather than re-attempted every run.
            logger.debug("llm_web (google) structured attempt failed ({}); retrying plain", type(e).__name__)
            self._schema_supported = False
            try:
                r = self._grounded_call(system, user, with_schema=False)
            except Exception as retry_error:
                logger.warning("llm_web (google) failed ({})", type(retry_error).__name__)
                return []
        usage = getattr(r, "usage_metadata", None)
        self.last_tokens = getattr(usage, "total_token_count", 0) or 0
        if not _searched(r):
            # INFO, not WARNING, and no longer "these titles are stale". Re-measured 2026-09-03 under
            # the year-anchored prompt: Gemini still issues no search queries for this task, but the
            # titles it returns from memory were 12 of 12 from 2024 or later, and matched what the
            # searching control found. The behaviour is real; the old conclusion drawn from it was
            # wrong. What remains true is that it cannot self-correct as its cutoff recedes.
            logger.info(
                "llm_web (google): Gemini answered from its own knowledge rather than searching. Its "
                "picks are current today, but unlike Claude and GPT it will not refresh them by "
                "searching — so they age with the model. Exa or SearXNG always reads live articles."
            )
        return parse_web_titles(r.text or "", k)

    def _grounded_call(self, system: str, user: str, *, with_schema: bool):
        """One generate_content call with Google Search grounding, optionally under a response schema.

        Dropping the schema — never the grounding — is the fallback: the tolerant parser copes
        without a schema, whereas losing the tool would leave the source with nothing to search.
        """
        from google.genai import types

        config: dict = {
            "system_instruction": system,
            "tools": [types.Tool(google_search=types.GoogleSearch())],
        }
        if with_schema:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = _TITLES_SCHEMA
        return self._client.models.generate_content(
            model=self._model, contents=user, config=types.GenerateContentConfig(**config)
        )

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


def _searched(response: object) -> bool:
    """Whether Gemini actually ran a Google Search for this response.

    ``grounding_metadata.web_search_queries`` carries the queries it issued, and is populated only
    when a search really ran — verified by a control that DID search (a live news question) against
    the recommendation calls that did not. Everything is read defensively: an SDK that stops
    populating the field would otherwise turn every run into a false warning.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return False
    metadata = getattr(candidates[0], "grounding_metadata", None)
    return bool(getattr(metadata, "web_search_queries", None))
