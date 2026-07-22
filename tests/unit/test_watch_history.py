"""StoreHistorySource: incremental sync into watch_events, complete read back."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.server.db.models import User, WatchEvent
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.watch_history import StoreHistorySource


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def sarah(sessions):
    with sessions() as session:
        session.add(User(plex_account_id=100, username="sarah", slug="sarah", enabled=True))
        session.commit()
    return UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")


class _FakeUpstream:
    """A history source that returns canned items and records the `since` it was called with."""

    def __init__(self, items: list[WatchedItem]):
        self._items = items
        self.since_calls: list[datetime | None] = []

    def fetch(self, user, *, min_completion, since=None):
        self.since_calls.append(since)
        # Simulate an incremental source: on a `since`, only return items newer than it.
        if since is None:
            return list(self._items)
        return [i for i in self._items if i.watched_at > since]


def _item(rating_key, title, days_ago, media=MediaType.MOVIE, completion=1.0):
    return WatchedItem(
        title=title,
        media_type=media,
        watched_at=datetime.now(UTC) - timedelta(days=days_ago),
        rating_key=rating_key,
        completion=completion,
    )


def _count(sessions, slug="sarah") -> int:
    with sessions() as s:
        uid = s.query(User).filter_by(slug=slug).one().id
        return s.query(WatchEvent).filter_by(user_id=uid).count()


def test_first_fetch_backfills_full_history_and_returns_it(sessions, sarah):
    up = _FakeUpstream([_item(1, "Dune", 400), _item(2, "Arrival", 300)])
    src = StoreHistorySource(sessions, up, min_completion=0.7)

    out = src.fetch(sarah, min_completion=0.7)

    assert up.since_calls == [None]  # no watermark yet -> full backfill
    assert {i.title for i in out} == {"Dune", "Arrival"}
    assert _count(sessions) == 2  # both persisted


def test_second_fetch_is_incremental_and_dedups(sessions, sarah):
    # Arrival is watched "now" — inside the 6h overlap window — so the second (incremental) fetch
    # RE-returns it, genuinely driving the on_conflict_do_nothing path. Dune is old (outside overlap).
    up = _FakeUpstream([_item(1, "Dune", 400), _item(2, "Arrival", 0)])
    src = StoreHistorySource(sessions, up, min_completion=0.7)
    src.fetch(sarah, min_completion=0.7)  # backfill
    assert _count(sessions) == 2

    up._items.append(_item(3, "Sicario", 0))  # a genuinely new watch
    out = src.fetch(sarah, min_completion=0.7)

    assert up.since_calls[1] is not None  # second call used the watermark (incremental)
    # Arrival was re-returned by the overlap and hit the unique constraint -> no duplicate row.
    assert {i.title for i in out} == {"Dune", "Arrival", "Sicario"}
    assert _count(sessions) == 3


def test_upstream_failure_keeps_whatever_is_stored(sessions, sarah):
    up = _FakeUpstream([_item(1, "Dune", 400)])
    src = StoreHistorySource(sessions, up, min_completion=0.7)
    src.fetch(sarah, min_completion=0.7)  # stores Dune

    def boom(*a, **k):
        raise RuntimeError("plex.tv down")

    up.fetch = boom
    out = src.fetch(sarah, min_completion=0.7)

    assert {i.title for i in out} == {"Dune"}  # the run still gets the stored history, not an empty set


def test_read_filters_by_completion_and_skips_ratingkeyless(sessions, sarah):
    up = _FakeUpstream(
        [
            _item(1, "Finished", 10, completion=1.0),
            _item(2, "HalfWatched", 5, completion=0.4),
            WatchedItem(title="NoKey", media_type=MediaType.MOVIE, watched_at=datetime.now(UTC), rating_key=None),
        ]
    )
    src = StoreHistorySource(sessions, up, min_completion=0.0)  # sync stores all (incl. partial)

    out = src.fetch(sarah, min_completion=0.7)  # read only counts finished ones

    assert {i.title for i in out} == {"Finished"}  # partial filtered out at read, keyless never stored
    assert _count(sessions) == 2  # Finished + HalfWatched stored; NoKey skipped (no ratingKey)


class TestPlexDbFlags:
    """The second source: watched FLAGS from the PMS database.

    Plex's history API returns playback sessions only — it never returns a mark-as-watched. On SFLIX
    that hid ~13,400 of one account's ~14,500 watched titles, which is how six films the user had
    marked ended up recommended back to them.
    """

    MOVIE_TYPE = 1

    def _reader(self, tmp_path: Path, rows):
        from shortlist.engine.clients.plex_db import PlexDbReader
        from tests.unit.test_plex_db import make_plex_db

        return PlexDbReader(make_plex_db(tmp_path / "plexdb", rows))

    @pytest.fixture
    def plexdb_dir(self, tmp_path: Path):
        (tmp_path / "plexdb").mkdir()
        return tmp_path

    def test_a_marked_title_reaches_the_store_and_the_filter(self, sessions, sarah, plexdb_dir):
        """The end-to-end point: the play history knows nothing, the reconcile closes the gap. The
        reconcile is the deliberate one-off — a plain fetch never touches the PMS database."""
        reader = self._reader(plexdb_dir, [(100, 9587, "Gravity", 2013, self.MOVIE_TYPE, 1, 1728609330)])
        source = StoreHistorySource(sessions, _FakeUpstream([]), min_completion=0.0, flags=reader)

        assert source.fetch(sarah, min_completion=0.0) == [], "a run must not read the PMS database"

        added = source.reconcile_flags(sarah)

        assert added == 1
        assert [w.title for w in source.fetch(sarah, min_completion=0.0)] == ["Gravity"]

    def test_flags_never_add_a_second_row_for_a_title_already_played(self, sessions, sarah, plexdb_dir):
        """`watch_events` holds one row PER PLAY and the finished-show fraction counts them, so a
        flag must fill gaps only — never inflate a real play count."""
        reader = self._reader(plexdb_dir, [(100, 55, "Played", 2020, self.MOVIE_TYPE, 1, 1728609330)])
        source = StoreHistorySource(
            sessions, _FakeUpstream([_item(55, "Played", days_ago=1)]), min_completion=0.0, flags=reader
        )

        source.fetch(sarah, min_completion=0.0)  # stores the real play
        source.reconcile_flags(sarah)  # the flag for the same title must not add a second row

        with sessions() as s:
            uid = s.query(User).filter_by(slug="sarah").one().id
            assert s.query(WatchEvent).filter_by(user_id=uid, rating_key=55).count() == 1

    def test_it_is_off_unless_configured(self, sessions, sarah):
        """No reader = the previous behaviour exactly. Reading someone's PMS database is opt-in."""
        source = StoreHistorySource(sessions, _FakeUpstream([]), min_completion=0.0)

        assert source.fetch(sarah, min_completion=0.0) == []

    def test_an_unreadable_database_never_fails_the_action(self, sessions, sarah, tmp_path: Path):
        """It's someone's live 2 GB production database — a bad path must degrade, not raise."""
        from shortlist.engine.clients.plex_db import PlexDbReader

        source = StoreHistorySource(
            sessions,
            _FakeUpstream([_item(1, "Played", days_ago=1)]),
            min_completion=0.0,
            flags=PlexDbReader(tmp_path / "does-not-exist"),
        )

        assert source.reconcile_flags(sarah) == 0, "a bad path degrades to a no-op, not an exception"
        got = source.fetch(sarah, min_completion=0.0)
        assert [w.title for w in got] == ["Played"], "the API-sourced history must survive intact"

    def test_re_running_does_not_duplicate_flags(self, sessions, sarah, plexdb_dir):
        reader = self._reader(plexdb_dir, [(100, 9587, "Gravity", 2013, self.MOVIE_TYPE, 1, 1728609330)])
        source = StoreHistorySource(sessions, _FakeUpstream([]), min_completion=0.0, flags=reader)

        source.reconcile_flags(sarah)
        source.reconcile_flags(sarah)

        assert _count(sessions) == 1

    def test_another_accounts_flags_are_not_borrowed(self, sessions, sarah, plexdb_dir):
        """Keyed on plex_account_id — mapping one person's marks onto another would be far worse
        than the bug this fixes."""
        reader = self._reader(plexdb_dir, [(999, 1, "Not theirs", 2020, self.MOVIE_TYPE, 1, 100)])
        source = StoreHistorySource(sessions, _FakeUpstream([]), min_completion=0.0, flags=reader)

        assert source.reconcile_flags(sarah) == 0
        assert source.fetch(sarah, min_completion=0.0) == []


class TestOwnerAccountResolution:
    """The `user_type` matrix's owner cell, which the first version of this feature missed entirely.

    `metadata_item_settings.account_id` is the PMS-LOCAL account space — the same one
    `history?accountID=` uses — and the owner is not in it under their plex.tv id (their local row
    is id=1). Passing the plex.tv id through matched zero rows and logged nothing, so the one person
    who can configure this feature would have seen it silently do nothing for themselves.
    """

    MOVIE_TYPE = 1
    PLEXTV_ID, PMS_LOCAL_ID = 218833834, 1

    def test_the_owners_flags_are_read_under_their_pms_local_id(self, sessions, tmp_path: Path):
        from shortlist.engine.clients.plex_db import PlexDbReader
        from tests.unit.test_plex_db import make_plex_db

        with sessions() as session:
            session.add(
                User(
                    plex_account_id=self.PLEXTV_ID,
                    username="S_FLIX",
                    slug="s_flix",
                    enabled=True,
                    user_type=UserType.OWNER.value,
                )
            )
            session.commit()
        owner = UserProfile(username="S_FLIX", plex_account_id=self.PLEXTV_ID, user_type=UserType.OWNER, slug="s_flix")
        (tmp_path / "db").mkdir()
        # Only present under the LOCAL id — exactly how a real PMS records the owner.
        reader = PlexDbReader(
            make_plex_db(tmp_path / "db", [(self.PMS_LOCAL_ID, 9587, "Gravity", 2013, self.MOVIE_TYPE, 1, 100)])
        )
        source = StoreHistorySource(
            sessions,
            _FakeUpstream([]),
            min_completion=0.0,
            flags=reader,
            flag_account_id=lambda u: self.PMS_LOCAL_ID if u.user_type is UserType.OWNER else u.plex_account_id,
        )

        assert source.reconcile_flags(owner) == 1
        assert [w.title for w in source.fetch(owner, min_completion=0.0)] == ["Gravity"]

    def test_without_the_resolver_a_shared_user_still_uses_their_own_id(self, sessions, sarah, tmp_path: Path):
        """Everyone except the owner IS in the PMS space under the id we already hold."""
        from shortlist.engine.clients.plex_db import PlexDbReader
        from tests.unit.test_plex_db import make_plex_db

        (tmp_path / "db2").mkdir()
        reader = PlexDbReader(make_plex_db(tmp_path / "db2", [(100, 1, "Theirs", 2020, self.MOVIE_TYPE, 1, 100)]))
        source = StoreHistorySource(sessions, _FakeUpstream([]), min_completion=0.0, flags=reader)

        source.reconcile_flags(sarah)
        assert [w.title for w in source.fetch(sarah, min_completion=0.0)] == ["Theirs"]
