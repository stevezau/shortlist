"""Which titles the libraries hold, for the season editor's counts (issue #137).

A run keeps each library's ``tmdb_id -> ratingKey`` index in the cache under the library's change signature,
and the editor reads it from there when it is current. It rarely is: any title added since the last run
changes the signature (measured on a real server, every section missed). A scan of a 10,000-film library
takes 7.5s, so a scan made here is held in this process for ten minutes, per library and signature, for the
next preview to reuse.

A scan made here is never written to the run's cache: that entry also carries the genre tally a run may need
(`pipeline._library_index`), which this scan does not take.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Protocol

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.models import MediaType
from shortlist.server.db.adapters import DbCache

#: How long a scan made here is reused.
MEMO_TTL_S = 600.0

_memo: dict[tuple[str, str | None], tuple[float, dict[int, int]]] = {}
_memo_lock = threading.Lock()
#: One per library: ten presets previewed at once must cost one scan of it, not ten.
_scan_locks: dict[str, threading.Lock] = {}


class _LibraryReader(Protocol):
    def sections(self) -> list: ...

    def section_signature(self, section: object) -> str | None: ...

    def build_library_index(self, section: object) -> dict[int, int]: ...


def library_index(plex: _LibraryReader, sessions: sessionmaker[Session]) -> dict[MediaType, dict[int, int]]:
    """``tmdb_id -> ratingKey`` per media type, across every movie and TV library.

    Args:
        plex: The owner's server.
        sessions: For the run's cached index.

    Returns:
        The index, in the shape `seasons.load_titles` and `seasons.preview` take.
    """
    cache = DbCache(sessions, kind="library_index")
    index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for section in plex.sections():
        kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
        index[kind].update(_section_index(plex, cache, section))
    return index


def forget() -> None:
    """Drop every scan held here."""
    with _memo_lock:
        _memo.clear()


def _section_index(plex: _LibraryReader, cache: DbCache, section) -> dict[int, int]:
    signature = plex.section_signature(section)
    # The run's key (`pipeline._library_index`). With no signature a run never caches, so there is nothing to read.
    if signature and (cached := cache.get(f"index3:{section.key}:{signature}")):
        return {int(tmdb_id): rating_key for tmdb_id, rating_key in json.loads(cached)["index"].items()}

    key = (str(section.key), signature)
    if (held := _held(key)) is not None:
        return held
    with _memo_lock:
        scan_lock = _scan_locks.setdefault(key[0], threading.Lock())
    with scan_lock:
        # Whoever this waited behind has just scanned it.
        if (held := _held(key)) is not None:
            return held
        index = plex.build_library_index(section)
        logger.debug("season editor: scanned library '{}' ({} titles with a TMDB id)", section.title, len(index))
        now = time.monotonic()
        with _memo_lock:
            for stale in [k for k, (expires, _index) in _memo.items() if expires <= now]:
                del _memo[stale]
            _memo[key] = (now + MEMO_TTL_S, index)
        return index


def _held(key: tuple[str, str | None]) -> dict[int, int] | None:
    with _memo_lock:
        entry = _memo.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    return None
