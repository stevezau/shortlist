"""Any server that speaks the OpenAI API: Ollama, llama.cpp, LM Studio, vLLM, LocalAI, OpenRouter…

Issue #7 asked for llama.cpp specifically, but a llama.cpp-shaped provider would have been the wrong
shape — and so, it turned out, was a separate Ollama one. Every local runtime people ask about
implements the same OpenAI-compatible `/v1/chat/completions` and `/v1/models`, Ollama included. One
provider with a configurable base URL covers all of them, including the next one, instead of
accreting a class per runtime.

Ollama used to have its own provider here (native `/api/tags`, `/api/chat`). It was merged into this
one; `make_curator("ollama")` still resolves for instances configured before the merge.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

from loguru import logger

from shortlist.engine.curator.base import picks_schema
from shortlist.engine.curator.openai import DEFAULT_MODEL, OpenAICurator


def normalize_base_url(url: str) -> str:
    """Point a bare host at its OpenAI API root, so `http://localhost:11434` just works.

    Every runtime we target serves the API under a path (`/v1`, or OpenRouter's `/api/v1`), but
    people paste the address they know their server by — the one in the Ollama docs has no path at
    all. Appending `/v1` to a bare host removes the single most likely reason "it can't reach my
    server": a URL that is right in every respect except the bit nobody told them to add.

    A URL that already carries a path is left exactly as typed, so an unusual layout stays possible.
    """
    parsed = urlparse(url.strip().rstrip("/"))
    if parsed.scheme and parsed.netloc and parsed.path in ("", "/"):
        return urlunparse(parsed._replace(path="/v1"))
    return url.strip().rstrip("/")


# Substrings of model names that cannot serve /chat/completions. A stock `ollama pull` leaves
# several of these on a box, and they sort ahead of every chat model people actually run
# (`all-minilm`, `bge-m3` < `llama3.3`), so picking the alphabetically-first model would reliably
# pick one that fails at curate time — after the settings "Test" button has already passed.
_EMBEDDING_HINTS = ("embed", "bge-", "-minilm", "all-minilm", "nomic-", "e5-", "gte-", "rerank")


def _is_embedding_model(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _EMBEDDING_HINTS)


class OpenAICompatibleCurator(OpenAICurator):
    name = "openai_compatible"
    # Web search is an OpenAI-hosted tool, not part of the API these servers implement. They can
    # still power the llm_web source through an external search provider (Exa), exactly like Ollama.
    supports_native_web_search = False

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        timeout: float = 300.0,
    ):
        """
        Args:
            base_url: The server's OpenAI-compatible root, e.g. ``http://llama:8080/v1``.
            api_key: Usually unused by local servers, but the SDK insists on a non-empty string —
                and a real one is needed for hosted gateways like OpenRouter.
            model: Whatever the server calls the loaded model. Local runtimes often ignore it.
            timeout: Generous by default: a CPU-bound local model is far slower than a hosted one.
        """
        if not base_url:
            raise ValueError("a local/OpenAI-compatible provider needs the server's base URL")
        resolved = normalize_base_url(base_url)
        if resolved != base_url.strip().rstrip("/"):
            logger.debug("curator: using {} for the OpenAI-compatible endpoint", resolved)
        super().__init__(api_key=api_key or "not-needed", model=model, timeout=timeout, base_url=resolved)
        # INDEX into the `_chat` ladder of the shape this server accepted. An index, not the shape
        # itself: `None` is a legitimate rung (send no response_format at all), so storing the shape
        # would make "not yet known" indistinguishable from "this server wants no format" — and the
        # first call would skip straight to the last rung.
        self._format_from = 0

    def _resolve_model(self) -> str:
        """The model name to send. Asks the server what it has if we weren't told.

        Inheriting OpenAI's `gpt-4o-mini` default would be actively wrong here: llama.cpp ignores the
        field, but vLLM and LM Studio VALIDATE it and answer "model not found" — so leaving Model
        blank, which is the natural thing to do for a server hosting exactly one model, would fail on
        two of the runtimes this provider exists to support. Resolved once and remembered.
        """
        if self._model and self._model != DEFAULT_MODEL:
            return self._model
        try:
            available = self.list_models()
        except Exception as e:
            logger.debug("could not list models on the local server ({}) — sending {!r}", type(e).__name__, self._model)
            return self._model
        if not available:
            return self._model
        chat_capable = [m for m in available if not _is_embedding_model(m)]
        self._model = (chat_capable or available)[0]
        if chat_capable:
            logger.warning(
                "curator: no model configured — using {!r}, the first chat-capable model the server "
                "offers. Set one in Settings if that is the wrong choice.",
                self._model,
            )
        else:
            logger.warning(
                "curator: no model configured, and every model the server lists looks like an "
                "embedding model — sending {!r}, which probably cannot chat. Load a chat model, or "
                "name one in Settings.",
                self._model,
            )
        return self._model

    def _chat(self, system: str, user: str):
        """Ask for JSON, degrading through what these servers actually implement.

        `json_schema` + `strict` is an OpenAI extension. llama.cpp, LM Studio and vLLM support it
        only in recent versions and reject it outright in older ones, so insisting on it would make
        this provider fail on the servers it was added for. The ladder tries the strictest form
        first, falls back to plain JSON mode, then to no format at all — the prompt asks for JSON
        regardless, and `validate_picks` downstream is what actually guarantees the result is sane.

        The rung that worked is remembered, so a run costs one attempt per user, not three.

        Only a *rejection of the shape* moves us down the ladder. A timeout or a 500 says nothing
        about what the server supports, and the curator instance lives for the whole run — so
        treating a blip as "json_schema unsupported" would silently drop every remaining user in the
        night's run to a weaker guarantee. Those propagate instead, to the existing CuratorError →
        heuristic path.
        """
        import openai

        model = self._resolve_model()
        formats: list[dict | None] = [
            {"type": "json_schema", "json_schema": {"name": "picks", "strict": True, "schema": picks_schema()}},
            {"type": "json_object"},
            None,
        ]
        unsupported = (openai.BadRequestError, openai.UnprocessableEntityError)
        last: Exception | None = None
        for index in range(self._format_from, len(formats)):
            response_format = formats[index]
            kwargs = {"response_format": response_format} if response_format else {}
            try:
                reply = self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    **kwargs,
                )
            except unsupported as e:
                last = e
                logger.warning(
                    "local server rejected response_format={} ({}) — trying a simpler request",
                    (response_format or {}).get("type", "none"),
                    type(e).__name__,
                )
                continue
            self._format_from = index
            return reply
        raise last if last else RuntimeError("the local server refused every request shape")

    def list_models(self) -> list[str]:
        """Every model the server offers, UNFILTERED.

        The inherited version keeps only OpenAI's own families (`gpt-`, `o1`…). Against a local
        server that is actively harmful: names are arbitrary, so the filter usually matches nothing
        and silently falls back — but Ollama ships a model literally called `gpt-oss`, and on that
        server the filter would match it alone and hide every other model you have.
        """
        return sorted(m.id for m in self._client.models.list().data)

    def ping(self) -> str:
        """Ask what the server is serving, rather than making it generate.

        The inherited ping sends a real chat completion. Against OpenAI that's a few cents and a
        second; against a CPU-bound local model it's a 30-second wait on the settings "Test" button
        for information a model list gives instantly — and it fails outright when the server is up
        but has no model loaded, which is exactly the state you'd want Test to help you diagnose.
        """
        models = self.list_models()
        if not models:
            return "connected — the server reports no models loaded"
        return f"connected — {len(models)} model(s) available"
