"""Web search providers for the ``llm_web`` candidate source.

Two ways the "search the web for what to watch next" source can get web results:

* **Native** — the curator's own provider runs the search server-side (Claude/GPT/Gemini web-search
  tools). Only works where the provider offers it; impossible for a local Ollama model.
* **External search provider (here)** — WE run the search from a query built off the user's
  watchlist, hand the result snippets to the curator, and let *any* model recommend from them. This
  is the universal path: it works for every curator, Ollama included, because the model reads what we
  found instead of searching itself.

Two external providers ship today, both behind the same ``WebSearchProvider`` protocol:

* **Exa** — a hosted search API built for LLM grounding. Bills per search; returns extracted PAGE
  TEXT, which is the richest context a curator can read.
* **SearXNG** — the self-hosted metasearch engine. Free to run, and it needs no vendor account or
  key, though it does forward each query on to real search engines. Returns those engines' result
  SNIPPETS (a couple hundred characters) rather than page text, so it takes a wider slice of the one
  page a search returns (see ``results_per_query``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry

EXA_SEARCH_URL = "https://api.exa.ai/search"
_DEFAULT_RESULTS = 8
_DEFAULT_MAX_CHARS = 800  # per-result text budget — enough to name titles, small enough to stay cheap


@dataclass(frozen=True)
class SearchResult:
    """One web result: a title, its URL, and an extracted text snippet for the model to read."""

    title: str
    url: str
    text: str


class WebSearchProvider(Protocol):
    """A web search backend for the ``llm_web`` source. ``name`` labels it in logs/settings."""

    name: str
    # How many results to pull per per-title search. A per-provider number rather than one constant
    # because the right depth follows the provider's economics AND its result quality: see each class.
    results_per_query: int

    def search(self, query: str, *, num_results: int = _DEFAULT_RESULTS) -> list[SearchResult]: ...

    def ping(self) -> str: ...


class ExaClient:
    """Exa semantic search (https://exa.ai). Returns ranked web results with extracted text.

    A search is a read, but Exa exposes it as POST, so it goes through ``http_retry.request`` — which
    retries the safe cases (a connect failure that never landed, or an explicit 429 rate-limit) and
    leaves the rest to the source's own try/except in ``candidates.py``. The API key travels in the
    ``x-api-key`` header (never the URL/query), so it can't leak into a logged request line (rule 9).
    """

    name = "exa"
    # Lean on purpose: Exa bills per search AND returns up to 800 chars of real page text per result,
    # so five results already fill the curator's RAG budget. Depth here costs money.
    results_per_query = 5

    def __init__(self, api_key: str, *, timeout: float = 20.0):
        self._api_key = api_key
        self._timeout = timeout

    def search(self, query: str, *, num_results: int = _DEFAULT_RESULTS) -> list[SearchResult]:
        """Run one search and return up to ``num_results`` results with extracted text.

        Args:
            query: The natural-language search query (built from the user's watchlist upstream).
            num_results: How many web results to ask Exa for.

        Returns:
            The parsed results, newest/most-relevant first. Results with no title are skipped.
        """
        response = http_retry.request(
            "POST",
            EXA_SEARCH_URL,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            json={
                "query": query,
                "numResults": num_results,
                "type": "auto",
                "contents": {"text": {"maxCharacters": _DEFAULT_MAX_CHARS}},
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        results: list[SearchResult] = []
        for item in response.json().get("results", []):
            title = (item.get("title") or "").strip()
            if not title:
                continue
            results.append(SearchResult(title=title, url=item.get("url") or "", text=(item.get("text") or "").strip()))
        logger.debug("exa search · {!r} → {} results", query[:60], len(results))
        return results

    def ping(self) -> str:
        """A cheap probe for the Settings 'test connection' button. Raises on an unusable key."""
        results = self.search("popular movies and TV shows to watch this week", num_results=1)
        return f"ok — {len(results)} result"


class SearxngClient:
    """SearXNG metasearch (https://docs.searxng.org), self-hosted by the owner.

    The self-hosted path: the search runs through the owner's own box rather than a paid vendor,
    which is the whole point for a self-hosted server (issue #78). Note SearXNG is a metasearch PROXY,
    not an index — it forwards each query on to real engines (Google, Brave, DuckDuckGo, …) and merges
    what they return, so this is not an offline or air-gapped path; what it avoids is a vendor
    account, an API key and a per-search bill. Reads go through ``http_retry.get`` — SearXNG's
    bot ``limiter`` is enabled in the stock docker config and answers API callers with 429, and a
    retry rides out the burst instead of losing the seed.

    Two shape notes, both verified against a real container rather than assumed:

    * **JSON is off by default.** Stock ``settings.yml`` ships ``search.formats: [html]``, and
      ``format=json`` against it returns a bare HTML **403** carrying no machine-readable hint. That
      403 is by far the most common way this integration fails, so it is translated into the exact
      fix rather than surfaced as a status code.
    * **There is no result-count parameter.** SearXNG returns a whole page (20-30 results), so
      ``num_results`` is applied by slicing here.
    """

    name = "searxng"
    # Wider than Exa's five, and it costs nothing to ask: SearXNG returns a whole page per search
    # either way, so this only changes how much of that page we keep. The depth is worth having
    # because `content` is an upstream engine's SNIPPET (~150-400 chars measured) rather than Exa's
    # page text. It cannot crowd other seeds out of the RAG prompt — `_interleave` shares that cap
    # across seeds — so it adds depth exactly where there is room for it, i.e. when few seeds ran.
    results_per_query = 10

    def __init__(self, base_url: str, *, username: str = "", password: str = "", timeout: float = 20.0):
        # Credentials inline in the URL (`http://user:pass@host:8080`) are moved into the auth header
        # and stripped from the URL, so they can't ride into a log line or an error string (rule 9).
        # This is a BACKSTOP, not the supported way in: the settings API refuses that shape outright,
        # because `searxng.url` is stored in the clear and recorded verbatim in the immutable
        # `settings.change` audit event, and stripping here would be far too late for either.
        parsed = httpx.URL(base_url.rstrip("/"))
        url_user, url_password = parsed.username, parsed.password
        # Appended, never resolved as a relative reference: an instance proxied at `/searxng` must
        # keep that prefix, and RFC-3986 resolution would replace the last segment and drop it.
        self._search_url = f"{parsed.copy_with(userinfo=b'')}/search"
        # Explicitly configured credentials win; the URL is only a fallback. Basic auth needs both
        # halves — SearXNG has no auth of its own, but owners routinely park it behind a proxy.
        user = username or url_user
        secret = password or url_password
        self._auth = (user, secret) if user and secret else None
        self._timeout = timeout

    def search(self, query: str, *, num_results: int = _DEFAULT_RESULTS) -> list[SearchResult]:
        """Run one search and return up to ``num_results`` results with their snippets.

        Args:
            query: The natural-language search query (built from the user's watchlist upstream).
            num_results: How many results to keep from the page SearXNG returns.

        Returns:
            The parsed results in SearXNG's own ranking order. Results with no title are skipped, and
            a page with no results at all yields ``[]`` — one seed whose engines were all rate-limited
            must not fail the other seeds.

        Raises:
            RuntimeError: The instance refused the JSON format, or answered with something that isn't
                a SearXNG JSON page. Never carries the URL — it can embed reverse-proxy credentials.
        """
        payload = self._fetch(query)
        results: list[SearchResult] = []
        for item in payload.get("results", [])[: max(0, num_results)]:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            # `content` is SearXNG's name for the snippet; `text` is what the RAG prompt reads.
            results.append(
                SearchResult(title=title, url=item.get("url") or "", text=(item.get("content") or "").strip())
            )
        dead = _unresponsive(payload)
        if not results and dead:
            logger.warning("searxng search · {!r} → no results; engines unresponsive: {}", query[:60], dead)
        else:
            logger.debug("searxng search · {!r} → {} results", query[:60], len(results))
        return results

    def _fetch(self, query: str) -> dict:
        """One raw JSON search page, with the two misconfigurations translated into their fix."""
        response = http_retry.get(
            self._search_url,
            params={"q": query, "format": "json", "categories": "general"},
            auth=self._auth,
            timeout=self._timeout,
        )
        if response.status_code == 403:
            raise RuntimeError(
                "SearXNG refused the JSON format (403). Add `json` to `search.formats` in its "
                "settings.yml and restart it — a stock instance only serves HTML."
            )
        if response.is_error:
            # Deliberately NOT `raise_for_status()`: its message embeds the full request URL, which
            # this error text carries all the way to the API response and an `events` row.
            raise RuntimeError(f"SearXNG answered HTTP {response.status_code} — check the address and that it is up.")
        try:
            return response.json()
        except ValueError as e:
            raise RuntimeError("That address answered with something other than SearXNG JSON — check the URL.") from e

    def ping(self) -> str:
        """A probe for the Settings 'test connection' button. Raises with the fix on a misconfigured
        instance — including the silent case, where the instance is healthy but every engine is
        blocked. ``search`` deliberately shrugs that off (one bad seed must not fail the rest), but
        an owner pressing Test has to be told, and told WHICH engines died."""
        payload = self._fetch("popular movies and TV shows to watch this week")
        results = [r for r in payload.get("results", []) if (r.get("title") or "").strip()]
        if not results:
            dead = _unresponsive(payload)
            detail = f" — these engines failed: {', '.join(dead)}" if dead else ""
            raise RuntimeError(
                f"SearXNG answered, but no engine returned anything{detail}. The instance is reachable "
                "and JSON is on; its upstream engines are blocked or rate-limited. Check its own logs."
            )
        return f"ok — {len(results)} results"


def _unresponsive(payload: dict) -> list[str]:
    """Engine names SearXNG reported as failing, e.g. ``['duckduckgo', 'brave']``.

    Shape is a list of ``[name, reason]`` pairs (recorded: ``tests/fixtures/searxng_search.json``);
    tolerate a bare string too rather than trust one recording of a third-party schema.
    """
    names: list[str] = []
    for entry in payload.get("unresponsive_engines") or []:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, (list, tuple)) and entry:
            names.append(str(entry[0]))
    return names
