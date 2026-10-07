"""Bounded reads of configured Plex and Arr choices for owner and assistant surfaces."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from loguru import logger

from shortlist.engine.clients.plex_pms import can_anchor, has_shortlist_marker
from shortlist.engine.models import ArrTarget
from shortlist.server.settings_store import SettingsStore

_PLEX_READ_TTL_S = 120.0
_INTERACTIVE_TIMEOUT_S = 8


class ArrNotConfigured(ValueError):
    """The requested Arr service has no saved URL or API key."""


@dataclass(frozen=True)
class ArrConnection:
    """A saved Arr endpoint used only inside server-side configured-service reads."""

    url: str
    api_key: str


def invalidate_plex_reads(state: Any) -> None:
    """Forget cached interactive Plex reads after a connected server may have changed."""
    state.__dict__.pop("_plex_read_cache", None)
    state.__dict__.pop("_plex_read_locks", None)


def cached_plex_read[T](state: Any, key: str, read: Callable[[], T]) -> T:
    """Read a harmless interactive Plex view once per short TTL and collapse concurrent misses."""
    cache = state.__dict__.setdefault("_plex_read_cache", {})
    locks = state.__dict__.setdefault("_plex_read_locks", {})
    lock = locks.setdefault(key, threading.Lock())

    entry = cache.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]

    with lock:
        entry = cache.get(key)
        if entry and entry[0] > time.monotonic():
            return entry[1]
        try:
            value = read()
        except HTTPException:
            if key not in cache:
                locks.pop(key, None)
            raise
        except Exception as error:
            if entry is None:
                if key not in cache:
                    locks.pop(key, None)
                raise
            logger.warning("plex read {} failed ({}) — serving the cached copy", key, type(error).__name__)
            return entry[1]
        cache[key] = (time.monotonic() + _PLEX_READ_TTL_S, value)
        return value


def read_libraries(state: Any) -> list[dict[str, str]]:
    """Read configured movie and show libraries through the interactive cache."""

    def read() -> list[dict[str, str]]:
        from shortlist.engine.clients.plex_pms import PlexClient

        with state.sessions() as session:
            store = SettingsStore(session, state.secrets)
            url, token = store.get("plex.url"), store.get("plex.token")
        if not url or not token:
            raise HTTPException(status_code=409, detail="Plex isn't connected yet")
        client = PlexClient(url, token, timeout=_INTERACTIVE_TIMEOUT_S)
        return [
            {"key": str(section.key), "title": section.title, "type": section.type} for section in client.sections()
        ]

    return cached_plex_read(state, "libraries", read)


def read_library_anchor_choices(state: Any, key: str) -> list[dict[str, str | bool]]:
    """Read only foreign Plex managed-hub anchors for one configured library.

    Marker filtering is title-based deliberately. Reading labels causes Plex to make another request,
    and a transient empty label response can expose a Shortlist person's row as an anchor candidate.
    """

    def read() -> list[dict[str, str | bool]]:
        from shortlist.engine.clients.plex_pms import PlexClient

        with state.sessions() as session:
            store = SettingsStore(session, state.secrets)
            url, token = store.get("plex.url"), store.get("plex.token")
        if not url or not token:
            raise HTTPException(status_code=409, detail="Plex isn't connected yet")
        client = PlexClient(url, token, timeout=_INTERACTIVE_TIMEOUT_S)
        section = next((candidate for candidate in client.sections() if str(candidate.key) == key), None)
        if section is None:
            raise HTTPException(status_code=404, detail="library not found")

        seen: dict[str, bool] = {}
        for hub in section.managedHubs():
            title = getattr(hub, "title", "") or ""
            if not title or has_shortlist_marker(title):
                continue
            seen[title] = seen.get(title, False) or can_anchor(hub)
        return [{"title": title, "on_shelf": on_shelf} for title, on_shelf in seen.items()]

    return cached_plex_read(state, f"collections:{key}", read)


def configured_arr_connection(state: Any, service: str) -> ArrConnection:
    """Return one normalized saved Arr destination without constructing a remote client."""
    if service not in ("radarr", "sonarr"):
        raise ValueError(f"unknown Arr service {service!r}")
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        url = (store.get(f"requests.{service}.url") or "").strip().rstrip("/")
        api_key = store.get(f"requests.{service}.apikey") or ""
    if not url or not api_key:
        raise ArrNotConfigured(f"{service.title()} isn't connected yet")
    return ArrConnection(url=url, api_key=api_key)


def read_arr_choices(service: str, connection: ArrConnection) -> dict[str, list[dict[str, int | str]]]:
    """Read a configured Arr service's profiles and root folders without accepting caller input."""
    from shortlist.engine.clients.arr import make_arr_client

    target = ArrTarget(url=connection.url, api_key=connection.api_key, quality_profile_id=0, root_folder="")
    client = make_arr_client(service, target)
    return {"quality_profiles": client.quality_profiles(), "root_folders": client.root_folders()}
