"""Shared fixtures. All external I/O (Plex, plex.tv, Tautulli, TMDB, LLMs) is mocked — no test touches the network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from rowarr.engine.clients.plex_pms import PlexClient
from rowarr.engine.clients.plextv import PlexTvUser
from rowarr.engine.models import (
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
    return Candidate(
        tmdb_id=tmdb_id,
        title=title,
        media_type=media_type,
        rating=rating,
        seeds=seeds or [Seed(tmdb_id=1, title="Seed Movie", media_type=media_type, weight=1.0)],
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
    return client


@pytest.fixture
def mock_plex():
    """PlexClient built without a real PlexServer connection; ._server is a MagicMock."""
    client = PlexClient.__new__(PlexClient)
    client._server = MagicMock()
    return client


@pytest.fixture
def mock_tautulli():
    client = MagicMock()
    client.get_history.return_value = []
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
