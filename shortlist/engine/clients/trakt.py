"""Trakt client: 'related titles' as a candidate source, using one API key (no per-user login).

Trakt's related graph is purpose-built for "what to watch next" and often beats TMDB's per-seed
similarity. Read-only public endpoints need only the app's client id in the ``trakt-api-key`` header,
so this stays a single-key integration — no OAuth, no per-user tokens.
"""

from __future__ import annotations

import json

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.clients.tmdb import Cache, NullCache
from shortlist.engine.models import MediaType

TRAKT_API = "https://api.trakt.tv"
CACHE_TTL_S = 7 * 24 * 3600  # a seed's related graph moves slowly — cache it 7 days, like TMDB


class TraktError(RuntimeError):
    """A Trakt call failed (connection, auth, or bad response). Never carries the client id."""


class TraktClient:
    def __init__(
        self,
        client_id: str,
        *,
        cache: Cache | None = None,
        # Trakt has no unusual latency profile of its own; the shared ceiling applies.
        timeout: float = http_retry.DEFAULT_TIMEOUT_S,
    ):
        self._client_id = client_id
        self._cache = cache or NullCache()
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "trakt-api-version": "2", "trakt-api-key": self._client_id}

    def _get(self, path: str) -> object:
        try:
            r = http_retry.get(f"{TRAKT_API}{path}", headers=self._headers(), timeout=self._timeout)
        except httpx.HTTPError as e:
            raise TraktError(f"Trakt unreachable ({type(e).__name__})") from e
        if r.status_code in (401, 403):
            raise TraktError("Trakt rejected the API key")
        if r.status_code != 200:
            raise TraktError(f"Trakt GET {path} returned HTTP {r.status_code}")
        return r.json()

    def ping(self) -> str:
        """A tiny authenticated call for the settings Test button (needs a valid client id)."""
        self._get("/movies/trending?limit=1")
        return "Connected to Trakt"

    def related(self, tmdb_id: int, media_type: MediaType, *, limit: int = 20) -> list[dict]:
        """Trakt's 'related' titles for a seed, normalized to {tmdb_id, title, year, genres}.

        Crosses the TMDB→Trakt namespace first (Trakt keys on its own slug), then reads /related.
        Returns [] rather than raising for a seed Trakt doesn't know — one seed's miss is not a failure.
        """
        kind = "movie" if media_type is MediaType.MOVIE else "show"
        # Related depends ONLY on (tmdb_id, media_type), never the user — so it caches across every
        # user in a run AND across nightly runs. This was the one uncached per-seed source: 2 HTTP
        # calls per seed per user, every night. Keyed on the pair; empty results cache too (a seed
        # Trakt doesn't know stays unknown for the TTL rather than being re-looked-up every run). A
        # network failure raises out of _fetch_related BEFORE the set, so a blip is never cached.
        cache_key = f"trakt:related:{kind}:{tmdb_id}:{limit}"
        if cached := self._cache.get(cache_key):
            logger.trace("trakt cache hit · related {} {}", kind, tmdb_id)
            return json.loads(cached)
        out = self._fetch_related(tmdb_id, kind, media_type, limit)
        self._cache.set(cache_key, json.dumps(out), CACHE_TTL_S)
        logger.debug("Trakt related for tmdb {}: {} titles", tmdb_id, len(out))
        return out

    def _fetch_related(self, tmdb_id: int, kind: str, media_type: MediaType, limit: int) -> list[dict]:
        plural = "movies" if media_type is MediaType.MOVIE else "shows"
        found = self._get(f"/search/tmdb/{tmdb_id}?type={kind}")
        if not isinstance(found, list) or not found:
            return []
        entry = found[0].get(kind) or {}
        ids = entry.get("ids") or {}
        slug = ids.get("slug") or ids.get("trakt")
        if not slug:
            return []
        related = self._get(f"/{plural}/{slug}/related?extended=full&limit={limit}")
        out: list[dict] = []
        for item in related if isinstance(related, list) else []:
            tid = (item.get("ids") or {}).get("tmdb")
            if tid is None:
                continue
            out.append(
                {
                    "tmdb_id": int(tid),
                    "title": item.get("title") or "",
                    "year": item.get("year"),
                    "genres": item.get("genres") or [],
                }
            )
        return out
