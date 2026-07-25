"""Candidate discovery from one or more configurable sources.

Every source returns "titles this person might like"; the pool is the union, deduped by
(tmdb_id, media_type) with seed provenance kept. Which sources run is owner-configurable, so the
recommendation engine is not locked to TMDB's per-seed similarity. Sources today:

* ``tmdb_similar`` — TMDB /recommendations + /similar for each seed (the recall baseline).
* ``tmdb_discover`` — TMDB /discover in the genres a person's history skews toward (widens recall).
* ``trakt`` — Trakt's related-titles graph for each seed.
* ``llm_web`` — a live web search proposes titles to watch next, each resolved via TMDB search.
  Backed by the curator's own web-search tool (Claude/GPT/Gemini) or an external provider (Exa),
  chosen by ``web_search_provider`` — Exa is the only path for a local Ollama model. This is the
  only AI-powered source: the providers are used to FIND titles, never to rank them.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from loguru import logger

from shortlist.engine.clients.search import SearchResult
from shortlist.engine.clients.tmdb import Cache, NullCache, TmdbClient
from shortlist.engine.curator import NullCurator
from shortlist.engine.curator.base import build_web_query_for_title, build_web_rag_prompt, parse_web_titles
from shortlist.engine.models import Candidate, MediaType, Seed

# One cached web search PER recent title (Exa bills per search): cache the RESULTS by (media, tmdb_id)
# so a title many users watched is searched once server-wide. 14 days — "if you liked X" doesn't
# churn fast, and the request pass runs off the critical path so a slightly stale result is harmless.
WEB_SEARCH_CACHE_TTL_S = 14 * 24 * 3600
_WEB_SEARCH_PER_TITLE = 5  # results per per-title search (many titles → keep each lean for the RAG)
_WEB_SEARCH_MAX_TITLES = 10  # default number of recent titles to search; overridden by recent_count
_WEB_SEARCH_RAG_CAP = 40  # cap the unioned results handed to the curator so the RAG prompt stays bounded

# Every candidate source the engine knows how to run. The owner can enable any subset globally
# (settings ``candidates.sources``) or per row (``collections.candidate_sources``); an unknown value
# is simply ignored by ``gather_candidates``, but the API validates against this set for good errors.
KNOWN_SOURCES = ("tmdb_similar", "tmdb_discover", "trakt", "llm_web")
DEFAULT_SOURCES = ("tmdb_similar",)
_DISCOVER_TOP_GENRES = 3  # how many of a person's dominant genres to widen into
_LLM_WEB_K = 20  # how many titles the web-search LLM proposes (each resolved to TMDB, then verified)
_TRACE_SEEDS_SAMPLE = 12  # per source, how many seeds' queries to record in the trace (display only)
_TRACE_RETURNS_SAMPLE = 25  # per seed, how many returned titles to record in the trace (display only —
# the UI shows the first few and lets you expand the rest, so this is the ceiling on what "expand" reveals)


@dataclass
class GatherStats:
    """AI cost incurred while gathering candidates, so a run can show WHERE its tokens went.

    Keyed by source because only the AI-powered source (``llm_web``) costs tokens — the TMDB/Trakt
    sources add nothing here. ``exa_searches`` is tracked separately on purpose: Exa bills per search
    request, not per token, so it must never be folded into a token total.

    ``exa_cache_hits`` counts searches served from the shared 14-day cache instead of billed. It's
    tracked next to ``exa_searches`` so a run can show "1 searched · 793 from cache" — without it a
    fully-cached run reads ``exa_searches: 1`` and is indistinguishable from a run that searched
    nothing, which is precisely how a warm cache gets misread as a broken source.
    """

    tokens_by_source: dict[str, int] = field(default_factory=dict)
    exa_searches: int = 0
    exa_cache_hits: int = 0
    # A diagnostic record of WHAT each source queried and returned this gather — the raw material of
    # the per-user run trace (``UserRunReport.trace``). Always populated (it's a few bounded lists of
    # strings, negligible next to the network calls a gather makes) and purely for display; the engine
    # never reads it back. Shape: {"sources": [{source, status, contributed, sample, detail}],
    # "web": {mode, searches, rag_system, rag_user, proposed, resolved}}.
    trace: dict = field(default_factory=dict)

    def add_tokens(self, source: str, n: int) -> None:
        """Add a source's token spend (a no-op for 0, e.g. NullCurator or a skipped call)."""
        if n:
            self.tokens_by_source[source] = self.tokens_by_source.get(source, 0) + n


def _web_search_capable(curator, search, mode: str) -> bool:
    """Whether the ``llm_web`` source can actually run for this curator + search backend under ``mode``.

    Gates ``attempted``: a source that CANNOT run (e.g. Ollama with no Exa key) must not register as
    attempted, or the "every source failed" check would misread an incapable setup as a failure.
    """
    native = getattr(curator, "supports_native_web_search", False)
    if mode == "native":
        return native
    if mode == "exa":
        return search is not None
    return native or search is not None  # auto: native tool, else external search


def web_recommendations(
    curator,
    search,
    mode: str,
    profile,
    seeds: list[Seed],
    k: int,
    stats: GatherStats,
    *,
    cache: Cache | None = None,
    recent_count: int = _WEB_SEARCH_MAX_TITLES,
) -> list[dict]:
    """Titles to watch next from a web search, as ``[{title, year, media}]`` for TMDB resolution.

    ``mode`` chooses the search backend:

    * ``native`` — the provider's own web-search tool only (Claude/GPT/Gemini).
    * ``exa`` — the external Exa search only (the only path for a local Ollama model).
    * ``auto`` (default) — use everything configured, UNIONED: the provider's own tool AND Exa when
      both are set up, else whichever one. The two surface largely different titles (measured — barely
      any overlap), so running both roughly doubles the usable pool. Duplicates cost nothing:
      ``gather_candidates`` dedupes by ``(tmdb_id, media_type)`` downstream.

    ``recent_count`` caps how many recent titles the external path searches (one cached search each).
    ``stats`` accumulates this source's token spend (and Exa searches) for per-run AI accounting —
    read ``last_tokens`` right after each LLM call, before the next one overwrites it.
    """
    native = getattr(curator, "supports_native_web_search", False)
    web_trace: dict = {"mode": mode}
    stats.trace["web"] = web_trace
    if mode == "native":
        if not native:
            return []
        recs = curator.recommend_web(profile, seeds, k)
        stats.add_tokens("llm_web", getattr(curator, "last_tokens", 0))
        web_trace["proposed"] = [_rec_label(r) for r in recs]
        return recs
    if mode == "exa":
        return (
            _web_via_search(
                curator, search, profile, seeds, k, stats, web_trace, cache=cache, recent_count=recent_count
            )
            if search is not None
            else []
        )
    # auto: union of every available backend.
    recs: list[dict] = []
    if native:
        recs += curator.recommend_web(profile, seeds, k)
        stats.add_tokens("llm_web", getattr(curator, "last_tokens", 0))
        web_trace["native_proposed"] = [_rec_label(r) for r in recs]
    if search is not None:
        recs += _web_via_search(
            curator, search, profile, seeds, k, stats, web_trace, cache=cache, recent_count=recent_count
        )
    return recs


def _rec_label(rec: dict) -> str:
    """A web recommendation as a display string: ``"Title (2024) [show]"`` — for the run trace."""
    year = rec.get("year")
    media = rec.get("media") or "movie"
    return f"{rec.get('title', '?')}{f' ({year})' if year else ''} [{media}]"


def _web_via_search(
    curator,
    search,
    profile,
    seeds: list[Seed],
    k: int,
    stats: GatherStats,
    web_trace: dict | None = None,
    *,
    cache: Cache | None = None,
    recent_count: int = _WEB_SEARCH_MAX_TITLES,
) -> list[dict]:
    """External-search path: one CACHED web search per recent title, then the curator picks from the
    union. Caching by (media, tmdb_id) means a title many users watched is searched once server-wide —
    Exa bills per search, so this is what keeps the per-title approach affordable across a big roster.

    ``web_trace`` (optional) collects a display record of the queries sent, whether each was cached,
    the RAG prompt handed to the curator, and the titles it proposed — the raw material of the run
    trace. Purely diagnostic; nothing here reads it back.
    """
    cache = cache or NullCache()
    trace_queries: list[dict] = []
    results: list[SearchResult] = []
    seen_urls: set[str] = set()
    for seed in seeds[: max(1, recent_count)]:
        key = f"exasearch:{seed.media_type.value}:{seed.tmdb_id}"
        query = build_web_query_for_title(seed.title)
        cached = cache.get(key)
        if cached is not None:
            stats.exa_cache_hits += 1  # served from the shared cache — not billed (see GatherStats)
            items = json.loads(cached)
        else:
            stats.exa_searches += 1  # a real (uncached) search — count the billable request
            hits = search.search(query, num_results=_WEB_SEARCH_PER_TITLE)
            items = [{"title": r.title, "url": r.url, "text": r.text} for r in hits]
            cache.set(key, json.dumps(items), WEB_SEARCH_CACHE_TTL_S)
        trace_queries.append(
            {"seed": seed.title, "query": query, "cached": cached is not None, "returned": [i["title"] for i in items]}
        )
        for it in items:
            # Dedup by url, but only when there IS one — Exa maps a missing url to "", and deduping
            # on "" would collapse every url-less snippet to a single result, dropping usable context.
            if it["url"] and it["url"] in seen_urls:
                continue
            if it["url"]:
                seen_urls.add(it["url"])
            results.append(SearchResult(title=it["title"], url=it["url"], text=it["text"]))
    if web_trace is not None:
        web_trace["searches"] = trace_queries
    if not results:
        return []
    system, user = build_web_rag_prompt(profile, results[:_WEB_SEARCH_RAG_CAP], k)
    titles = parse_web_titles(curator.complete(system, user), k)
    stats.add_tokens("llm_web", getattr(curator, "last_tokens", 0))
    if web_trace is not None:
        web_trace["rag_system"] = system
        web_trace["rag_user"] = user
        web_trace["proposed"] = [_rec_label(t) for t in titles]
    return titles


def _seed_genre_ids(tmdb: TmdbClient, seed: Seed) -> set[int]:
    """The seed's own genres, for `genre_coherence`. Cached by the client; a failure just means
    "no opinion" — never a dead source."""
    try:
        return set(tmdb.genre_ids_for(seed.tmdb_id, seed.media_type))
    except Exception as e:
        logger.debug("could not read genres for seed {} ({})", seed.title, type(e).__name__)
        return set()


def genre_coherence(seed_genre_ids: set[int], candidate_genre_ids: list[int]) -> float:
    """How much a candidate stays inside the seed's genres, 0.5..1.0.

    Position in TMDB's list is not enough on its own. `The Pitt` is tagged simply "Drama", and so is
    almost everything it suggests — but Torchwood and The Sandman are ALSO "Sci-Fi & Fantasy", and
    that foreign genre is the whole difference between a medical drama and a fantasy series. TMDB
    still recommends them, fairly high up; nothing in position or rating says they don't belong.

    Measured on genres the candidate has that the seed does NOT, as a share of its own genres — not
    on overlap, which cannot discriminate when every title shares the one broad genre. Floored at
    0.5 so this shades the ranking rather than dominating it, and returns 1.0 (no opinion) whenever
    either side has no genres recorded.
    """
    if not seed_genre_ids or not candidate_genre_ids:
        return 1.0
    foreign = set(candidate_genre_ids) - seed_genre_ids
    return 1.0 - 0.5 * (len(foreign) / len(set(candidate_genre_ids)))


def gather_candidates(
    tmdb: TmdbClient,
    seeds: list[Seed],
    *,
    sources: list[str] | None = None,
    curator=None,
    profile=None,
    trakt=None,
    search=None,
    web_search_mode: str = "auto",
    web_search_cache: Cache | None = None,
    recent_count: int = _WEB_SEARCH_MAX_TITLES,
    stats: GatherStats | None = None,
) -> list[Candidate]:
    """Pool candidates from every enabled source, deduped by (tmdb_id, media_type).

    ``curator``/``profile`` are only needed by the ``llm_web`` source and ``trakt`` by the Trakt
    source; the TMDB sources ignore them. ``search``/``web_search_mode`` drive the ``llm_web``
    source's external-search backend (Exa) — ``search`` is None when no key is configured.

    Pass a ``stats`` (a :class:`GatherStats`) to have the AI token spend of the ``llm_web`` source
    (and Exa searches) accumulated into it, for per-run AI accounting.
    """
    enabled = set(sources) if sources else set(DEFAULT_SOURCES)
    stats = stats if stats is not None else GatherStats()
    pool: dict[tuple[int, MediaType], Candidate] = {}
    attempted: set[str] = set()
    failures: dict[str, str] = {}  # source -> why it failed; named in the raise when ALL of them do
    genre_maps: dict[MediaType, dict[int, str]] = {}

    def genres_for(media_type: MediaType) -> dict[int, str]:
        if media_type not in genre_maps:
            genre_maps[media_type] = tmdb.genre_names(media_type)
        return genre_maps[media_type]

    # The best MEASURED affinity per title. Kept separately from `Candidate.affinity` because the
    # field's default (1.0) is "no ranking information", which is indistinguishable from a source
    # claiming a perfect match — so a title that tmdb_discover also found would otherwise have its
    # measured position overwritten by that neutral default and sail back to the top of the row.
    measured: dict[tuple[int, MediaType], float] = {}

    def add(item: dict, media_type: MediaType, source: str, affinity: float | None = None) -> Candidate:
        key = (item["id"], media_type)
        if key not in pool:
            date = item.get("release_date") or item.get("first_air_date") or ""
            gmap = genres_for(media_type)
            pool[key] = Candidate(
                tmdb_id=item["id"],
                title=item.get("title") or item.get("name") or "",
                media_type=media_type,
                year=int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
                genres=[gmap[g] for g in item.get("genre_ids", []) if g in gmap],
                rating=float(item.get("vote_average") or 0.0),
                vote_count=int(item.get("vote_count") or 0),
            )
        # A title two sources both found belongs to both — it competes in each one's share, and
        # keeps the STRONGEST claim any of them made for it. A source with nothing to claim
        # (`affinity is None`) adds itself to `sources` but never touches the score.
        pool[key].sources.add(source)
        if affinity is not None:
            measured[key] = max(measured.get(key, 0.0), affinity)
            pool[key].affinity = measured[key]
        return pool[key]

    def merge(tmdb_id: int, title: str, media_type: MediaType, year, genres, source: str) -> Candidate:
        """Merge a candidate described by explicit fields (non-TMDB sources) into the pool."""
        key = (tmdb_id, media_type)
        if key not in pool:
            pool[key] = Candidate(
                tmdb_id=tmdb_id, title=title, media_type=media_type, year=year, genres=list(genres or [])
            )
        pool[key].sources.add(source)
        return pool[key]

    # Fetch each media type's genre map once up front (used to name every candidate, whatever source
    # produced it) — never per seed.
    for media_type in {s.media_type for s in seeds}:
        genres_for(media_type)

    # Per-source, per-seed "what we asked for → what came back" records for the trace, so the operator
    # can follow a TMDB/Trakt query the same way they can an AI web search. Each returned title carries
    # its tmdb_id so the disposition pass (in _candidate_pool) can mark it kept/dropped precisely, not
    # by fuzzy title match. Bounded to a sample.
    seed_queries: dict[str, list[dict]] = {}

    def _record_query(source: str, label: str, media: str, returned: list[tuple[int, str]]) -> None:
        rows = seed_queries.setdefault(source, [])
        if len(rows) >= _TRACE_SEEDS_SAMPLE:
            return
        rows.append(
            {
                "seed": label,
                "media": media,
                "returned": [{"tmdb_id": tid, "title": title} for tid, title in returned[:_TRACE_RETURNS_SAMPLE]],
                "total": len(returned),
            }
        )

    def _record_seed_query(source: str, seed: Seed, returned: list[tuple[int, str]]) -> None:
        _record_query(source, seed.title, seed.media_type.value, returned)

    if "tmdb_similar" in enabled:
        attempted.add("tmdb_similar")
        try:
            for seed in seeds:
                seed_genres = _seed_genre_ids(tmdb, seed)
                returned: list[tuple[int, str]] = []
                for item, affinity in tmdb.suggestions(seed.tmdb_id, seed.media_type):
                    coherence = genre_coherence(seed_genres, item.get("genre_ids") or [])
                    add(item, seed.media_type, "tmdb_similar", affinity * coherence).seeds.append(seed)
                    returned.append((int(item.get("id") or 0), item.get("title") or item.get("name") or ""))
                _record_seed_query("tmdb_similar", seed, returned)
        except Exception as e:
            # The only source that used to have no isolation: a TMDB hiccup here killed the user's
            # whole run, discarding the pools every other source had already gathered.
            failures["tmdb_similar"] = f"{type(e).__name__}: {e}"
            logger.warning("tmdb_similar source failed ({}); continuing with the other sources", type(e).__name__)

    if "tmdb_discover" in enabled:
        attempted.add("tmdb_discover")
        try:
            discover_genres: dict[str, list[str]] = {}
            for media_type in {s.media_type for s in seeds}:
                # No seed provenance — this is "in genres you like", not "because you watched X".
                genre_ids = _dominant_genre_ids(tmdb, seeds, media_type)
                gmap = genres_for(media_type)
                genre_names = [gmap[g] for g in genre_ids if g in gmap]
                discover_genres[media_type.value] = genre_names
                returned = []
                for item in tmdb.discover(media_type, genre_ids):
                    add(item, media_type, "tmdb_discover")
                    returned.append((int(item.get("id") or 0), item.get("title") or item.get("name") or ""))
                # Record the returns so the trace can expand this source title-by-title like the others.
                # The "seed" here is the genre set it widened into, not a watched title.
                _record_query("tmdb_discover", " · ".join(genre_names) or "your genres", media_type.value, returned)
            stats.trace["discover_genres"] = discover_genres  # the top genres discover widened into, per type
        except Exception as e:
            # Discover is a supplementary "widen" source: a TMDB hiccup here must never discard the
            # tmdb_similar pool already gathered for this user. Degrade to "no widening", not a failure.
            failures["tmdb_discover"] = f"{type(e).__name__}: {e}"
            logger.warning("tmdb_discover source failed ({}); continuing with the other sources", type(e).__name__)

    # NullCurator isn't an LLM (it's the no-AI stub), so the web-search source needs a real curator;
    # without one it's a no-op — matching the UI, which blocks the toggle.
    llm_ready = curator is not None and not isinstance(curator, NullCurator)
    if "trakt" in enabled and trakt is not None:
        attempted.add("trakt")
        try:
            for seed in seeds:
                returned = []
                for item in trakt.related(seed.tmdb_id, seed.media_type):
                    # Related-to-a-seed, so keep the provenance (a real "because you watched X").
                    cand = merge(
                        item["tmdb_id"], item["title"], seed.media_type, item.get("year"), item.get("genres"), "trakt"
                    )
                    cand.seeds.append(seed)
                    returned.append((int(item["tmdb_id"]), item["title"]))
                _record_seed_query("trakt", seed, returned)
        except Exception as e:
            failures["trakt"] = f"{type(e).__name__}: {e}"
            logger.warning("trakt source failed ({}); continuing with the other sources", type(e).__name__)

    if (
        "llm_web" in enabled
        and llm_ready
        and profile is not None
        and _web_search_capable(curator, search, web_search_mode)
    ):
        attempted.add("llm_web")
        try:
            # Web search (the provider's own tool, or an external search provider like Exa) proposes
            # titles to watch next; each is resolved to a real TMDB id and (later) library-verified,
            # so a hallucinated title simply resolves to nothing rather than reaching a row.
            resolved: list[str] = []
            unresolved: list[str] = []
            # Each resolved proposal WITH its tmdb_id + media, so the disposition pass can mark whether it
            # made this library's shortlist (kept) or fell out (already watched / not in library / lost the
            # ranking cut) — the same fate every TMDB/Trakt return carries. `resolved`/`unresolved` stay as
            # bare labels for the "hallucinated, no match" strike-through; this is the richer join key.
            proposals: list[dict] = []
            for rec in web_recommendations(
                curator,
                search,
                web_search_mode,
                profile,
                seeds,
                _LLM_WEB_K,
                stats,
                cache=web_search_cache,
                recent_count=recent_count,
            ):
                media_type = MediaType.SHOW if rec.get("media") == "show" else MediaType.MOVIE
                found = tmdb.search(rec["title"], media_type, year=rec.get("year"))
                if found:
                    add(found, media_type, "llm_web")
                    resolved.append(_rec_label(rec))
                    proposals.append({"title": _rec_label(rec), "tmdb_id": int(found["id"]), "media": media_type.value})
                else:
                    unresolved.append(_rec_label(rec))  # proposed but no TMDB match — a likely hallucination
            web = stats.trace.setdefault("web", {})
            web["resolved"] = resolved
            web["unresolved"] = unresolved
            web["proposals"] = proposals
        except Exception as e:
            failures["llm_web"] = f"{type(e).__name__}: {e}"
            logger.warning("llm_web source failed ({}); continuing with the other sources", type(e).__name__)

    # One source down is a degradation the other sources absorb. EVERY source down is not: we know
    # nothing about this person tonight, and returning an empty pool would report a cheerful "ok"
    # while quietly leaving yesterday's row in place. Fail loudly instead — the caller isolates it
    # to this one user, and the run report names them.
    if attempted and set(failures) == attempted and not pool:
        detail = "; ".join(f"{source}: {why}" for source, why in sorted(failures.items()))
        raise RuntimeError(f"every candidate source failed — no candidates gathered ({detail})")

    # Per-source contribution, so a run's log shows WHERE candidates came from — e.g.
    # "candidates · tmdb_similar 142, trakt 63, tmdb_discover 40 → 187 unique". A title found by two
    # sources counts under each (its .sources set), so the parts sum to more than the unique total.
    by_source = Counter(source for cand in pool.values() for source in cand.sources)
    breakdown = ", ".join(f"{source} {count}" for source, count in by_source.most_common())
    logger.debug("candidates · {} → {} unique from {} seeds", breakdown or "none", len(pool), len(seeds))
    # A per-source line for the run trace: every source we tried, whether it worked, and how many
    # pooled candidates it contributed (a title several sources found counts under each).
    stats.trace["sources"] = [
        {
            "source": source,
            "status": "failed" if source in failures else "ok",
            "contributed": by_source.get(source, 0),
            "detail": failures.get(source, ""),
            # Per-seed "searched for X → got these back" sample; only the seeded TMDB/Trakt sources
            # have one (discover queries by genre, llm_web records its own queries under `web`).
            "queries": seed_queries.get(source, []),
        }
        for source in sorted(attempted)
    ]
    return list(pool.values())


def _slice_for_llm(items: list[dict], taste_genres: set[str], cap: int) -> list[dict]:
    """Trim a library down to what an LLM can read, favouring titles in the person's taste genres."""
    if len(items) <= cap:
        return items
    return sorted(items, key=lambda it: len(set(it.get("genres") or []) & taste_genres), reverse=True)[:cap]


def _dominant_genre_ids(tmdb: TmdbClient, seeds: list[Seed], media_type: MediaType) -> list[int]:
    """The genres this person's seeds skew toward, weighted by each seed's recency/frequency."""
    counts: Counter[int] = Counter()
    for seed in seeds:
        if seed.media_type is not media_type:
            continue
        for gid in tmdb.genre_ids_for(seed.tmdb_id, seed.media_type):
            counts[gid] += seed.weight
    return [gid for gid, _ in counts.most_common(_DISCOVER_TOP_GENRES)]


def filter_candidates(
    candidates: list[Candidate],
    library_index: dict[MediaType, dict[int, int]],
    *,
    watched_tmdb_ids: set[tuple[int, MediaType]],
    excluded_genres: set[str],
    recent_pick_ids: set[tuple[int, MediaType]],
    dropped: list[tuple[Candidate, str]] | None = None,
) -> list[Candidate]:
    """Intersect with the library and drop watched/excluded/stale titles.

    Titles are identified by (tmdb_id, media_type), never by id alone: TMDB ids are unique only
    WITHIN a namespace, so movie 550 and TV 550 are different titles. Keying on the bare id makes
    watching a film silently blacklist the show that happens to share its number.

    Args:
        candidates: The pooled TMDB candidates.
        library_index: media_type -> {tmdb_id -> ratingKey} built once per run.
        watched_tmdb_ids: (tmdb_id, media_type) this user has already watched.
        excluded_genres: Per-user genre exclusions (case-insensitive).
        recent_pick_ids: (tmdb_id, media_type) recommended within the last N runs (staleness guard).
        dropped: Optional out-list; each dropped candidate is appended as ``(candidate, reason)`` for
            the run trace. Purely observational — it never changes which candidates are kept.
    """
    excluded = {g.lower() for g in excluded_genres}
    kept = []
    for c in candidates:
        rating_key = library_index.get(c.media_type, {}).get(c.tmdb_id)
        if rating_key is None:
            if dropped is not None:
                dropped.append((c, "not_in_your_libraries"))
            continue
        if (c.tmdb_id, c.media_type) in watched_tmdb_ids or (c.tmdb_id, c.media_type) in recent_pick_ids:
            if dropped is not None:
                dropped.append((c, "already_watched"))
            continue
        if excluded and any(g.lower() in excluded for g in c.genres):
            if dropped is not None:
                dropped.append((c, "excluded_genre"))
            continue
        c.rating_key = rating_key
        kept.append(c)
    return kept
