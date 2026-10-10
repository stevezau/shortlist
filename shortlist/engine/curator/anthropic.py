"""Anthropic curator — web-search title discovery via Claude's web_search server tool."""

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

# Design doc §3: cheap tier is plenty for a web-search title lookup.
#
# The UNDATED alias, deliberately, and it must stay that way. `gemini-2.5-flash` was pinned here's
# sibling until Google retired it and every fresh Google install 404'd on its first run with nothing
# in the UI saying why; a dated Anthropic id is the same bug on a different retirement schedule.
# Probed live (2026-09-03): `claude-haiku-4-5` answers 200, and Anthropic's own /v1/models listing
# now carries undated ids for everything newer than this generation — `claude-haiku-4-5-20251001`
# was the OLDEST id still listed, so it is the next one to go.
#
# Must match `defaultModel` for "anthropic" in web/src/lib/providers.ts — the wizard WRITES its copy
# into `curator.model`, so a disagreement means two different defaults depending on how you set up.
# Pinned by tests/unit/test_curator_defaults.py.
DEFAULT_MODEL = "claude-haiku-4-5"


class AnthropicCurator:
    name = "anthropic"
    supports_native_web_search = True  # Claude's web_search server tool (see recommend_web)
    last_tokens = ThreadLocalTokens()  # per-thread, so parallel per-user web search doesn't race
    last_output_tokens = ThreadLocalTokens()

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
        max_retries: int = 2,
        base_url: str | None = None,
        follow_redirects: bool = True,
        provider_controls: ProviderCallControls | None = None,
    ):
        try:
            import anthropic
        except ImportError as e:
            raise ImportError("Anthropic provider needs `pip install shortlist[anthropic]`") from e
        if provider_controls is not None and base_url is None:
            base_url = "https://api.anthropic.com"
        self._client = anthropic.Anthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=0 if provider_controls is not None else max_retries,
            base_url=base_url,
            **(
                {"http_client": anthropic.DefaultHttpxClient(follow_redirects=False)}
                if provider_controls is not None or not follow_redirects
                else {}
            ),
        )
        self._provider_controls = provider_controls
        self._destination = (base_url or "https://api.anthropic.com").rstrip("/")
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
        try:
            return [m.id for m in self._client.models.list(limit=100).data]
        except Exception as e:
            logger.warning("Anthropic model list unavailable ({}); offering the built-in list", type(e).__name__)
            return [
                "claude-sonnet-5",
                "claude-haiku-4-5",
                "claude-opus-4",
                "claude-sonnet-4",
            ]

    def recommend_web(
        self,
        profile: UserProfile,
        seeds: list,
        k: int,
        *,
        guidance: Guidance | None = None,
        taste: TastePrompt | None = None,
    ) -> list[dict]:
        """Propose up to k titles to watch next via Claude's web-search tool (the ``llm_web`` source).

        Returns ``[{title, year, media}]`` for the caller to resolve against TMDB. Degrades to an
        empty list on a provider error; the source's own try/except in candidates.py is the backstop
        for any other failure (unexpected response shape, etc.), so a run never fails here.

        ``guidance`` is the row's AI instructions (#138); None sends the built-in prompt. ``taste`` is the
        history text (#152); only a wide one changes the prompt.
        """
        import anthropic

        system, user = build_web_prompt(profile, seeds, k, guidance=guidance, taste=taste)
        controls = self._provider_controls
        output_tokens = controls.output_limit(2048) if controls is not None else 2048
        tool_uses = (
            min(5, controls.max_native_tool_uses)
            if controls is not None and controls.max_native_tool_uses is not None
            else 5
        )
        try:
            with provider_call(
                controls,
                ProviderCall(
                    kind="native_search",
                    provider=self.name,
                    destination=self._destination,
                    model=self._model,
                    output_tokens=output_tokens,
                    native_tool_uses=tool_uses,
                ),
            ):
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=output_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    # Five, not three, and the difference is the YEAR. Measured against the live API on
                    # five seeds: at max_uses=3 only 4 of 12 proposals carried a release year, at 5 it is
                    # 10-12 of 12, and the resolver needs the year to disambiguate. It costs ~$0.05 more
                    # per user per run ($0.055 → $0.106, five searches at $10/1k plus the tokens they
                    # bring). Ten searches buys one more year and doubles the bill again.
                    #
                    # The tool version stays at the original: `web_search_20260209` and `_20260318` add
                    # dynamic filtering, which needs Claude 4.6+ and 400s on our default haiku-4-5 unless
                    # you pass `allowed_callers: ["direct"]` — and with `direct` they produce byte-identical
                    # output at identical cost. Nothing to gain until the default model moves.
                    #
                    # No `allowed_domains` either: a whitelist of review sites is rejected outright with
                    # "The following domains are not accessible to our user agent: ['reddit.com',
                    # 'vulture.com']". `blocked_domains` and `user_location` both work and both changed
                    # nothing except the token bill.
                    tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": tool_uses}],
                )
        except anthropic.APIError as e:
            logger.warning("llm_web (anthropic): {}", e)
            return []
        self.last_tokens = response.usage.input_tokens + response.usage.output_tokens
        self.last_output_tokens = response.usage.output_tokens
        # The model may emit several text blocks around its searches; the JSON list is in the last one.
        text = "".join(b.text for b in response.content if b.type == "text")
        return parse_web_titles(text, k)

    def complete(self, system: str, user: str, *, max_tokens: int | None = None) -> str:
        """Plain completion (no tools) — the external-search ``llm_web`` path (see base.complete)."""
        import anthropic

        controls = self._provider_controls
        output_tokens = controls.output_limit(max_tokens or 2048) if controls is not None else max_tokens or 2048
        try:
            with provider_call(
                controls,
                ProviderCall(
                    kind="completion",
                    provider=self.name,
                    destination=self._destination,
                    model=self._model,
                    output_tokens=output_tokens,
                ),
            ):
                response = self._client.messages.create(
                    model=self._model,
                    max_tokens=output_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
        except anthropic.APIError as e:
            logger.warning("complete (anthropic): {}", e)
            return ""
        self.last_tokens = response.usage.input_tokens + response.usage.output_tokens
        self.last_output_tokens = response.usage.output_tokens
        return "".join(b.text for b in response.content if b.type == "text")
