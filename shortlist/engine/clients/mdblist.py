"""MDBList client: one title's ratings (IMDb, Trakt, Rotten Tomatoes, Metacritic, TMDB) by TMDB id.

Used when the owner picks a non-TMDB rating source for Sonarr/Radarr requests. A single lookup
returns EVERY source's score at once, so we cache the whole set per title (persistent, cross-run):
a title re-scored on a later night — or the same title under a different chosen source — is a cache
hit, not another API call. That matters because MDBList's free tier is ~1000 requests/day; see
``rating`` and the ``MdbListRateLimitError`` the caller turns into a user-facing alert.
"""

from __future__ import annotations

import json

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.clients.tmdb import Cache, NullCache
from shortlist.engine.models import MediaType

API = "https://api.mdblist.com"
RATING_CACHE_TTL_S = 7 * 24 * 3600  # ratings barely move in a week; long TTL keeps us under the daily cap

# The MDBList `ratings[].source` names we surface, mapped to the app's rating_source values. RT is
# "tomatoes" in MDBList. TMDB is included so a title fetched for one source also warms the TMDB score.
KNOWN_SOURCES = ("imdb", "trakt", "tmdb", "tomatoes", "metacritic")
# Sources whose `votes` is a real audience-vote count worth enforcing a floor on. Rotten Tomatoes and
# Metacritic are CRITIC scores (a handful of reviews), so a large min_votes must not filter them out.
VOTE_SOURCES = frozenset({"imdb", "trakt", "tmdb"})
# Sources MDBList reports on a 0..100 scale; the rest are already 0..10. Scaled per-source (not by
# magnitude) so a genuinely low RT/Metacritic score — e.g. RT 8% — normalises to 0.8, not 8.0.
SCALE_100 = frozenset({"tomatoes", "metacritic"})


class MdbListError(RuntimeError):
    """An MDBList call failed. Never carries the api key (plex-safety rule 9)."""


class MdbListRateLimitError(MdbListError):
    """MDBList returned 429 — the daily request quota is spent. The caller stops looking up further
    titles this run, falls back to TMDB ratings, and alerts the owner."""


class MdbListClient:
    def __init__(self, api_key: str, *, cache: Cache | None = None, timeout: float = 15.0):
        self._api_key = api_key
        self._cache = cache or NullCache()
        self._timeout = timeout

    def rating(self, tmdb_id: int, media_type: MediaType, source: str) -> tuple[float, int] | None:
        """(rating 0..10, votes) for ``source`` on this title, or None if that source has no score.

        Every source is fetched and cached together, so this is one API call per title regardless of
        which source is asked for, and zero calls once cached. Raises ``MdbListRateLimitError`` when
        the quota is spent (so the caller can alert); any other failure returns None (soft miss).
        """
        key = f"{media_type.value}:{tmdb_id}"
        cached = self._cache.get(key)
        if cached is not None:
            by_source = json.loads(cached)
        else:
            by_source = self._fetch_all(tmdb_id, media_type)
            if by_source is None:
                return None
            self._cache.set(key, json.dumps(by_source), RATING_CACHE_TTL_S)
        entry = by_source.get(source)
        return (entry[0], entry[1]) if entry else None

    def _fetch_all(self, tmdb_id: int, media_type: MediaType) -> dict[str, list] | None:
        """Fetch every source's (rating, votes) for one title, normalised to a 0..10 scale.

        Returns ``{source: [rating, votes]}`` for each source with a numeric score, or None on a soft
        failure. Raises ``MdbListRateLimitError`` on 429.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "show"
        try:
            r = http_retry.get(f"{API}/tmdb/{kind}/{tmdb_id}", params={"apikey": self._api_key}, timeout=self._timeout)
        except httpx.HTTPError as e:
            logger.warning("MDBList unreachable for {} {}: {}", kind, tmdb_id, type(e).__name__)
            return None
        if r.status_code == 429:
            raise MdbListRateLimitError("MDBList daily request limit reached")
        if r.status_code != 200:
            logger.warning("MDBList returned HTTP {} for {} {}", r.status_code, kind, tmdb_id)
            return None
        try:
            data = r.json()
        except ValueError:  # a 200 with a non-JSON body (proxy/error page)
            return None
        out: dict[str, list] = {}
        for entry in data.get("ratings", []) if isinstance(data, dict) else []:
            if not isinstance(entry, dict):
                continue
            source = str(entry.get("source", ""))
            if source not in KNOWN_SOURCES:
                continue
            rating = _normalise(entry.get("value"), source)
            if rating is None:
                continue
            votes = _parse_int(entry.get("votes")) if source in VOTE_SOURCES else 0
            out[source] = [rating, votes or 0]
        return out

    def usage(self) -> tuple[int, int] | None:
        """(requests used today, daily allowance) from /user, or None if it can't be read — for the
        'Test' button and the early low-quota warning."""
        try:
            r = http_retry.get(f"{API}/user", params={"apikey": self._api_key}, timeout=self._timeout)
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        try:
            data = r.json()
        except ValueError:
            return None
        used = _parse_int(data.get("api_requests_count"))
        limit = _parse_int(data.get("api_requests"))
        return (used or 0, limit) if limit is not None else None

    def ping(self) -> str:
        """A tiny authenticated call for the settings 'Test' button; raises on a bad key."""
        try:
            r = http_retry.get(f"{API}/user", params={"apikey": self._api_key}, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise MdbListError(f"MDBList unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise MdbListError("MDBList rejected the API key")
        if r.status_code != 200:
            raise MdbListError(f"MDBList returned HTTP {r.status_code}")
        usage = self.usage()
        return f"Connected — {usage[0]} of {usage[1]} requests used today" if usage else "Connected to MDBList"


def _normalise(value: object, source: str) -> float | None:
    """A rating on a 0..10 scale. RT/Metacritic are 0..100 in MDBList (divide by 10); IMDb/Trakt/TMDB
    are already 0..10. Scaled by SOURCE, not magnitude — a real RT 8% must land at 0.8, not 8.0, or a
    panned title would clear the floor."""
    try:
        rating = float(str(value))
    except (TypeError, ValueError):
        return None
    if rating <= 0:
        return None
    return round(rating / 10, 1) if source in SCALE_100 else rating


def _parse_int(value: object) -> int | None:
    """Vote counts arrive as ints or thousands-separated strings; None/'N/A' when absent."""
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
