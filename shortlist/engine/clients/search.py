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
  TEXT *and*, via ``outputSchema``, a structured list of the titles those pages recommend — so the
  curator reads a title list rather than paragraphs of prose.
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

# Exa's search modes, cheapest first. Measured on 2026-09-02 (see
# `.claude/docs/llm-web-search-upgrade.md`): `deep-lite` returned 47 and 36 TMDB-resolvable titles
# on two seeds against `auto`'s 13 and 8, for $0.012 a search against $0.007. `auto` is NOT a safe
# default here — with `outputSchema` attached it returned ZERO titles on one run despite 26k
# characters of page text, and `instant` puts a year on barely one title in nine, which the TMDB
# resolver needs. The owner picks the mode; this list is what the settings dropdown offers.
# Three of Exa's six `/search` types, because only these three do the job. Measured live against the
# API on one query, with the `outputSchema` this client always sends:
#
#   instant   11 extracted titles   2.8s
#   fast       0                    1.5s   <- rejected
#   auto       0                    2.3s   <- rejected, and it is Exa's OWN "recommended" setting
#   deep-lite 42                   13.5s   <- the default
#   deep      32                    7.5s
#   deep-reasoning 32              13.4s   <- rejected: same titles as `deep`, ~2x the wait and cost
#
# `fast` and `auto` return results but decline to synthesise the structured output, so they hand back
# ZERO titles — and title extraction is what lets Exa run with no AI provider at all. Offering a
# setting that silently produces an empty row is worse than not offering it.
#
# All six remain valid API values; a stored setting naming a dropped one still resolves, because
# `ExaClient.__init__` clamps anything unrecognised to the default.
EXA_SEARCH_TYPES: tuple[str, ...] = ("instant", "deep-lite", "deep")
DEFAULT_EXA_SEARCH_TYPE = "deep-lite"

# How long to wait, by mode. Measured response times: `instant` answers in 2.5-4.6s, `deep-lite` and
# `deep` in 6.5-18s. The ceiling matters because Exa does sometimes
# hang rather than answer — 1 of 6 `deep-lite` searches never returned and hit the timeout, and a
# request that runs past ~100s comes back as an HTML 524 from Exa's CDN instead of JSON. So the wait
# is set a few times the mode's real spread and no more: every second beyond that is wall-clock a
# THREE FIELDS, AND THAT IS THE CEILING. Asking Exa for a fourth does not just cost time — it
# destroys the three that work. Retested live 2026-09-03 on `deep-lite`, one query each:
#
#   fields                secs  titles   year     media    the extra field
#   title/year/media       9.0      38   38/38    38/38    -
#   + blurb (<=15 words)  28.5      45    0/45     0/45    0/45   <- never populated
#   + why (free text)     66.4      35    0/35     0/35    0/35   <- never populated
#   + genres (array)      47.0      31   18/31    31/31    0/31   <- never populated
#
# The extra field comes back EMPTY every time, and asking for it makes Exa drop the year and media
# from every title. The year is what TMDB resolution matches on, so that trade is strictly losing.
# (An earlier note blamed HTTP 524 timeouts; those were transient and are not the real reason.)
#
# If more context is ever needed, the article snippets are already in the response and already
# fetched — see `_web_via_search`, which keeps them for the fallback prompt. Do not ask the schema.

# nightly run spends waiting for a search that is not coming, once per hanging seed per user.
_EXA_TIMEOUTS: dict[str, float] = {
    "instant": 20.0,
    "deep-lite": 45.0,
    "deep": 45.0,
}


@dataclass(frozen=True)
class SearchResult:
    """One web result: a title, its URL, and an extracted text snippet for the model to read."""

    title: str
    url: str
    text: str


@dataclass(frozen=True)
class TitleCandidate:
    """One film/series a search result recommended, as extracted by the search provider itself.

    This is what makes the structured path cheap: the provider turns paragraphs of prose into
    ``Silo (2023) [show]``, so the curator's prompt carries ~10 tokens per candidate instead of an
    800-character article block, and every seed's findings fit rather than being rationed by a cap.

    Deliberately just the three fields. A ``why`` field — a one-line reason from the source article,
    which would have given the curator more to match a taste profile against — was measured and
    REMOVED: asking Exa for it made every ``deep-lite`` search exceed Cloudflare's 100s limit and
    return an HTML **524** (5 of 5 attempts, ~125s each), where the same call without it answers 200
    in about 10s with 31-44 titles. See ``_EXA_TITLE_SCHEMA``.

    ``year`` is often None — the extraction can only report a year the article actually printed, and
    a recorded response carried one for 15 of 31 titles. The TMDB resolver has to cope without it.
    """

    title: str
    year: int | None
    media: str  # "movie" or "show"


class WebSearchProvider(Protocol):
    """A web search backend for the ``llm_web`` source. ``name`` labels it in logs/settings."""

    name: str
    # How many results to pull per per-title search. A per-provider number rather than one constant
    # because the right depth follows the provider's economics AND its result quality: see each class.
    results_per_query: int

    def search(self, query: str, *, num_results: int = _DEFAULT_RESULTS) -> list[SearchResult]: ...

    def ping(self) -> str: ...


def extracts_titles(provider: object) -> bool:
    """Whether this provider can return TitleCandidates as well as prose snippets.

    Exa can (its ``outputSchema`` does the extraction server-side, inside the price of the search);
    SearXNG cannot — it is a metasearch proxy with no synthesis of its own, so its snippets still go
    to the curator as prose. Both shapes flow through the same source in ``candidates.py``.
    """
    return callable(getattr(provider, "search_detailed", None))


# The shape Exa is asked to synthesise from the pages it found. Extraction is deliberately
# TASTE-NEUTRAL — "what do these articles recommend", never "what would this person like" — because
# the answer is cached under the seed title and shared across every user on the server. Put a taste
# profile in the systemPrompt and that cache stops being shareable, which is what makes the per-title
# approach affordable at all. Personalisation stays in the curator, which reads the whole union.
# KEEP THIS SCHEMA SMALL. Adding a fourth field — a one-line "why" reason per title — made every
# `deep-lite` search time out at Cloudflare and return an HTML 524 after ~125s, 5 attempts out of 5,
# while the identical request without it answered 200 in ~10s. Exa's synthesis cost scales with what
# you ask it to write, and the ceiling is the CDN's, not ours: there is no timeout we can raise. Any
# new field here needs the same live check before it ships.
_EXA_TITLE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "titles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "year": {"type": ["integer", "null"]},
                    "media": {"type": "string", "enum": ["movie", "show"]},
                },
                "required": ["title", "media"],
            },
        }
    },
    "required": ["titles"],
}
_EXA_SYSTEM_PROMPT = (
    "List every movie or TV series recommended as something to watch next in these sources. "
    "Exclude the title the reader has already watched. Give each one's exact release year and "
    "whether it is a movie or a series. Be exhaustive — list all of them, not just the best few."
)


class ExaClient:
    """Exa semantic search (https://exa.ai). Returns ranked web results with extracted text.

    A search is a read, but Exa exposes it as POST, so it goes through ``http_retry.request`` — which
    retries the safe cases (a connect failure that never landed, or an explicit 429 rate-limit) and
    leaves the rest to the source's own try/except in ``candidates.py``. The API key travels in the
    ``x-api-key`` header (never the URL/query), so it can't leak into a logged request line (rule 9).

    Every search also carries an ``outputSchema``, so one request returns both the page text and the
    titles those pages recommend. The synthesis is free in money — measured across every mode,
    ``costDollars`` came back identical to the bare search — but NOT free in time: it is what pushes
    a `deep-lite` search from 4s to ~10s, and asking for one more field per title pushed it past
    Cloudflare's limit entirely (see ``_EXA_TITLE_SCHEMA``).
    """

    name = "exa"
    # Ten, the free ceiling: Exa bills results past 10 at $1/1k on top of the search, and a measured
    # sweep found n=5, 10 and 20 all yielding the same range of usable titles. Asking for fewer saves
    # nothing and asking for more only costs.
    results_per_query = 10

    def __init__(self, api_key: str, *, search_type: str = DEFAULT_EXA_SEARCH_TYPE, timeout: float | None = None):
        self._api_key = api_key
        # An unknown mode would be a 400 from Exa on every seed of every run, so an unrecognised
        # setting falls back to the default rather than failing the source all night.
        self._search_type = search_type if search_type in EXA_SEARCH_TYPES else DEFAULT_EXA_SEARCH_TYPE
        if self._search_type != search_type:
            logger.warning("exa: unknown search type {!r}; using {!r}", search_type, self._search_type)
        # Per mode, not one number: a `deep-lite` search legitimately runs 18s where an `instant`
        # one is done in 3, so a single timeout either cuts the slow mode off or leaves the fast mode
        # waiting half a minute on a search that has already hung. See `_EXA_TIMEOUTS`.
        self._timeout = timeout if timeout is not None else _EXA_TIMEOUTS.get(self._search_type, 45.0)

    def search_detailed(
        self, query: str, *, num_results: int = _DEFAULT_RESULTS
    ) -> tuple[list[SearchResult], list[TitleCandidate]]:
        """Run one search, returning both the page snippets and the titles Exa extracted from them.

        Args:
            query: The natural-language search query (built from the user's watchlist upstream).
            num_results: How many web results to ask Exa for.

        Returns:
            ``(results, titles)``. Either half can be empty without the other being: a page set with
            no recommendations in it yields no titles, and a synthesis that fails still leaves the
            snippets, which the caller falls back to.
        """
        payload = self._post(query, num_results)
        results: list[SearchResult] = []
        for item in payload.get("results", []):
            title = (item.get("title") or "").strip()
            if not title:
                continue
            results.append(SearchResult(title=title, url=item.get("url") or "", text=(item.get("text") or "").strip()))
        titles = _parse_extracted_titles(payload)
        logger.debug("exa {} · {!r} → {} results, {} titles", self._search_type, query[:60], len(results), len(titles))
        return results, titles

    def search(self, query: str, *, num_results: int = _DEFAULT_RESULTS) -> list[SearchResult]:
        """Run one search and return up to ``num_results`` results with extracted text.

        Args:
            query: The natural-language search query (built from the user's watchlist upstream).
            num_results: How many web results to ask Exa for.

        Returns:
            The parsed results, newest/most-relevant first. Results with no title are skipped.
        """
        return self.search_detailed(query, num_results=num_results)[0]

    def _post(self, query: str, num_results: int) -> dict:
        """One raw search response. Raises for status; a non-JSON body raises too and is caught by
        the source's own guard — Exa answered 200 with an unparseable body once during testing."""
        response = http_retry.request(
            "POST",
            EXA_SEARCH_URL,
            headers={"x-api-key": self._api_key, "Content-Type": "application/json"},
            json={
                "query": query,
                "numResults": num_results,
                "type": self._search_type,
                "contents": {"text": {"maxCharacters": _DEFAULT_MAX_CHARS}},
                "outputSchema": _EXA_TITLE_SCHEMA,
                "systemPrompt": _EXA_SYSTEM_PROMPT,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def ping(self) -> str:
        """A cheap probe for the Settings 'test connection' button. Raises on an unusable key."""
        results = self.search("popular movies and TV shows to watch this week", num_results=1)
        return f"ok — {len(results)} result"


def _parse_extracted_titles(payload: dict) -> list[TitleCandidate]:
    """Pull the schema-shaped synthesis out of an Exa response.

    Shape recorded from a real response (``tests/fixtures/exa_search_structured.json``): the
    synthesis lands under ``output.content``, beside an ``output.grounding`` list giving per-field
    citations. Nothing here trusts that it is present — a mode that declines to synthesise, or a
    response shape change, leaves the snippets intact and simply contributes no titles.
    """
    content = (payload.get("output") or {}).get("content")
    if not isinstance(content, dict):
        return []
    out: list[TitleCandidate] = []
    for item in content.get("titles") or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        year = item.get("year")
        media = "show" if str(item.get("media") or "").lower() in ("show", "tv", "series") else "movie"
        out.append(TitleCandidate(title=title, year=int(year) if isinstance(year, int) else None, media=media))
    return out


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
            # `safesearch=1` is free: measured against a real instance it returned an identical
            # yield (12 usable titles either way), and a "what to watch next" recommender has no
            # business surfacing adult results. `safesearch=2` costs a title and is not worth it.
            #
            # `time_range` is deliberately absent, and that is a measured decision rather than an
            # omission. It looked like a large win on one seed (Severance: 12 → 20 usable) and is a
            # serious loss on another (Poor Things: 18 → 15 at `year`, → 8 at `month`), because a
            # recency window cuts out exactly the evergreen "best of" articles that are an older
            # title's only coverage. `language` changed nothing, and `pageno` 2 adds results the
            # prompt's own snippet cap then discards.
            params={"q": query, "format": "json", "categories": "general", "safesearch": 1},
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
