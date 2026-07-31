"""The watched-title cache: read incrementally, but never trust it to be complete on its own.

The watched set drives every recommendation and the dashboard's hit rate, and it was read COMPLETE —
per user, per library, 500 titles a page — on the nightly sync and again inside every run. Caching it
makes the common night cheap. The danger is the opposite of a slow sync: a cache that quietly holds
the WRONG set, which no user-visible symptom would announce.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from shortlist.engine.models import MediaType, WatchedItem
from shortlist.server.db.models import User, WatchedTitle, WatchSyncState
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.watch_cache import CURSOR_OVERLAP, WatchCache

SECTION = "1"


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    return make_session_factory(make_engine(tmp_path))


@pytest.fixture
def user_id(sessions):
    with sessions() as session:
        user = User(username="sarah", slug="sarah", plex_account_id=1, user_type="shared", enabled=True)
        session.add(user)
        session.commit()
        return user.id


def profile():
    return SimpleNamespace(username="sarah", slug="sarah")


_SAME_AS_TMDB = object()


def watched(title: str, *, tmdb_id: int, days_ago: float = 1, rating_key=_SAME_AS_TMDB) -> WatchedItem:
    """A watched title. `rating_key=None` really means none — the default mirrors tmdb_id."""
    return WatchedItem(
        title=title,
        media_type=MediaType.MOVIE,
        watched_at=datetime.now(UTC) - timedelta(days=days_ago),
        tmdb_id=tmdb_id,
        rating_key=tmdb_id if rating_key is _SAME_AS_TMDB else rating_key,
    )


def sync(cache, sessions, user_id, items, *, force_full=False, capture=None):
    """Run one section sync, recording the `since` the reader was asked for."""

    def read(since):
        if capture is not None:
            capture.append(since)
        return items

    with sessions() as session:
        outcome = cache.sync_section(session, profile(), user_id, SECTION, MediaType.MOVIE, read, force_full=force_full)
        session.commit()
    return outcome


class TestFullVsIncremental:
    def test_the_first_read_is_always_full(self, sessions, user_id):
        cache = WatchCache(sessions)
        asked: list[datetime | None] = []

        outcome = sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)], capture=asked)

        assert outcome.full is True
        assert asked == [None], "there is nothing to be incremental against on a first read"

    def test_the_next_read_asks_only_for_what_changed(self, sessions, user_id):
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=2)])

        asked: list[datetime | None] = []
        sync(cache, sessions, user_id, [], capture=asked)

        assert asked[0] is not None, "the second read re-read everything"

    def test_the_cursor_sits_behind_the_newest_watch(self, sessions, user_id):
        """A page walk takes time and Plex stamps `lastViewedAt` from its own clock. Resuming from
        exactly the newest timestamp skips anything written during the walk."""
        cache = WatchCache(sessions)
        newest = watched("Heat", tmdb_id=1, days_ago=1)

        sync(cache, sessions, user_id, [newest])

        with sessions() as session:
            state = session.query(WatchSyncState).one()
            cursor = state.cursor_viewed_at.replace(tzinfo=UTC)
        assert cursor == newest.watched_at - CURSOR_OVERLAP

    def test_a_full_read_falls_due_again_on_schedule(self, sessions, user_id):
        """An incremental read cannot see an un-watch or a deletion, so a complete one has to happen
        regardless of how well the cursor is working."""
        cache = WatchCache(sessions, full_every=timedelta(days=7))
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)])

        with sessions() as session:
            assert cache.needs_full(session, user_id, SECTION) is False
            state = session.query(WatchSyncState).one()
            state.last_full_at = datetime.now(UTC) - timedelta(days=8)
            session.commit()
        with sessions() as session:
            assert cache.needs_full(session, user_id, SECTION) is True


class TestCorrectness:
    def test_a_full_read_drops_a_title_that_was_un_watched(self, sessions, user_id):
        """The whole reason the weekly full read exists — nothing else can notice this."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2)])

        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)], force_full=True)

        with sessions() as session:
            assert [row.title for row in session.query(WatchedTitle).all()] == ["Heat"]

    def test_an_incremental_read_keeps_what_it_did_not_ask_about(self, sessions, user_id):
        """The opposite failure: an incremental top-up must not be mistaken for the whole truth."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=30)])

        sync(cache, sessions, user_id, [watched("Dune", tmdb_id=2, days_ago=1)])

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Heat", "Dune"}

    def test_re_seeing_a_title_updates_it_rather_than_duplicating_it(self, sessions, user_id):
        """The cursor overlaps deliberately, so every incremental read re-sees a few recent titles."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=2)])

        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=1)])

        with sessions() as session:
            rows = session.query(WatchedTitle).all()
            assert len(rows) == 1
            assert rows[0].viewed_at.replace(tzinfo=UTC) > datetime.now(UTC) - timedelta(days=1, minutes=1)

    def test_seeding_then_topping_up_matches_one_complete_read(self, sessions, user_id):
        """The property that makes the whole optimisation legitimate: however the set was assembled,
        it must equal what a single complete read would have given."""
        everything = [watched(f"T{i}", tmdb_id=i, days_ago=30 - i) for i in range(10)]

        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, everything[:6])
        sync(cache, sessions, user_id, everything[6:])
        with sessions() as session:
            incremental = {item.tmdb_id for item in cache.watched_set(session, user_id)}

        with sessions() as session:
            session.query(WatchedTitle).delete()
            session.query(WatchSyncState).delete()
            session.commit()
        sync(cache, sessions, user_id, everything, force_full=True)
        with sessions() as session:
            complete = {item.tmdb_id for item in cache.watched_set(session, user_id)}

        assert incremental == complete

    def test_a_failed_read_leaves_the_cursor_where_it_was(self, sessions, user_id):
        """Advancing past a read that raised would silently skip whatever it missed, for ever."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=5)])
        with sessions() as session:
            before = session.query(WatchSyncState).one().cursor_viewed_at

        def boom(_since):
            raise RuntimeError("PMS is down")

        with sessions() as session, pytest.raises(RuntimeError):
            cache.sync_section(session, profile(), user_id, SECTION, MediaType.MOVIE, boom)

        with sessions() as session:
            assert session.query(WatchSyncState).one().cursor_viewed_at == before

    def test_a_title_with_no_rating_key_is_still_cached(self, sessions, user_id):
        """Dropping it would mean the cache silently holds less than the direct read did."""
        cache = WatchCache(sessions)

        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=77, rating_key=None)])

        with sessions() as session:
            items = cache.watched_set(session, user_id)
            assert [item.tmdb_id for item in items] == [77]
            # The fallback key is ours, not Plex's — it must not be handed back as a rating key.
            assert items[0].rating_key is None

    def test_an_empty_library_does_not_get_read_in_full_for_ever(self, sessions, user_id):
        cache = WatchCache(sessions)
        assert sync(cache, sessions, user_id, []).full is True

        assert sync(cache, sessions, user_id, []).full is False

    def test_a_section_that_cannot_be_topped_up_is_forced_back_to_a_full_read(self, sessions, user_id):
        """The escape hatch. If the PMS refuses the incremental `lastViewedAt>=` filter, every
        incremental read raises, the cursor never advances, and the cache goes stale for ever —
        silently re-recommending titles people have already seen. A full read sends no filter, so
        forcing one is the way out."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)])
        with sessions() as session:
            assert cache.needs_full(session, user_id, SECTION) is False

            cache.force_full_next_time(session, user_id, SECTION)
            session.commit()

        with sessions() as session:
            assert cache.needs_full(session, user_id, SECTION) is True
        asked: list[datetime | None] = []
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)], capture=asked)
        assert asked == [None], "the recovery read must send no filter at all"
