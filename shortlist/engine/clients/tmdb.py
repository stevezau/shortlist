"""TMDB client: similar + recommendations pooling with a pluggable cache."""

from __future__ import annotations

import contextvars
import json
from concurrent.futures import ThreadPoolExecutor
from typing import ClassVar, Protocol
from urllib.parse import urlencode

from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.models import MediaType

API = "https://api.themoviedb.org/3"
CACHE_TTL_S = 7 * 24 * 3600  # design: (tmdb_id, endpoint) cached 7 days
#: The vote floor for a whole-genre discover query. A genre holds tens of thousands of titles; this keeps
#: to ones enough people have rated for a score to mean something.
DISCOVER_MIN_VOTES = 200
#: TMDB serves at most this many pages of a list: page 501 answers HTTP 400
#: (tests/fixtures/tmdb_discover_paged.json).
MAX_DISCOVER_PAGES = 500
#: Pages of one list read at once. Each read is its own `httpx.request`, so they share no client state.
_LIST_PAGE_WORKERS = 4
#: What `discover_all` keeps of each title: the fields `candidates.gather_candidates` builds a candidate
#: from. A Christmas list is ~3,700 titles, and overviews alone would make its cache entry megabytes.
_LIST_FIELDS = (
    "id",
    "title",
    "name",
    "release_date",
    "first_air_date",
    "genre_ids",
    "vote_average",
    "vote_count",
    "poster_path",
    "original_language",
)


class Cache(Protocol):
    """Minimal cache the adapter provides (a DB table for the server)."""

    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_s: int) -> None: ...


class NullCache:
    def get(self, key: str) -> str | None:
        return None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        return None


class TmdbClient:
    def __init__(
        self,
        api_key: str,
        *,
        cache: Cache | None = None,
        # TMDB is fast and well-provisioned; the shared ceiling needs no override.
        timeout: float = http_retry.DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key
        self._cache = cache or NullCache()
        self._timeout = timeout

    def _get(self, path: str, *, params: dict | None = None) -> dict:
        extra = params or {}
        # The cache keys on path + query (never the api_key): two discover queries that differ only
        # in their genres must cache separately, and the secret must not sit in a cache key.
        cache_key = "tmdb:" + path + (("?" + urlencode(sorted(extra.items()))) if extra else "")
        if cached := self._cache.get(cache_key):
            logger.trace("tmdb cache hit · {}", path)
            return json.loads(cached)
        data = self._fetch(path, extra)
        # Cache the miss too (like trakt.py's `related()` deliberately does): without this, a title TMDB
        # doesn't have is re-fetched every run for every user who has it as a seed. A 404 reads as {}.
        self._cache.set(cache_key, json.dumps(data), CACHE_TTL_S)
        return data

    def _fetch(self, path: str, params: dict) -> dict:
        """One uncached read. {} for a 404; raises for any other failure."""
        r = http_retry.get(
            f"{API}{path}",
            params={"api_key": self._api_key, **params},
            timeout=self._timeout,
        )
        if r.status_code == 404:
            return {}
        if r.status_code != 200:
            # Never raise_for_status(): its message embeds the full URL, api_key included
            # (plex-safety rule 9 — secrets never in exception messages).
            raise RuntimeError(f"TMDB API error HTTP {r.status_code} for {path}")
        return r.json()

    def ping(self) -> bool:
        return bool(self._get("/configuration"))

    # How much each endpoint's vouching is worth, and how fast that decays down its list.
    #
    # `/recommendations` is built from what people actually watch together and is reliably good at
    # the top; `/similar` is genre+keyword matching and gets noisy fast — for "The Pitt" (a medical
    # drama) it returns Torchwood and The Sandman partway down. Pooling the two and forgetting where
    # each title sat cost us the whole signal: position IS the similarity claim, and without it the
    # only thing left to rank on was TMDB's average rating, which is how a well-rated but unrelated
    # show beat an obviously-similar one.
    _ENDPOINT_WEIGHT: ClassVar[dict[str, float]] = {"recommendations": 1.0, "similar": 0.6}
    _POSITION_DECAY = 0.5  # the bottom of a list is worth half its top

    def suggestions(self, tmdb_id: int, media_type: MediaType) -> list[tuple[dict, float]]:
        """Pooled /recommendations + /similar for one seed, each with an affinity in (0, 1].

        Affinity is "how strongly TMDB vouched for this title for this seed": which endpoint it came
        from, and how near the top of that endpoint's list it sat. A title both endpoints return
        keeps the better of the two.

        Returns:
            ``(item, affinity)`` pairs, best first.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        pooled: dict[int, tuple[dict, float]] = {}
        for endpoint, weight in self._ENDPOINT_WEIGHT.items():
            results = self._get(f"/{kind}/{tmdb_id}/{endpoint}").get("results", [])
            last = max(len(results) - 1, 1)
            for position, item in enumerate(results):
                affinity = weight * (1 - self._POSITION_DECAY * position / last)
                previous = pooled.get(item["id"])
                if previous is None or affinity > previous[1]:
                    pooled[item["id"]] = (item, affinity)
        logger.debug("TMDB suggestions for {} {}: {} pooled", kind, tmdb_id, len(pooled))
        return sorted(pooled.values(), key=lambda pair: -pair[1])

    def search(self, title: str, media_type: MediaType, *, year: int | None = None) -> dict | None:
        """Resolve a free-text title to its best TMDB match, or None if nothing matches.

        Used to turn proposed titles (which arrive as strings, not ids) into real candidates. Returns
        one result in the same shape as ``suggestions`` items (``id``, ``title``/``name``,
        ``genre_ids``, ``vote_average``, dates), so it pools identically.

        The year is used to RANK, never to filter. Filtering server-side looks tidier and is worse in
        both directions: a proposal whose year is off by one — common, because sources date a series
        by its premiere and a film by its festival run — returns nothing at all and the title is lost,
        while a proposal with no year (about half of what web extraction produces, since articles
        often don't print one) gets no help whatever. Ranking keeps the near-misses and still puts
        the right release first.

        Args:
            title: The proposed title, as written by a model or extracted from an article.
            media_type: Which TMDB index to search.
            year: Release year, if known. Used to break ties, not to exclude.

        Returns:
            The best match, or None when the search returned nothing at all.
        """
        query = (title or "").strip()
        if not query:
            return None
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        results = self._get(f"/search/{kind}", params={"query": query}).get("results", [])
        if not results:
            return None
        # `max` keeps the first of equal scores, so a tie falls back to TMDB's own popularity order —
        # which is what this function used to return outright.
        return max(results, key=lambda r: _match_score(r, query, year))

    def genre_names(self, media_type: MediaType) -> dict[int, str]:
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        data = self._get(f"/genre/{kind}/list")
        return {g["id"]: g["name"] for g in data.get("genres", [])}

    def details(self, tmdb_id: int, media_type: MediaType) -> dict:
        """One title's genres, franchise collection and top-billed cast, from a SINGLE cached call.

        `append_to_response=credits` folds in the `/credits` sub-resource TMDB would otherwise need a
        second round trip for, and `belongs_to_collection` already rides on the bare detail payload.
        One method and one cache entry serve the genre, franchise and cast lookups alike — three
        separate per-title fetches would each carry their own cache-key shape and triple the requests
        for data that arrives together anyway.

        Adding the parameter changes the cache key (`_get` keys on path + query), so entries written
        before this existed simply age out on the normal TTL rather than colliding.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        return self._get(f"/{kind}/{tmdb_id}", params={"append_to_response": "credits"})

    def genre_ids_for(self, tmdb_id: int, media_type: MediaType) -> list[int]:
        """A title's own genre ids — used to derive a person's dominant genres for discover."""
        data = self.details(tmdb_id, media_type)
        return [g["id"] for g in data.get("genres", []) if isinstance(g, dict) and "id" in g]

    def collection_members(self, collection_id: int) -> set[int]:
        """Every movie tmdb_id in a TMDB collection (a "franchise"), from ONE cached call.

        Movie-only by TMDB's own schema — `belongs_to_collection` does not exist for TV, so the
        franchise signal is inert for shows by construction rather than by choice.
        """
        data = self._get(f"/collection/{collection_id}")
        return {p["id"] for p in data.get("parts", []) if isinstance(p, dict) and "id" in p}

    def top_cast(self, tmdb_id: int, media_type: MediaType, limit: int = 5) -> list[str]:
        """The top-billed cast names, in TMDB's billing order.

        Billing order, not popularity: the leads are what make two titles feel related, and an
        ensemble's twentieth credit is noise that would dilute every overlap it touches.
        """
        credits = self.details(tmdb_id, media_type).get("credits") or {}
        cast = credits.get("cast") or []
        return [c["name"] for c in cast[:limit] if isinstance(c, dict) and c.get("name")]

    def discover(
        self, media_type: MediaType, genre_ids: list[int], *, min_votes: int = DISCOVER_MIN_VOTES, page: int = 1
    ) -> list[dict]:
        """Popular, well-reviewed titles in the given genres — the 'discover by taste' source.

        Params go through ``_get(params=…)``, which keys the cache on path + query — so two discover
        queries that differ only by genre cache separately, and the api_key never lands in a key.
        """
        if not genre_ids:
            return []
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        params = {
            "with_genres": ",".join(str(g) for g in genre_ids),
            "sort_by": "popularity.desc",
            "vote_count.gte": min_votes,
            "page": page,
        }
        return self._get(f"/discover/{kind}", params=params).get("results", [])

    def discover_all(self, media_type: MediaType, params: dict) -> list[dict]:
        """Every title a discover query matches, read to its last page and cached as ONE entry.

        What a season's list is read with (discussion #124). Three things about it are measured, not
        chosen (tests/fixtures/tmdb_discover_paged.json):

        * the order is RELEASE DATE. Paged by popularity, a list loses titles between page reads because
          popularity moves while it is being read — 3,133 unique titles out of 3,370 for Christmas;
        * pages stop at 500, because TMDB answers HTTP 400 past that;
        * the result is cached whole, never per page: pages cached at different moments expire at
          different moments and would be re-read against a list that has shifted since.

        Raises if any page fails, so a partial list is never returned or cached.

        Args:
            media_type: Films or shows.
            params: The query itself, e.g. ``{"with_keywords": "3335|9694"}``. Sort order and the adult
                filter are added here.

        Returns:
            The matching titles, de-duplicated by id, each reduced to ``_LIST_FIELDS``.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        path = f"/discover/{kind}"
        query = {
            **params,
            "sort_by": "primary_release_date.asc" if media_type is MediaType.MOVIE else "first_air_date.asc",
            "include_adult": "false",
        }
        cache_key = "tmdb:all:" + path + "?" + urlencode(sorted((k, str(v)) for k, v in query.items()))
        if cached := self._cache.get(cache_key):
            return json.loads(cached)

        def read(page: int) -> dict:
            # `_fetch` answers a 404 with {}, which is right for a title TMDB lacks and wrong here: taken as
            # an empty page it would cache a short list for a week.
            data = self._fetch(path, {**query, "page": page})
            if "results" not in data:
                raise RuntimeError(f"TMDB list {path} returned no page {page}")
            return data

        first = read(1)
        total_pages = int(first.get("total_pages") or 1)
        if total_pages > MAX_DISCOVER_PAGES:
            logger.warning(
                "tmdb list {} has {} pages; TMDB serves {}, so the rest of it is not read",
                path,
                total_pages,
                MAX_DISCOVER_PAGES,
            )
        last = min(total_pages, MAX_DISCOVER_PAGES)
        pages = [first]
        if last > 1:
            numbers = range(2, last + 1)
            # In copies of the caller's contextvars, so a page read's warnings stay with the run that
            # asked for them (see `pipeline._deliver_phase`); one copy per call, as a context can be
            # entered by only one thread at a time.
            contexts = [contextvars.copy_context() for _ in numbers]
            with ThreadPoolExecutor(max_workers=_LIST_PAGE_WORKERS) as pool:
                pages += pool.map(lambda context, page: context.run(read, page), contexts, numbers)
        titles: dict[int, dict] = {}
        for page in pages:
            for item in page.get("results") or []:
                titles.setdefault(item["id"], {field: item[field] for field in _LIST_FIELDS if field in item})
        result = list(titles.values())
        logger.debug("tmdb list {} · {} titles over {} pages", path, len(result), last)
        self._cache.set(cache_key, json.dumps(result), CACHE_TTL_S)
        return result

    def external_ids(self, tmdb_id: int, media_type: MediaType) -> dict:
        """A title's ids in other databases (``tvdb_id``, ``imdb_id``, …); {} if TMDB has none."""
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        return self._get(f"/{kind}/{tmdb_id}/external_ids") or {}

    def tvdb_id(self, tmdb_id: int, media_type: MediaType) -> int | None:
        """The TheTVDB id for a title, or None if TMDB doesn't have one.

        Sonarr keys every show on its TVDB id, but Shortlist only ever knows the TMDB id — so a show
        request has to cross that namespace here first. Movies never need this (Radarr keys on
        tmdbId directly), and a show with no TVDB mapping simply can't be requested from Sonarr.
        """
        raw = self.external_ids(tmdb_id, media_type).get("tvdb_id")
        return int(raw) if raw else None

    def imdb_id(self, tmdb_id: int, media_type: MediaType) -> str | None:
        """The IMDb id (``tt…``) for a title, or None — used for the inbox's IMDb deep-link."""
        raw = self.external_ids(tmdb_id, media_type).get("imdb_id")
        return raw or None

    def poster_path(self, tmdb_id: int, media_type: MediaType) -> str:
        """A title's poster path (``/abc.jpg``), or ``""`` when TMDB has no artwork for it.

        Only needed for a title a NON-TMDB source surfaced: every TMDB list response already carries
        ``poster_path``, so the candidate normally arrives with one and this is never called.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        return (self._get(f"/{kind}/{tmdb_id}") or {}).get("poster_path") or ""

    def overview(self, tmdb_id: int, media_type: MediaType) -> str:
        """A title's synopsis, or ``""`` when TMDB has none for it.

        Same terms as :meth:`poster_path` — every TMDB list response already carries ``overview``, so
        this is only reached for a title a non-TMDB source surfaced. It hits the same detail endpoint,
        which the response cache serves, so a title missing both costs one round-trip, not two.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        return (self._get(f"/{kind}/{tmdb_id}") or {}).get("overview") or ""


def _normalise(title: str) -> str:
    """A title reduced to letters and digits, lowercased.

    So "Marvel's Daredevil", the same with a curly apostrophe (what a copy-paste out of an article
    actually contains) and "marvels daredevil" all compare equal. Deliberately crude: this decides
    which of TMDB's own results to prefer, not whether two titles are the same work.
    """
    return "".join(c for c in title.lower() if c.isalnum())


def _match_score(result: dict, query: str, year: int | None) -> tuple[int, int]:
    """Rank one TMDB search result against the title (and year) that was asked for.

    Returns a (title, year) score pair, compared left to right — an exact title match always beats a
    better year, because the year is the less reliable half of a proposal. Sources routinely date a
    series by its premiere and a film by its festival showing, so a year that is one out is a normal
    near-miss, while a title that doesn't match is usually a different work.

    Without this the caller took ``results[0]``, TMDB's popularity order, which is right most of the
    time and quietly wrong for remakes and shared titles — searching "Poor Things (2023)" ahead of
    an unrelated more-popular entry is exactly the case that produced a wrong row.
    """
    name = _normalise(str(result.get("title") or result.get("name") or ""))
    wanted = _normalise(query)
    if name == wanted:
        title_score = 2
    elif wanted and (wanted in name or name in wanted):
        title_score = 1
    else:
        title_score = 0

    if year is None:
        return (title_score, 0)
    date = str(result.get("release_date") or result.get("first_air_date") or "")
    try:
        found = int(date[:4])
    except ValueError:
        return (title_score, 0)
    gap = abs(found - year)
    # One year out is the common honest disagreement, so it still scores — just below an exact hit.
    year_score = 2 if gap == 0 else 1 if gap <= 1 else 0
    return (title_score, year_score)
