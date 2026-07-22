"""TMDB client: similar + recommendations pooling with a pluggable cache."""

from __future__ import annotations

import json
from typing import ClassVar, Protocol
from urllib.parse import urlencode

from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.models import MediaType

API = "https://api.themoviedb.org/3"
CACHE_TTL_S = 7 * 24 * 3600  # design: (tmdb_id, endpoint) cached 7 days


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
    def __init__(self, api_key: str, *, cache: Cache | None = None, timeout: float = 30.0):
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
        r = http_retry.get(
            f"{API}{path}",
            params={"api_key": self._api_key, **extra},
            timeout=self._timeout,
        )
        if r.status_code == 404:
            return {}
        if r.status_code != 200:
            # Never raise_for_status(): its message embeds the full URL, api_key included
            # (plex-safety rule 9 — secrets never in exception messages).
            raise RuntimeError(f"TMDB API error HTTP {r.status_code} for {path}")
        data = r.json()
        self._cache.set(cache_key, json.dumps(data), CACHE_TTL_S)
        return data

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

        Used to turn an LLM's proposed titles (which come back as strings, not ids) into real
        candidates. Returns the top result in the same shape as ``suggestions`` items (``id``,
        ``title``/``name``, ``genre_ids``, ``vote_average``, dates), so it pools identically.
        """
        query = (title or "").strip()
        if not query:
            return None
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        params: dict[str, object] = {"query": query}
        if year:
            params["year" if media_type is MediaType.MOVIE else "first_air_date_year"] = year
        results = self._get(f"/search/{kind}", params=params).get("results", [])
        return results[0] if results else None

    def genre_names(self, media_type: MediaType) -> dict[int, str]:
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        data = self._get(f"/genre/{kind}/list")
        return {g["id"]: g["name"] for g in data.get("genres", [])}

    def genre_ids_for(self, tmdb_id: int, media_type: MediaType) -> list[int]:
        """A title's own genre ids — used to derive a person's dominant genres for discover."""
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        data = self._get(f"/{kind}/{tmdb_id}")
        return [g["id"] for g in data.get("genres", []) if isinstance(g, dict) and "id" in g]

    def discover(
        self, media_type: MediaType, genre_ids: list[int], *, min_votes: int = 200, page: int = 1
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
