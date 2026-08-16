"""Did they FINISH it, or just start it? — `WatchedItem.is_finished` and `picks.finished_at`.

`watched_at` comes from Plex's binary watched flag. For a movie that flag means played; for a SERIES
it flips on the first finished episode, so one episode of a 60-episode show has always scored
identically to a whole film. Measured on a real 47-user server (2026-08-16): of 158 show picks
credited as watched, only 21 were actually finished.

The threshold for a series is OURS by necessity — Plex publishes no show-level watched flag, only
`viewedLeafCount`/`leafCount` (`tests/fixtures/pms_watched_shows.xml.txt` records `2/176` coming back
as "watched"). These tests pin the strictest reading: every episode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.server.db.models import Base, PickRow, User
from shortlist.server.services.run_persistence import reconcile_watched
from shortlist.server.services.run_service import HIT_WINDOW_DAYS

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def watched_item(media_type: MediaType, *, tmdb_id: int = 100, viewed=None, leaf=None, when=None) -> WatchedItem:
    return WatchedItem(
        title="T",
        media_type=media_type,
        watched_at=when or NOW,
        tmdb_id=tmdb_id,
        viewed_leaf_count=viewed,
        leaf_count=leaf,
    )


class TestIsFinished:
    """The matrix that matters: type x episode counts. Every cell drives different reporting."""

    def test_a_movie_is_finished_because_plex_only_reports_it_once_played(self):
        assert watched_item(MediaType.MOVIE).is_finished is True

    def test_a_series_with_every_episode_watched_is_finished(self):
        assert watched_item(MediaType.SHOW, viewed=12, leaf=12).is_finished is True

    def test_a_series_watched_beyond_its_episode_count_is_finished(self):
        """Observed live: an on-deck item read 145% of its duration. Counts can exceed their total
        (a re-scanned library, a bulk mark), and `>=` must not turn that into 'unfinished'."""
        assert watched_item(MediaType.SHOW, viewed=14, leaf=12).is_finished is True

    @pytest.mark.parametrize("viewed", [1, 2, 3, 11])
    def test_a_series_short_of_its_last_episode_is_not_finished(self, viewed):
        """3 of 12 is the case the engine calls 'already seen' (its bar is min(80%, max(3, 15%))).
        Reporting deliberately disagrees: 'engaged enough not to re-recommend' is not 'finished'."""
        assert watched_item(MediaType.SHOW, viewed=viewed, leaf=12).is_finished is False

    def test_the_one_episode_case_that_plex_itself_calls_watched(self):
        """`unwatched=0` returns a series from its first episode — the whole reason this exists."""
        assert watched_item(MediaType.SHOW, viewed=1, leaf=176).is_finished is False

    @pytest.mark.parametrize("leaf", [None, 0])
    def test_a_series_with_no_known_episode_total_is_not_finished(self, leaf):
        """The opposite lean to the already-watched rule, which counts an unknown total as watched to
        avoid re-recommending. Here 'we cannot show they finished it' must not become a claim."""
        assert watched_item(MediaType.SHOW, viewed=9, leaf=leaf).is_finished is False

    def test_a_series_reporting_zero_watched_episodes_is_not_finished(self):
        assert watched_item(MediaType.SHOW, viewed=0, leaf=12).is_finished is False


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


@pytest.fixture
def user(sessions):
    with sessions() as session:
        session.add(User(id=1, plex_account_id=99, username="alex", slug="alex"))
        session.commit()
    return "alex"


def add_pick(sessions, *, tmdb_id: int, media_type: str, created: datetime, watched=None, finished=None) -> int:
    with sessions() as session:
        pick = PickRow(
            user_id=1,
            tmdb_id=tmdb_id,
            media_type=media_type,
            rating_key=1,
            rank=1,
            created_at=created,
            watched_at=watched,
            finished_at=finished,
        )
        session.add(pick)
        session.commit()
        return pick.id


def read(sessions, pick_id: int) -> PickRow:
    with sessions() as session:
        return session.query(PickRow).filter_by(id=pick_id).one()


def profile(history: list[WatchedItem]) -> UserProfile:
    return UserProfile(
        username="alex",
        plex_account_id=99,
        user_type=UserType.SHARED,
        slug="alex",
        history=history,
    )


class TestReconcileStampsFinished:
    def test_a_watched_movie_is_stamped_watched_and_finished_together(self, sessions, user):
        delivered = NOW - timedelta(days=3)
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=delivered)

        reconcile_watched(
            sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW - timedelta(days=1))])]
        )

        pick = read(sessions, pid)
        assert pick.watched_at is not None
        assert pick.finished_at == pick.watched_at, "a movie has no middle state — one event, both columns"

    def test_a_series_credited_on_one_episode_is_watched_but_not_finished(self, sessions, user):
        """The bug in one assertion: this pick counts as a hit and must NOT count as finished."""
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=3))

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=1, leaf=60, when=NOW - timedelta(days=1))])],
        )

        pick = read(sessions, pid)
        assert pick.watched_at is not None
        assert pick.finished_at is None

    def test_a_series_watched_out_is_stamped_finished(self, sessions, user):
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=3))

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=60, leaf=60, when=NOW - timedelta(days=1))])],
        )

        assert read(sessions, pid).finished_at is not None

    def test_an_already_credited_series_is_stamped_when_it_later_completes(self, sessions, user):
        """The forward-fill path. This pick was credited weeks ago and carries no `finished_at`; the
        migration deliberately leaves series NULL, so the only way it ever fills is right here."""
        old_watch = NOW - timedelta(days=40)
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=50), watched=old_watch)

        last_episode = NOW - timedelta(days=1)
        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=12, leaf=12, when=last_episode)])],
        )

        pick = read(sessions, pid)
        assert pick.finished_at is not None
        assert pick.watched_at.replace(tzinfo=UTC) == old_watch, "crediting must not move — only completion is new"

    def test_completion_is_dated_by_the_last_episode_not_by_tonights_sync(self, sessions, user):
        """Stamping `now` would file an old completion in this week's trend bucket. The show's own
        `lastViewedAt` IS when they finished it, because it is the most recent episode they watched."""
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=20))
        last_episode = NOW - timedelta(days=9)

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=8, leaf=8, when=last_episode)])],
        )

        assert read(sessions, pid).finished_at.replace(tzinfo=UTC) == last_episode

    def test_a_watch_before_delivery_stamps_neither_column(self, sessions, user):
        """Recommending something they had already seen is not a hit, and must not become a finish."""
        pid = add_pick(sessions, tmdb_id=200, media_type="show", created=NOW - timedelta(days=2))

        reconcile_watched(
            sessions,
            [profile([watched_item(MediaType.SHOW, tmdb_id=200, viewed=12, leaf=12, when=NOW - timedelta(days=30))])],
        )

        pick = read(sessions, pid)
        assert pick.watched_at is None
        assert pick.finished_at is None

    def test_a_watch_past_the_hit_window_stamps_neither_column(self, sessions, user):
        delivered = NOW - timedelta(days=HIT_WINDOW_DAYS + 10)
        pid = add_pick(sessions, tmdb_id=100, media_type="movie", created=delivered)

        reconcile_watched(sessions, [profile([watched_item(MediaType.MOVIE, tmdb_id=100, when=NOW)])])

        pick = read(sessions, pid)
        assert pick.watched_at is None
        assert pick.finished_at is None

    def test_a_movie_and_a_series_sharing_a_tmdb_id_are_kept_apart(self, sessions, user):
        """A TMDB id is unique only within its namespace. If the key dropped media_type, finishing the
        film would mark the series finished too."""
        movie = add_pick(sessions, tmdb_id=42, media_type="movie", created=NOW - timedelta(days=3))
        show = add_pick(sessions, tmdb_id=42, media_type="show", created=NOW - timedelta(days=3))

        reconcile_watched(
            sessions,
            [
                profile(
                    [
                        watched_item(MediaType.MOVIE, tmdb_id=42, when=NOW - timedelta(days=1)),
                        watched_item(MediaType.SHOW, tmdb_id=42, viewed=2, leaf=20, when=NOW - timedelta(days=1)),
                    ]
                )
            ],
        )

        assert read(sessions, movie).finished_at is not None
        assert read(sessions, show).watched_at is not None
        assert read(sessions, show).finished_at is None
