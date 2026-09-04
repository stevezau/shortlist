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


#: Consecutive failures before this client gives up on MDBList for the rest of the run.
#:
#: Measured, not guessed. MDBList went down mid-run on 2026-09-04: every lookup burned the full
#: retry ladder (3 attempts x a 15s timeout plus backoff, ~43s each) and returned a soft None, so
#: the run walked its entire 100-lookup budget one dead call at a time — over an hour of a nightly
#: run spent on a provider that was answering nothing, and it would have happened again every night
#: until MDBList came back. Five failures is enough to tell an outage from a blip and costs ~3
#: minutes instead of ~72.
#:
#: Deliberately never re-closes. The client is built once per run, so a new run always gets a fresh
#: circuit — that is the right granularity, and re-probing mid-run would just re-pay the ladder.
_BREAKER_TRIP = 5


class MdbListClient:
    def __init__(
        self,
        api_key: str,
        *,
        cache: Cache | None = None,
        # Shorter than the shared default: a rating is optional enrichment, not required, so a slow
        # MDBList response should fail fast rather than eat run time other sources need.
        timeout: float = 15.0,
    ):
        self._api_key = api_key
        self._cache = cache or NullCache()
        self._timeout = timeout
        # Lookups that actually cost an API call. The daily quota is spent here and NOWHERE else — a
        # cached title is answered from SQLite — so this, not the number of titles inspected, is what
        # a caller rationing the free tier has to budget against. See `requests._gate_by_source`.
        self.live_lookups = 0
        # Circuit breaker. Consecutive transport/5xx failures; at `_BREAKER_TRIP` the client stops
        # calling MDBList for the rest of its life and answers None instantly. See `_fetch_all`.
        self._consecutive_failures = 0
        self._circuit_open = False

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
            # Counted BEFORE the call, and regardless of how it turns out: a request that 404s or
            # times out has still been billed against the daily allowance.
            self.live_lookups += 1
            by_source = self._fetch_all(tmdb_id, media_type)
            if by_source is None:
                return None
            self._cache.set(key, json.dumps(by_source), RATING_CACHE_TTL_S)
        entry = by_source.get(source)
        return (entry[0], entry[1]) if entry else None

    def defer_recheck(self, tmdb_id: int, media_type: MediaType, ttl_s: int) -> None:
        """Keep this title's already-cached ratings for ``ttl_s`` instead of the usual week.

        Re-stamps the TTL on what is already stored; it never fetches, so it costs no quota and
        cannot change a stored score. For a caller that has just judged a title far short of its bar
        and does not want to pay to re-ask soon — see ``requests._gate_by_source``. A title with
        nothing cached is left alone (there is no verdict to hold on to).
        """
        key = f"{media_type.value}:{tmdb_id}"
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.set(key, cached, ttl_s)

    def _note_failure(self, reason: str) -> None:
        """Count a failure and open the circuit once MDBList has clearly stopped answering."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_TRIP and not self._circuit_open:
            self._circuit_open = True
            # WARNING, and said once: this changes what the run produces (titles go unrated, so they
            # are not requested), and an owner seeing thin requests needs the reason without reading
            # a hundred identical timeout lines.
            logger.warning(
                "MDBList has failed {} times in a row ({}); giving up on it for the rest of this run. "
                "Titles will go unrated rather than each one waiting out the retries — roughly an hour "
                "of run time on a full budget. Ratings resume automatically on the next run.",
                self._consecutive_failures,
                reason,
            )

    def _fetch_all(self, tmdb_id: int, media_type: MediaType) -> dict[str, list] | None:
        """Fetch every source's (rating, votes) for one title, normalised to a 0..10 scale.

        Returns ``{source: [rating, votes]}`` for each source with a numeric score, or None on a soft
        failure. Raises ``MdbListRateLimitError`` on 429.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "show"
        if self._circuit_open:
            return None
        try:
            r = http_retry.get(f"{API}/tmdb/{kind}/{tmdb_id}", params={"apikey": self._api_key}, timeout=self._timeout)
        except httpx.HTTPError as e:
            logger.warning("MDBList unreachable for {} {}: {}", kind, tmdb_id, type(e).__name__)
            self._note_failure("unreachable")
            return None
        if r.status_code == 429:
            # A quota verdict, not an outage — it has its own handling and must not trip the breaker,
            # or a rate-limited run would look like a dead provider to the next caller.
            raise MdbListRateLimitError("MDBList daily request limit reached")
        if r.status_code != 200:
            logger.warning("MDBList returned HTTP {} for {} {}", r.status_code, kind, tmdb_id)
            self._note_failure(f"HTTP {r.status_code}")
            return None
        self._consecutive_failures = 0  # a real answer clears the run of failures behind it
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

    def ping(self) -> str:
        """A tiny authenticated call for the settings 'Test' button; raises on a bad key.

        One ``/user`` call: the quota line is parsed from the same response, never a second request.
        (The Test button auto-fires when the settings page opens, so a wasted extra call was billing
        two requests against the daily cap per page view.)
        """
        try:
            r = http_retry.get(f"{API}/user", params={"apikey": self._api_key}, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise MdbListError(f"MDBList unreachable ({type(e).__name__})") from e
        if r.status_code == 401:
            raise MdbListError("MDBList rejected the API key")
        if r.status_code != 200:
            raise MdbListError(f"MDBList returned HTTP {r.status_code}")
        usage = self._parse_usage(r)
        return f"Connected — {usage[0]} of {usage[1]} requests used today" if usage else "Connected to MDBList"

    @staticmethod
    def _parse_usage(response: httpx.Response) -> tuple[int, int] | None:
        """(requests used today, daily allowance) from a ``/user`` response body, or None if unreadable."""
        try:
            data = response.json()
        except ValueError:
            return None
        used = _parse_int(data.get("api_requests_count"))
        limit = _parse_int(data.get("api_requests"))
        return (used or 0, limit) if limit is not None else None


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
