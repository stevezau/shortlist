"""Helpers shared by the split test modules; moved verbatim from the original file."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import shortlist.engine.picker as picker_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.models import (
    EngineConfig,
    MediaType,
)
from tests.conftest import MemorySnapshotStore, fake_media_item, make_watched


def _ranked(items: list[dict], affinity: float = 1.0) -> list[tuple[dict, float]]:
    """`TmdbClient.suggestions` returns (item, affinity) pairs. These tests predate affinity and
    don't exercise it, so everything sits at the neutral top-of-list 1.0."""
    return [(item, affinity) for item in items]


def spy_build_picks(monkeypatch) -> list[list]:
    """Record the candidate pools handed to ``picker.build_picks`` — the code-based pick-selection
    step that replaced the old LLM ``curate`` call. Returns one entry per call: the candidate list
    that row+library was offered. ``build_picks`` still runs for real, so the picks are unchanged.
    """
    calls: list[list] = []
    real = picker_mod.build_picks

    def spy(candidates, k):
        calls.append(list(candidates))
        return real(candidates, k)

    monkeypatch.setattr(picker_mod, "build_picks", spy)
    return calls


@pytest.fixture
def ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    plex = MagicMock()
    movie_section = MagicMock()
    movie_section.type = "movie"
    movie_section.title = "Movies"  # fills {library_name} in the default row title
    plex.sections.return_value = [movie_section]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section}
    movie_section.collections.return_value = []
    # Library: watched item 900 (ratingKey 999) + candidates 10 and 20.
    plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020}
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []  # delivery finds by title; promotion enumerates rows
    # `extra` is the constant `shortlist` label, which now rides along in the SAME write on a
    # newly created row (one PUT instead of two).
    plex.stored_label.side_effect = lambda collection, label, *, extra=None: label.replace("shortlist", "Shortlist", 1)
    # (items, missing) — the real client reports what Plex no longer has, because a partial batch
    # omits dead keys silently and delivery must not claim it delivered them.
    plex.fetch_items.side_effect = lambda keys: ([fake_media_item(k, f"item{k}") for k in keys], [])

    history = MagicMock()
    history.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]

    # (item, affinity) pairs — see TmdbClient.suggestions. These predate affinity and don't
    # exercise it, so both sit at the neutral top-of-list 1.0.
    mock_tmdb.suggestions.return_value = [
        ({"id": 10, "title": "Candidate Ten", "genre_ids": [], "vote_average": 8.0}, 1.0),
        ({"id": 20, "title": "Candidate Twenty", "genre_ids": [], "vote_average": 7.0}, 1.0),
    ]
    mock_tmdb.genre_names.return_value = {}

    def put(account_id, fields):
        for u in mock_plextv.users:
            if u.id == account_id:
                u.filters.update(fields)

    mock_plextv.update_user_filters.side_effect = put

    return EngineContext(
        config=engine_config,
        plex=plex,
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=history,
        curator=mock_curator,
        snapshots=MemorySnapshotStore(),
    )
