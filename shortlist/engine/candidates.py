"""Candidate discovery from one or more configurable sources.

Every source returns "titles this person might like"; the pool is the union, deduped by
(tmdb_id, media_type) with seed provenance kept. Which sources run is owner-configurable, so the
recommendation engine is not locked to TMDB's per-seed similarity. Sources today:

* ``tmdb_similar`` — TMDB /recommendations + /similar for each seed (the recall baseline).
* ``tmdb_discover`` — TMDB /discover in the genres a person's history skews toward (widens recall).
* ``llm_library`` — the AI curator proposes owned titles from a taste-sliced library catalog.

More sources (Trakt, LLM+web-search) plug in as additional branches here.
"""

from __future__ import annotations

from collections import Counter

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.curator import NullCurator
from shortlist.engine.models import Candidate, MediaType, Seed

# Every candidate source the engine knows how to run. The owner can enable any subset globally
# (settings ``candidates.sources``) or per row (``collections.candidate_sources``); an unknown value
# is simply ignored by ``gather_candidates``, but the API validates against this set for good errors.
KNOWN_SOURCES = ("tmdb_similar", "tmdb_discover", "llm_library", "trakt")
DEFAULT_SOURCES = ("tmdb_similar",)
_DISCOVER_TOP_GENRES = 3  # how many of a person's dominant genres to widen into
_LLM_LIBRARY_CAP = 300  # most catalog titles to show the LLM (a big library must be sliced to fit)
_LLM_LIBRARY_K = 40  # how many owned titles the LLM proposes as candidates


def gather_candidates(
    tmdb: TmdbClient,
    seeds: list[Seed],
    *,
    sources: list[str] | None = None,
    curator=None,
    catalog: dict[MediaType, list[dict]] | None = None,
    profile=None,
    trakt=None,
) -> list[Candidate]:
    """Pool candidates from every enabled source, deduped by (tmdb_id, media_type).

    ``curator``/``catalog``/``profile`` are only needed by the ``llm_library`` source and ``trakt``
    by the Trakt source; the TMDB sources ignore them.
    """
    enabled = set(sources) if sources else set(DEFAULT_SOURCES)
    pool: dict[tuple[int, MediaType], Candidate] = {}
    genre_maps: dict[MediaType, dict[int, str]] = {}

    def genres_for(media_type: MediaType) -> dict[int, str]:
        if media_type not in genre_maps:
            genre_maps[media_type] = tmdb.genre_names(media_type)
        return genre_maps[media_type]

    def add(item: dict, media_type: MediaType) -> Candidate:
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
        return pool[key]

    def merge(tmdb_id: int, title: str, media_type: MediaType, year, genres) -> Candidate:
        """Merge a candidate described by explicit fields (non-TMDB sources) into the pool."""
        key = (tmdb_id, media_type)
        if key not in pool:
            pool[key] = Candidate(
                tmdb_id=tmdb_id, title=title, media_type=media_type, year=year, genres=list(genres or [])
            )
        return pool[key]

    # Fetch each media type's genre map once up front (used to name every candidate, whatever source
    # produced it) — never per seed.
    for media_type in {s.media_type for s in seeds}:
        genres_for(media_type)

    if "tmdb_similar" in enabled:
        for seed in seeds:
            for item in tmdb.suggestions(seed.tmdb_id, seed.media_type):
                add(item, seed.media_type).seeds.append(seed)

    if "tmdb_discover" in enabled:
        try:
            for media_type in {s.media_type for s in seeds}:
                # No seed provenance — this is "in genres you like", not "because you watched X".
                for item in tmdb.discover(media_type, _dominant_genre_ids(tmdb, seeds, media_type)):
                    add(item, media_type)
        except Exception as e:
            # Discover is a supplementary "widen" source: a TMDB hiccup here must never discard the
            # tmdb_similar pool already gathered for this user. Degrade to "no widening", not a failure.
            logger.warning("tmdb_discover source failed ({}); continuing with the other sources", type(e).__name__)

    # NullCurator isn't AI (it ranks heuristically), so "AI suggests from your library" needs a real
    # curator; without one the source is a no-op — matching the UI, which blocks the toggle.
    llm_ready = curator is not None and not isinstance(curator, NullCurator)
    if "trakt" in enabled and trakt is not None:
        try:
            for seed in seeds:
                for item in trakt.related(seed.tmdb_id, seed.media_type):
                    # Related-to-a-seed, so keep the provenance (a real "because you watched X").
                    cand = merge(item["tmdb_id"], item["title"], seed.media_type, item.get("year"), item.get("genres"))
                    cand.seeds.append(seed)
        except Exception as e:
            logger.warning("trakt source failed ({}); continuing with the other sources", type(e).__name__)

    if "llm_library" in enabled and llm_ready and catalog and profile is not None:
        try:
            # Taste = the genres already in this person's pool; used only to slice a big library down
            # to what the LLM can read. The curator then picks the owned titles that actually fit.
            taste = {g for c in pool.values() for g in c.genres}
            for media_type, items in catalog.items():
                owned = [
                    Candidate(
                        tmdb_id=it["tmdb_id"],
                        title=it["title"],
                        media_type=media_type,
                        year=it.get("year"),
                        genres=list(it.get("genres") or []),
                        rating_key=it.get("rating_key"),
                    )
                    for it in _slice_for_llm(items, taste, _LLM_LIBRARY_CAP)
                ]
                chosen = {p.tmdb_id for p in curator.curate(profile, owned, _LLM_LIBRARY_K)}
                for cand in owned:
                    if cand.tmdb_id in chosen:
                        pool.setdefault((cand.tmdb_id, media_type), cand)
        except Exception as e:
            logger.warning("llm_library source failed ({}); continuing with the other sources", type(e).__name__)

    logger.debug("candidate pool: {} unique titles from {} seeds via {}", len(pool), len(seeds), sorted(enabled))
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
    """
    excluded = {g.lower() for g in excluded_genres}
    kept = []
    for c in candidates:
        rating_key = library_index.get(c.media_type, {}).get(c.tmdb_id)
        if rating_key is None:
            continue
        if (c.tmdb_id, c.media_type) in watched_tmdb_ids or (c.tmdb_id, c.media_type) in recent_pick_ids:
            continue
        if excluded and any(g.lower() in excluded for g in c.genres):
            continue
        c.rating_key = rating_key
        kept.append(c)
    return kept
