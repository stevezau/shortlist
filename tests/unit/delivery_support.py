"""Helpers shared by the split test modules; moved verbatim from the original file."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.models import MediaType, Pick


def picks(n: int = 2, media_type: MediaType = MediaType.MOVIE, start: int = 1) -> list[Pick]:
    kind = "Movie" if media_type is MediaType.MOVIE else "Show"
    return [
        Pick(
            tmdb_id=i,
            rating_key=1000 + i,
            title=f"{kind} {i}",
            rank=i,
            reason="Because you watched Fargo",
            media_type=media_type,
            seed_title="Fargo",
            seed_tmdb_id=900,
        )
        for i in range(start, start + n)
    ]


def _section(title: str, kind: str, key: int) -> MagicMock:
    section = MagicMock()
    section.title = title
    section.type = kind
    section.key = key  # sections are matched by key, never by object identity
    return section


@pytest.fixture
def movies() -> MagicMock:
    return _section("Movies", "movie", 1)


@pytest.fixture
def shows() -> MagicMock:
    return _section("TV Shows", "show", 2)


def _labelling_plex_mock(plex: MagicMock) -> MagicMock:
    """Make `stored_label` leave the label ON the collection, as the real one does, and give
    `fetch_items` the `(items, missing)` shape the real client returns.

    `fetch_items` reports what Plex still HAS and what has GONE, because a partial batch omits dead
    keys silently — a mock returning a bare list would let delivery claim it delivered titles the row
    does not contain. Default: nothing missing.

    Not decoration: `_apply_shortlist_label` refuses to write unless the owner label is already in
    `collection.labels`, because plexapi's addLabel PUTs an ABSOLUTE tag set built from that list —
    so writing against an empty one would DELETE the owner label and un-hide the row. A mock that
    returned a string without touching the object would report that guard as broken, and (worse) a
    mock that ignored the guard entirely would let a regression through. Testing rule: the fake must
    be no easier than the real server.
    """

    # `fetch_items` returns (items, missing): a partial batch omits dead keys silently, so delivery
    # has to be told what went. A bare MagicMock ITERATES EMPTY rather than raising, so unpacking it
    # would quietly yield nothing — the mock must carry the real shape.
    plex.fetch_items.return_value = ([], [])

    def stored_label(collection, label, *, extra=None):
        # `extra` lands in the SAME write, exactly as the real client does on a create — so the
        # constant label is already present when `_apply_shortlist_label` runs and that call
        # short-circuits without a write. A fake that ignored `extra` would leave the label absent
        # and hide the fact that the second write is now redundant.
        current = list(getattr(collection, "_labels", []))

        def _put(name: str) -> str:
            stored_name = name.replace("shortlist", "Shortlist", 1)
            if not any(t.tag.lower() == name.lower() for t in current):
                current.append(SimpleNamespace(tag=stored_name))
            return stored_name

        stored = _put(label)  # the CRITICAL label's casing is what the caller reports
        if extra is not None:
            _put(extra)
        collection._labels = current
        collection.labels = current
        return stored

    plex.stored_label.side_effect = stored_label
    original_create = plex.create_collection.side_effect

    def create(section, title, items):
        # Default to the mock's own return_value, not a fresh MagicMock — tests assert identity
        # against `plex.create_collection.return_value`.
        collection = original_create(section, title, items) if original_create else plex.create_collection.return_value
        collection._labels = []
        collection.labels = []
        return collection

    plex.create_collection.side_effect = create
    return plex
