"""OMDb client: look up a title's IMDb rating and vote count by IMDb id.

Optional — only used when the owner chooses IMDb (rather than TMDB) as the rating source for
Sonarr/Radarr requests. TMDB gives us the IMDb id; OMDb turns it into an IMDb score. Lookups are
deliberately bounded by the caller (only a shortlist of candidates per run) because OMDb's free
tier is rate-limited.
"""

from __future__ import annotations

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry

API = "https://www.omdbapi.com/"


class OmdbClient:
    def __init__(self, api_key: str, *, timeout: float = 15.0):
        self._api_key = api_key
        self._timeout = timeout

    def rating(self, imdb_id: str) -> tuple[float, int] | None:
        """(imdb_rating 0..10, imdb_votes) for an IMDb id, or None if unavailable.

        Returns None rather than raising on any problem — a missing rating just means the title
        can't be gated on IMDb and is skipped, never a failed run. The api key is never put in an
        exception or log (plex-safety rule 9).
        """
        try:
            r = http_retry.get(API, params={"apikey": self._api_key, "i": imdb_id}, timeout=self._timeout)
        except httpx.HTTPError as e:
            logger.warning("OMDb unreachable for {}: {}", imdb_id, type(e).__name__)
            return None
        if r.status_code != 200:
            logger.warning("OMDb returned HTTP {} for {}", r.status_code, imdb_id)
            return None
        try:
            data = r.json()
        except ValueError:  # a 200 with a non-JSON body (proxy/error page) — honor "never raises"
            return None
        if data.get("Response") != "True":
            return None
        rating = _parse_float(data.get("imdbRating"))
        votes = _parse_int(data.get("imdbVotes"))
        if rating is None or votes is None:
            return None
        return rating, votes

    def ping(self) -> str:
        """A tiny lookup for the settings 'Test' button; raises on a bad key."""
        r = http_retry.get(API, params={"apikey": self._api_key, "i": "tt0111161"}, timeout=self._timeout)
        data = r.json() if r.status_code == 200 else {}
        if data.get("Response") == "True":
            return "OMDb key works"
        raise RuntimeError(data.get("Error") or f"OMDb rejected the request (HTTP {r.status_code})")


def _parse_float(value: object) -> float | None:
    """OMDb gives ratings as strings like "8.1", or "N/A" when it has none."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _parse_int(value: object) -> int | None:
    """OMDb gives vote counts as thousands-separated strings like "2,754,113"."""
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
