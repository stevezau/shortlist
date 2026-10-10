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
from collections.abc import Collection
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.delivery import section_kind, target_sections
from shortlist.engine.models import MediaType, RowSpec
from shortlist.server.db.adapters import DbCache

if TYPE_CHECKING:
    from plexapi.library import LibrarySection

#: How long a scan made here is reused.
MEMO_TTL_S = 600.0

_memo: dict[tuple[str, str | None], tuple[float, dict[int, int]]] = {}
_memo_lock = threading.Lock()
#: One per library: ten presets previewed at once must cost one scan of it, not ten.
_scan_locks: dict[str, threading.Lock] = {}


class _LibraryReader(Protocol):
    def sections(self) -> list[LibrarySection]: ...

    def section_signature(self, section: LibrarySection) -> str | None: ...

    def build_library_index(self, section: LibrarySection) -> dict[int, int]: ...


def library_index(
    plex: _LibraryReader,
    sessions: sessionmaker[Session],
    *,
    media: str = "both",
    library_keys: Collection[str] = (),
) -> dict[MediaType, dict[int, int]]:
    """``tmdb_id -> ratingKey`` per media type, across the libraries one row builds in.

    A row draws only from its own libraries of its own media type, so a count over every library would
    overstate what it can use (#137 I-1). Which libraries those are is `delivery.target_sections`'s answer,
    the very rule a run delivers by.

    Args:
        plex: The owner's server.
        sessions: For the run's cached index.
        media: The row's ``media``: "movie", "show" or "both".
        library_keys: The row's ``library_keys``; empty is every library of its media type.

    Returns:
        The index, in the shape `seasons.load_titles` and `seasons.preview` take.
    """
    cache = DbCache(sessions, kind="library_index")
    index: dict[MediaType, dict[int, int]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for section in row_sections(plex, media=media, library_keys=library_keys):
        index[section_kind(section)].update(_section_index(plex, cache, section))
    return index


def row_sections(plex: _LibraryReader, *, media: str, library_keys: Collection[str]) -> list[LibrarySection]:
    """The libraries a row with this ``media`` and ``library_keys`` builds in, by `delivery.target_sections`."""
    row = RowSpec(slug="", name_template="", size=0, media=media, library_keys=[str(key) for key in library_keys])
    return target_sections(plex.sections(), row)


def _section_index(plex: _LibraryReader, cache: DbCache, section: LibrarySection) -> dict[int, int]:
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
