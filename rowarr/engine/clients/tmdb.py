"""TMDB client: similar + recommendations pooling with a pluggable cache."""

from __future__ import annotations

import json
from typing import Protocol

import httpx
from loguru import logger

from rowarr.engine.models import MediaType

API = "https://api.themoviedb.org/3"
CACHE_TTL_S = 7 * 24 * 3600  # design: (tmdb_id, endpoint) cached 7 days


class Cache(Protocol):
    """Minimal cache the adapters provide (JSON-file for the CLI, DB table for the server)."""

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

    def _get(self, path: str, **params) -> dict:
        cache_key = f"tmdb:{path}"
        if cached := self._cache.get(cache_key):
            return json.loads(cached)
        r = httpx.get(
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
        data = r.json()
        self._cache.set(cache_key, json.dumps(data), CACHE_TTL_S)
        return data

    def ping(self) -> bool:
        return bool(self._get("/configuration"))

    def suggestions(self, tmdb_id: int, media_type: MediaType) -> list[dict]:
        """Pooled /recommendations + /similar results for one seed title."""
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        pooled: dict[int, dict] = {}
        for endpoint in ("recommendations", "similar"):
            data = self._get(f"/{kind}/{tmdb_id}/{endpoint}")
            for item in data.get("results", []):
                pooled.setdefault(item["id"], item)
        logger.debug("TMDB suggestions for {} {}: {} pooled", kind, tmdb_id, len(pooled))
        return list(pooled.values())

    def genre_names(self, media_type: MediaType) -> dict[int, str]:
        kind = "movie" if media_type is MediaType.MOVIE else "tv"
        data = self._get(f"/genre/{kind}/list")
        return {g["id"]: g["name"] for g in data.get("genres", [])}
