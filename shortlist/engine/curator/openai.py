"""OpenAI curator — web-search title discovery via the Responses API web_search tool."""

from __future__ import annotations

from loguru import logger

from shortlist.engine.curator.base import (
    ThreadLocalTokens,
    build_web_prompt,
    parse_web_titles,
)
from shortlist.engine.models import UserProfile
from shortlist.engine.provider_calls import ProviderCall, ProviderCallControls, provider_call
from shortlist.engine.taste import TastePrompt
from shortlist.engine.web_guidance import Guidance

# Must match `defaultModel` for "openai" in web/src/lib/providers.ts, which the wizard writes into
# `curator.model` — this constant only applies when that setting is blank. The two disagreed
# (gpt-4o-mini here, gpt-5-mini there), so an owner who cleared the field silently switched model.
# Pinned by tests/unit/test_curator_defaults.py, which fails if this and the SPA disagree — they did
# (gpt-4o-mini here, gpt-5-mini there), so clearing the Model field silently changed which ran.
#
# `gpt-4o-mini`, on measurement rather than recency. Same seeds, same day, both under the
# year-anchored prompt:
#
#   gpt-4o-mini   12 titles, 12 from 2024+,   8,384 tokens,   5.9s
#   gpt-5-mini    12 titles,  9 from 2024+,  67,612 tokens, 100.0s
#
# 8x the tokens and 17x the wall clock for a slightly worse answer, and the native path runs one call
# per person per row — roughly 92 a night on a 46-user server. The case for gpt-5-mini is that it is
# newer and lives longer; the case against is that this cost is paid every night and is invisible,
# whereas a retired model 404s loudly and gets fixed in a minute.
#
# Undated on purpose (see the note on the Anthropic default). Also measured: gpt-4o returned 4 usable
# titles where gpt-4o-mini returned 12, so a thin row on OpenAI is a reason to check the model.
DEFAULT_MODEL = "gpt-4o-mini"

# Structured Outputs for the title list. `strict` requires every property to be listed in `required`
# and `additionalProperties: false` at each level, so `year` is nullable rather than optional.
# `parse_web_titles` still parses the result — it accepts the bare array or an object wrapping one,
# and stays the fallback for any model that rejects the format.
#
# Not measured here: gpt-4o. It returned 4 titles from 32k input tokens where gpt-4o-mini returned
# 12 from 8k, so if anyone reports a thin row on OpenAI, the model is the first thing to check.
_TITLES_FORMAT: dict = {
    "format": {
        "type": "json_schema",
        "name": "titles",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "titles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "title": {"type": "string"},
                            "year": {"type": ["integer", "null"]},
                            "media": {"type": "string", "enum": ["movie", "show"]},
                        },
                        "required": ["title", "year", "media"],
                    },
                }
            },
            "required": ["titles"],
        },
    }
}


class OpenAICurator:
    name = "openai"
    supports_native_web_search = True  # Responses API web_search tool (see recommend_web)
    last_tokens = ThreadLocalTokens()  # per-thread, so parallel per-user web search doesn't race
    last_output_tokens = ThreadLocalTokens()

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        base_url: str | None = None,
        max_retries: int = 2,
        follow_redirects: bool = True,
        provider_controls: ProviderCallControls | None = None,
    ):
        try:
            import openai
        except ImportError as e:
            raise ImportError("OpenAI provider needs `pip install shortlist[openai]`") from e
        if provider_controls is not None and base_url is None:
            base_url = "https://api.openai.com/v1"
        # `base_url` points the same client at any server speaking the OpenAI API — llama.cpp,
        # LM Studio, vLLM, LocalAI, OpenRouter (issue #7). None keeps OpenAI's own endpoint.
        self._client = openai.OpenAI(
            api_key=api_key,
            timeout=timeout,
            max_retries=0 if provider_controls is not None else max_retries,
            base_url=base_url,
            **(
                {"http_client": openai.DefaultHttpxClient(follow_redirects=False)}
                if provider_controls is not None or not follow_redirects
                else {}
            ),
        )
        self._provider_controls = provider_controls
        self._destination = (base_url or "https://api.openai.com/v1").rstrip("/")
        self._model = model
        # Whether this model takes a response schema alongside web search. Measured true on
        # gpt-4o-mini and gpt-4.1-mini; assumed true until a call proves otherwise, then remembered
        # for the life of this curator so the failed attempt is paid once, not once per user per night.
        self._schema_supported = True

    def ping(self) -> str:
        """A cheap "does this key work" probe. The Settings card fires it automatically.

        `max_completion_tokens`, NOT `max_tokens`: Chat Completions rejects the latter outright on
        the gpt-5 and o-series families ("Unsupported parameter: 'max_tokens' is not supported with
        this model"), which turned a perfectly good key into "Connection failed" the moment the
        default model moved to `gpt-5-mini`. The budget is generous because a reasoning model spends
        it on reasoning first and would otherwise return an empty string, which reads as a failure
        too. Both spellings are accepted by the 4-series, so this is safe for every model.
        """
        r = self._client.chat.completions.create(
            model=self._model,
            max_completion_tokens=256,
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

    def recommend_web(
        self,
        profile: UserProfile,
        seeds: list,
        k: int,
        *,
        guidance: Guidance | None = None,
        taste: TastePrompt | None = None,
    ) -> list[dict]:
        """Propose up to k titles to watch next via the Responses API web-search tool (``llm_web``).

        Returns ``[{title, year, media}]`` for the caller to resolve against TMDB. Degrades to an
        empty list on a provider error; the source's own try/except in candidates.py is the backstop
        for any other failure, so a run never fails here.

        ``guidance`` is the row's AI instructions (#138); None sends the built-in prompt. ``taste`` is the
        history text (#152); only a wide one changes the prompt.
        """
        import openai

        system, user = build_web_prompt(profile, seeds, k, guidance=guidance, taste=taste)
        try:
            r = self._web_search_call(system, user, with_schema=self._schema_supported)
        except openai.OpenAIError as e:
            if self._provider_controls is not None:
                logger.warning("llm_web (openai) failed ({})", type(e).__name__)
                return []
            if not self._schema_supported:  # already schema-less, so the fault is not the format
                logger.warning("llm_web (openai): {}", e)
                return []
            # A model that rejects the schema rejects it every time. Remembering means one wasted
            # request per process rather than one per user per night, seen only at DEBUG.
            logger.debug("llm_web (openai) structured attempt failed ({}); retrying without a schema", type(e).__name__)
            self._schema_supported = False
            try:
                r = self._web_search_call(system, user, with_schema=False)
            except openai.OpenAIError as retry_error:
                logger.warning("llm_web (openai): {}", retry_error)
                return []
        usage = getattr(r, "usage", None)
        if usage is not None:
            self.last_tokens = getattr(usage, "total_tokens", 0) or 0
            # Responses API: `output_tokens` already includes any reasoning tokens.
            self.last_output_tokens = getattr(usage, "output_tokens", 0) or 0
        return parse_web_titles(getattr(r, "output_text", "") or "", k)

    def _web_search_call(self, system: str, user: str, *, with_schema: bool):
        """One Responses API call with the web-search tool, optionally under a response schema.

        `search_context_size: high` scored best of the three sizes on a live run (12 of 12 resolvable
        titles against 10 at `low`), with identical input tokens and no price difference — web search
        is $10/1k calls on every model whatever the size. The gap is within run variance, so this is
        "free, so take it" rather than a proven win.

        `filters` is deliberately never sent: it 400s on gpt-4o-mini and gpt-4.1-mini ("Parameter
        'filters' not supported with model 'X'") and works only on the gpt-5 family. The DEFAULT is
        now in that family, but the owner picks the model — so sending it would break every setup
        that chose a 4-series one.

        Dropping the schema — never the search — is the fallback, because losing the schema costs
        tidy parsing (which `parse_web_titles` already handles) while losing the search costs the
        whole source.
        """
        kwargs: dict = {
            "model": self._model,
            "instructions": system,
            "input": user,
            "tools": [{"type": "web_search", "search_context_size": "high"}],
        }
        if with_schema:
            # Structured output DOES combine with web search here — measured on gpt-4o-mini and
            # gpt-4.1-mini, 12 of 12 resolvable both times, and it cut output tokens from ~300 to 212
            # because the model stops narrating around the JSON.
            kwargs["text"] = _TITLES_FORMAT
        controls = self._provider_controls
        if controls is not None:
            if controls.max_output_tokens is not None:
                kwargs["max_output_tokens"] = controls.max_output_tokens
            if controls.max_native_tool_uses is not None:
                kwargs["max_tool_calls"] = controls.max_native_tool_uses
        with provider_call(
            controls,
            ProviderCall(
                kind="native_search",
                provider=self.name,
                destination=self._destination,
                model=self._model,
                output_tokens=kwargs.get("max_output_tokens"),
                native_tool_uses=kwargs.get("max_tool_calls"),
            ),
        ):
            return self._client.responses.create(**kwargs)

    def _send_model(self) -> str:
        """The model name to send on a request. Overridden by the compatible provider, which resolves
        a blank model against the local server's ``/models`` list."""
        return self._model

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        """Plain completion (no tools) — the external-search ``llm_web`` path (see base.complete)."""
        import openai

        kwargs: dict = {}
        controls = self._provider_controls
        if controls is not None:
            max_tokens = controls.output_limit(max_tokens)
        if max_tokens is not None:
            # `max_completion_tokens`, not `max_tokens`: the gpt-5 and o-series reject the latter (see ping).
            kwargs["max_completion_tokens"] = max_tokens
        try:
            model = self._send_model()
            with provider_call(
                controls,
                ProviderCall(
                    kind="completion",
                    provider=self.name,
                    destination=self._destination,
                    model=model,
                    output_tokens=max_tokens,
                ),
            ):
                r = self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    **kwargs,
                )
        except openai.OpenAIError as e:
            logger.warning("complete (openai): {}", e)
            return ""
        usage = getattr(r, "usage", None)
        if usage is not None:
            self.last_tokens = getattr(usage, "total_tokens", 0) or 0
            self.last_output_tokens = getattr(usage, "completion_tokens", 0) or 0
        return r.choices[0].message.content or ""
