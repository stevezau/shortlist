"""Shared fixtures. All external I/O (Plex, plex.tv, Tautulli, TMDB, LLMs) is mocked — no test touches the network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvUser
from shortlist.engine.models import (
    Candidate,
    EngineConfig,
    FilterSnapshot,
    MediaType,
    Seed,
    UserProfile,
    UserType,
    WatchedItem,
)

NOW = datetime(2026, 7, 12, tzinfo=UTC)


def make_watched(title: str, days_ago: int = 1, media_type: MediaType = MediaType.MOVIE, **kw) -> WatchedItem:
    return WatchedItem(title=title, media_type=media_type, watched_at=NOW - timedelta(days=days_ago), **kw)


def make_candidate(
    tmdb_id: int,
    title: str,
    *,
    rating: float = 7.0,
    seeds: list[Seed] | None = None,
    media_type: MediaType = MediaType.MOVIE,
    **kw,
) -> Candidate:
    # `is None`, not `or`: seeds=[] must produce a genuinely SEEDLESS candidate. `or` quietly handed
    # one back, so no test could express what tmdb_discover/llm_library/llm_web actually produce —
    # which is why nothing caught those three sources being ranked at zero and dropped.
    default_seed = [Seed(tmdb_id=1, title="Seed Movie", media_type=media_type, weight=1.0)]
    return Candidate(
        tmdb_id=tmdb_id,
        title=title,
        media_type=media_type,
        rating=rating,
        seeds=default_seed if seeds is None else list(seeds),
        **kw,
    )


def make_profile(
    username: str = "sarah", user_type: UserType = UserType.SHARED, account_id: int = 100, **kw
) -> UserProfile:
    return UserProfile(username=username, plex_account_id=account_id, user_type=user_type, **kw)


class MemorySnapshotStore:
    def __init__(self):
        self.saved: dict[int, FilterSnapshot] = {}

    def get(self, plex_account_id: int) -> FilterSnapshot | None:
        return self.saved.get(plex_account_id)

    def save(self, snapshot: FilterSnapshot) -> None:
        self.saved[snapshot.plex_account_id] = snapshot


@pytest.fixture(autouse=True)
def _no_retry_backoff_waits(monkeypatch):
    """Keep every retry ATTEMPT, drop the wall-clock WAIT between them.

    The clients retry unreachable hosts on a real clock: ``plex_pms``'s urllib3 adapter waits
    0s+3s+6s per exhausted request, and ``http_retry`` waits ~1s+2s. Tests point at hostnames that
    do not resolve (``http://pms:32400`` and friends), so any test reaching a client pays the full
    backoff for nothing — measured at 22% of total suite wall-clock, and it lands hardest under
    xdist, where a sleeping worker is a worker not running tests.

    Only the sleeps go. Attempt counts, ordering and every assertion about retry behaviour are
    unchanged, and ``time.sleep`` is still called (with 0), so the tests that COUNT backoffs still
    see them. A test that needs real waits overrides this with its own ``monkeypatch``, which is
    applied after this fixture and therefore wins.

    ``_backoff`` is patched rather than ``BASE_BACKOFF_S``: ``_send`` binds that constant as a
    default argument at import time, so rebinding the module attribute would silently do nothing.
    A server-sent ``Retry-After`` is deliberately left alone — it is asserted by value in
    ``test_http_retry.py``, which stubs its own sleep anyway.
    """
    from urllib3.util.retry import Retry

    from shortlist.engine.clients import http_retry

    monkeypatch.setattr(Retry, "sleep", lambda self, response=None: None, raising=False)
    monkeypatch.setattr(Retry, "sleep_for_retry", lambda self, response=None: False, raising=False)
    monkeypatch.setattr(http_retry, "_backoff", lambda attempt, base, cap: 0.0)


@pytest.fixture
def engine_config() -> EngineConfig:
    return EngineConfig(row_size=5, min_history=3, candidates_pre_rank=10, max_seeds=10)


@pytest.fixture
def snapshot_store() -> MemorySnapshotStore:
    return MemorySnapshotStore()


@pytest.fixture
def mock_plextv():
    """plex.tv client mock; tests configure .users (list[PlexTvUser]) and inspect .update_user_filters calls."""
    client = MagicMock()
    client.users = []
    client.list_users.side_effect = lambda: client.users
    client.get_user.side_effect = lambda account_id: next(u for u in client.users if u.id == account_id)
    # Default: the Home roster was read AND covered this account. Tests that model an outage or a
    # partial roster override it — a bare MagicMock would answer truthy and hide both cases.
    client.home_profile_known.return_value = True
    return client


@pytest.fixture
def mock_plex():
    """PlexClient built without a real PlexServer connection; ._server is a MagicMock."""
    client = PlexClient.__new__(PlexClient)
    client._server = MagicMock()
    # __new__ skips __init__, so replicate its per-run read caches (sections + collections + top_rated).
    client._sections_cache = None
    client._collections_cache = {}
    client._top_rated_cache = {}
    return client


@pytest.fixture
def mock_tmdb():
    client = MagicMock()
    client.suggestions.return_value = []
    client.genre_names.return_value = {18: "Drama", 35: "Comedy"}
    return client


@pytest.fixture
def mock_curator():
    curator = MagicMock()
    curator.name = "mock"
    curator.last_tokens = 0
    return curator


def plextv_user(account_id: int, username: str, *, filters: dict | None = None, home: bool = False) -> PlexTvUser:
    base = {"filterAll": "", "filterMovies": "", "filterTelevision": "", "filterMusic": "", "filterPhotos": ""}
    return PlexTvUser(
        id=account_id,
        username=username,
        user_type=UserType.SHARED,
        home=home,
        restricted=False,
        protected=False,
        filters={**base, **(filters or {})},
    )


def fake_media_item(rating_key: int, title: str, tmdb_id: int | None = None, year: int | None = None):
    guids = [SimpleNamespace(id=f"tmdb://{tmdb_id}")] if tmdb_id else []
    return SimpleNamespace(ratingKey=rating_key, title=title, guids=guids, year=year)
