"""Play events, and the historical membership test they exist to feed.

The rule: credit the pick if there is a play at time T where the title was in a row that person could
see at T. Every test here is about T — that the question is asked of the PAST, not of the row as it
stands now. The distinction is not academic: being watched is exactly what makes the engine drop a
title from a row, so "is it in their row now" is false for precisely the titles that earned a credit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.clients.plex_pms import PlayEvent
from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.server.db.models import (
    Base,
    Collection,
    CollectionAudience,
    Delivery,
    PickRow,
    Run,
    RunSharedRow,
    User,
    WatchEvent,
    WatchSession,
)
from shortlist.server.services.run_persistence import reconcile_watched
from shortlist.server.services.watch_events import (
    CURSOR_KEY,
    RowMembership,
    event_credits,
    ingest_play_history,
    session_progress,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


@pytest.fixture
def world(sessions):
    """One user, one enabled row, and two runs a day apart."""
    with sessions() as s:
        s.add(User(id=1, plex_account_id=99, username="alex", slug="alex"))
        s.add(Collection(id=1, slug="picked", name="Picked for You", enabled=True))
        s.add(Run(id=1, trigger="schedule", status="ok", started_at=NOW - timedelta(days=2)))
        s.add(Run(id=2, trigger="schedule", status="ok", started_at=NOW - timedelta(days=1)))
        # The delivery-ledger entry a real run leaves behind. `live_pick_ids` requires it — liveness
        # means the collection is still ON PLEX — so without it the snapshot credit path sees nothing
        # at all and a test of it passes for the wrong reason.
        s.add(Delivery(collection_slug="picked", user_slug="alex", library_key="1", rating_key=1))
        s.commit()
    return sessions


def deliver(sessions, run_id: int, rating_keys, *, tmdb_base: int = 500, slug: str = "picked"):
    """One delivery of a row: the picks that run put in it."""
    with sessions() as s:
        for rk in rating_keys:
            s.add(
                PickRow(
                    run_id=run_id,
                    user_id=1,
                    collection_slug=slug,
                    section_key="1",
                    library="Movies",
                    tmdb_id=tmdb_base + rk,
                    media_type="movie",
                    rating_key=rk,
                    rank=1,
                    created_at=NOW - timedelta(days=2),
                )
            )
        s.commit()


def play(sessions, rating_key: int, when: datetime, *, show_rating_key=None, key: str | None = None):
    with sessions() as s:
        s.add(
            WatchEvent(
                plex_account_id=99,
                rating_key=rating_key,
                show_rating_key=show_rating_key,
                media_type="episode" if show_rating_key else "movie",
                viewed_at=when,
                source="history",
                history_key=key or f"h{rating_key}-{when.timestamp()}",
            )
        )
        s.commit()


class TestIngest:
    def test_new_plays_land_and_the_cursor_advances(self, sessions):
        store = MagicMock()
        store.get.return_value = None
        newest = NOW - timedelta(hours=1)
        plex = MagicMock()
        plex.play_history.return_value = [
            PlayEvent(99, 10, None, "movie", NOW - timedelta(hours=3), "h1"),
            PlayEvent(99, 20, 21, "episode", newest, "h2"),
        ]

        with sessions() as s:
            assert ingest_play_history(s, plex, store) == 2
            s.commit()

        with sessions() as s:
            rows = s.query(WatchEvent).order_by(WatchEvent.viewed_at).all()
        assert [r.rating_key for r in rows] == [10, 20]
        assert rows[1].show_rating_key == 21
        store.set.assert_called_once_with(CURSOR_KEY, newest.isoformat())

    def test_re_reading_the_same_window_inserts_nothing(self, sessions):
        """The cursor is deliberately rewound a minute on every read, so overlap is the normal case,
        not an edge one. `history_key` is unique, so the overlap costs nothing — which is what makes
        it safe to re-ask rather than risk losing an event stamped in the second we read past it."""
        store = MagicMock()
        store.get.return_value = None
        plex = MagicMock()
        plex.play_history.return_value = [PlayEvent(99, 10, None, "movie", NOW, "h1")]

        with sessions() as s:
            ingest_play_history(s, plex, store)
            s.commit()
        with sessions() as s:
            assert ingest_play_history(s, plex, store) == 0
            s.commit()

        with sessions() as s:
            assert s.query(WatchEvent).count() == 1

    def test_an_unreadable_cursor_backfills_instead_of_crashing(self, sessions):
        store = MagicMock()
        store.get.return_value = "not-a-date"
        plex = MagicMock()
        plex.play_history.return_value = []

        with sessions() as s:
            assert ingest_play_history(s, plex, store) == 0

        since = plex.play_history.call_args.kwargs["since"]
        assert since < datetime.now(UTC) - timedelta(days=80), "fell back to the backfill window"


class TestMembershipIsAskedOfThePast:
    def test_a_title_in_the_row_at_the_time_counts(self, world):
        deliver(world, 1, [10, 11])
        play(world, 10, NOW - timedelta(days=1, hours=12))

        with world() as s:
            assert event_credits(s, RowMembership(s)) != {}

    def test_a_title_the_row_dropped_before_the_play_does_not(self, world):
        """Run 1 delivered it, run 2 swapped it out, they played it after. They cannot have started it
        from a row that no longer listed it."""
        deliver(world, 1, [10])
        deliver(world, 2, [11])
        play(world, 10, NOW - timedelta(hours=2))

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_a_title_the_row_dropped_AFTER_the_play_still_counts(self, world):
        """The case the whole rewrite exists for. Run 2 dropped the title — because they watched it —
        and asking about now would score this genuine hit as zero. Asking about the play's own moment
        gets it right, and needs no snapshot taken before the rebuild."""
        deliver(world, 1, [10])
        deliver(world, 2, [11])
        play(world, 10, NOW - timedelta(days=1, hours=6))  # between run 1 and run 2

        with world() as s:
            credits = event_credits(s, RowMembership(s))
        assert credits == {(1, 510, "movie"): NOW - timedelta(days=1, hours=6)}

    def test_a_play_before_the_row_ever_had_it_does_not_count(self, world):
        deliver(world, 2, [10])
        play(world, 10, NOW - timedelta(days=5))

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_the_earliest_qualifying_play_is_the_credit(self, world):
        """A rewatch is not when the recommendation worked. Taking the newest event would file the hit
        in the wrong week of the trend chart for ever."""
        deliver(world, 1, [10])
        deliver(world, 2, [10])
        first = NOW - timedelta(days=1, hours=12)
        play(world, 10, first, key="a")
        play(world, 10, NOW - timedelta(hours=1), key="b")

        with world() as s:
            assert event_credits(s, RowMembership(s))[(1, 510, "movie")] == first

    def test_an_episode_is_matched_through_the_shows_key(self, world):
        """A pick for a series stores the SHOW's rating key; the log reports the EPISODE played. On 30
        days of real history 46 of 78 matches were reachable only through this mapping."""
        deliver(world, 1, [700])  # the show
        play(world, 7011, NOW - timedelta(hours=6), show_rating_key=700)

        with world() as s:
            assert (1, 1200, "movie") in event_credits(s, RowMembership(s))

    def test_a_play_of_something_never_recommended_is_ignored(self, world):
        deliver(world, 1, [10])
        play(world, 9999, NOW - timedelta(hours=6))

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_a_detached_pick_cannot_be_placed_in_time(self, world):
        """`DELETE /api/runs` and the retention prune both null `run_id`. Without a run there is no
        delivery time, so no claim about "was it in the row then" can be supported."""
        deliver(world, 1, [10])
        with world() as s:
            s.query(PickRow).update({"run_id": None})
            s.commit()
        play(world, 10, NOW - timedelta(hours=6))

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_a_deleted_row_credits_nothing(self, world):
        deliver(world, 1, [10])
        play(world, 10, NOW - timedelta(hours=6))
        with world() as s:
            s.query(Collection).filter_by(slug="picked").delete()
            s.commit()

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}


class TestSharedRows:
    """Shared rows write NO pick rows — `RunSharedRow` explains why — so their contents come out of
    the run record's JSON, and visibility out of the audience snapshotted beside it."""

    def _shared(self, sessions, run_id: int, rating_keys, audience=None):
        with sessions() as s:
            s.add(
                RunSharedRow(
                    run_id=run_id,
                    collection_slug="popular",
                    picks=[{"rating_key": rk, "tmdb_id": 900 + rk, "title": "T"} for rk in rating_keys],
                    audience=audience,
                )
            )
            s.commit()

    def test_a_public_shared_row_credits_anyone(self, world):
        with world() as s:
            s.add(Collection(id=2, slug="popular", name="Popular", enabled=True, build="shared"))
            # The title must also be a pick of theirs for the report to have anything to key on.
            s.commit()
        deliver(world, 1, [10], slug="picked")
        self._shared(world, 1, [10])
        play(world, 10, NOW - timedelta(days=1, hours=12))

        with world() as s:
            assert event_credits(s, RowMembership(s)) != {}

    def test_a_subset_row_credits_only_its_audience(self, world):
        with world() as s:
            s.add(Collection(id=2, slug="popular", name="Popular", enabled=True, build="shared", audience="subset"))
            s.commit()
        deliver(world, 1, [10], slug="picked")
        with world() as s:
            s.query(PickRow).delete()  # only the shared row can supply membership now
            s.commit()
        self._shared(world, 1, [10], audience=[12345])  # NOT account 99
        play(world, 10, NOW - timedelta(hours=6))

        with world() as s:
            membership = RowMembership(s)
            user = s.get(User, 1)
            assert membership.visible_rows(user, {10}, NOW - timedelta(hours=6)) == []

    def test_the_audience_is_read_from_the_snapshot_not_from_today(self, world):
        """`collection_audience` is current state with no history. Without the per-run snapshot,
        adding someone to a subset row today would retroactively credit watches from before they
        could see it."""
        with world() as s:
            s.add(Collection(id=2, slug="popular", name="Popular", enabled=True, build="shared", audience="subset"))
            s.add(CollectionAudience(collection_id=2, user_id=1))  # in the audience TODAY
            s.commit()
        self._shared(world, 1, [10], audience=[])  # but nobody could see it at delivery
        play(world, 10, NOW - timedelta(hours=6))

        with world() as s:
            user = s.get(User, 1)
            assert RowMembership(s).visible_rows(user, {10}, NOW - timedelta(hours=6)) == []

    def test_a_pre_snapshot_row_falls_back_to_public(self, world):
        """Every row written before 0076 carries `audience = NULL`. Treating that as "everyone" is
        exactly the behaviour that preceded the snapshot, so upgrading changes nothing."""
        with world() as s:
            s.add(Collection(id=2, slug="popular", name="Popular", enabled=True, build="shared"))
            s.commit()
        self._shared(world, 1, [10], audience=None)
        play(world, 10, NOW - timedelta(hours=6))

        with world() as s:
            user = s.get(User, 1)
            assert RowMembership(s).visible_rows(user, {10}, NOW - timedelta(hours=6)) == ["popular"]


class TestStartsCountEvenWhenTheFinishComesLater:
    """Steve's case, end to end: watch 20% of something from your row, finish it four days later once
    the row has moved on. The START is what the row earned, so the START is what counts."""

    def _session(self, sessions, rating_key, started, *, offset, duration, ended=None):
        with sessions() as s:
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=rating_key,
                    media_type="movie",
                    started_at=started,
                    last_seen_at=started + timedelta(minutes=20),
                    ended_at=ended,
                    max_offset_ms=offset,
                    duration_ms=duration,
                    end_reason="stopped",
                )
            )
            s.commit()

    def test_a_partial_watch_while_it_was_in_the_row_is_credited(self, world):
        """20% of a film generates NO history-log entry — Plex records completions only — so without
        the session this watch is invisible and the pick scores zero."""
        deliver(world, 1, [10])
        deliver(world, 2, [11])  # the row dropped it the next night
        self._session(world, 10, NOW - timedelta(days=1, hours=6), offset=1_200_000, duration=6_000_000)

        with world() as s:
            credits = event_credits(s, RowMembership(s))
        assert credits == {(1, 510, "movie"): NOW - timedelta(days=1, hours=6)}

    def test_the_credit_hangs_on_the_start_not_the_later_completion(self, world):
        """They finish it days later, by which time the row has dropped it. The completion alone would
        be rejected — correctly — so the start is what has to carry the credit."""
        deliver(world, 1, [10])
        deliver(world, 2, [11])
        started = NOW - timedelta(days=1, hours=6)
        self._session(world, 10, started, offset=1_200_000, duration=6_000_000)
        play(world, 10, NOW - timedelta(hours=1))  # the finish, after the row moved on

        with world() as s:
            credits = event_credits(s, RowMembership(s))
        assert credits[(1, 510, "movie")] == started

    def test_a_start_after_the_row_dropped_it_still_does_not_count(self, world):
        """Sessions do not weaken the rule — they only make it observable earlier."""
        deliver(world, 1, [10])
        deliver(world, 2, [11])
        self._session(world, 10, NOW - timedelta(hours=2), offset=1_200_000, duration=6_000_000)

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_the_furthest_sitting_wins_and_the_earliest_start_is_the_credit(self, world):
        """One title watched over four sittings, where the LAST one is not the furthest — they saw it
        out, then went back to the beginning. Taking the most recent sitting would report 20% for
        something they finished; taking the most recent START would date the credit to after the row
        had already moved on. Ordered deliberately so neither mistake can pass."""
        deliver(world, 1, [10])
        base = NOW - timedelta(days=1, hours=8)
        sittings = [(0, 540_000), (1, 6_000_000), (2, 900_000), (3, 1_200_000)]
        for hours, offset in sittings:
            self._session(world, 10, base + timedelta(hours=hours), offset=offset, duration=6_000_000)

        with world() as s:
            started, percent = session_progress(s)[(99, 10)]

        assert percent == 100, "the furthest point reached, not the last sitting's 20%"
        assert started == base, "the credit hangs on the FIRST sitting, not the most recent"


class TestEngagementReport:
    """The detail behind the Dropped tile: four outcomes, and the split that matters most."""

    def _pick(self, sessions, tmdb_id, *, watched, finished=None, percent=None, title="T"):
        with sessions() as s:
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    rating_key=tmdb_id,
                    rank=1,
                    title=title,
                    created_at=NOW - timedelta(days=2),
                    watched_at=watched,
                    finished_at=finished,
                    max_percent=percent,
                )
            )
            s.commit()

    def test_the_four_outcomes_are_told_apart(self, world):
        from shortlist.server.services.report_service import engagement

        self._pick(world, 1, watched=NOW - timedelta(hours=1), finished=NOW, percent=100, title="Seen out")
        self._pick(world, 2, watched=NOW - timedelta(hours=2), percent=40, title="Gave up")
        self._pick(world, 3, watched=NOW - timedelta(hours=3), percent=2, title="Bounced off")
        self._pick(world, 4, watched=NOW - timedelta(hours=4), percent=None, title="Unknown")

        with world() as s:
            data = engagement(s, "30")

        outcomes = {p["title"]: p["outcome"] for p in data["people"][0]["picks"]}
        assert outcomes == {
            "Seen out": "finished",
            "Gave up": "dropped",
            "Bounced off": "bounced",
            # No live session ever saw this one, so how far they got is UNKNOWN — not 0%, and not an
            # abandonment. Calling it "bounced" would invent the strongest negative signal on the
            # page out of missing data.
            "Unknown": "watching",
        }

    def test_a_title_only_one_person_dropped_is_not_called_a_losing_pick(self, world):
        """One person abandoning something is a night. The pattern across people is what makes it a
        bad recommendation, so the threshold is deliberately more than one."""
        from shortlist.server.services.report_service import engagement

        self._pick(world, 1, watched=NOW - timedelta(hours=1), percent=20)

        with world() as s:
            assert engagement(s, "30")["losing"] == []

    def test_a_title_several_people_drop_is_surfaced_with_where_they_stop(self, world):
        from shortlist.server.services.report_service import engagement

        with world() as s:
            s.add(User(id=2, plex_account_id=100, username="sam", slug="sam"))
            s.commit()
        self._pick(world, 7, watched=NOW - timedelta(hours=1), percent=10, title="Loses people")
        with world() as s:
            s.add(
                PickRow(
                    run_id=1,
                    user_id=2,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    tmdb_id=7,
                    media_type="movie",
                    rating_key=7,
                    rank=1,
                    title="Loses people",
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(hours=2),
                    max_percent=30,
                )
            )
            s.commit()

        with world() as s:
            losing = engagement(s, "30")["losing"]

        assert len(losing) == 1
        assert losing[0]["started"] == 2
        assert losing[0]["finished"] == 0
        assert losing[0]["stops_at"] in (10, 30), "the median of where the two of them stopped"

    def test_stop_points_bucket_the_abandons(self, world):
        from shortlist.server.services.report_service import engagement

        for i, percent in enumerate((3, 8, 20, 60, 90), start=1):
            self._pick(world, i, watched=NOW - timedelta(hours=i), percent=percent)

        with world() as s:
            points = {b["label"]: b["count"] for b in engagement(s, "30")["stop_points"]}

        assert points["0-10%"] == 2
        assert points["10-25%"] == 1
        assert points["50-75%"] == 1
        assert points["75%+"] == 1

    def test_a_finished_pick_is_not_counted_as_an_abandon(self, world):
        from shortlist.server.services.report_service import engagement

        self._pick(world, 1, watched=NOW - timedelta(hours=1), finished=NOW, percent=100)

        with world() as s:
            assert sum(b["count"] for b in engagement(s, "30")["stop_points"]) == 0


class TestReconcileActuallyUsesTheCredits:
    """At `reconcile_watched` level, not `event_credits` level.

    Every other test here calls the helper directly, and the helper was right the whole time — the
    CALLER discarded its answer. The candidate query gated on "in the live pick set OR already
    credited", so a pick the row had dropped was never even loaded, and a partial watch (which sets
    no Plex flag) had nothing to be credited against. Both are the cases the change exists for, and
    both were invisible to a test that stopped at the helper.
    """

    def _profile(self, history=()):
        return UserProfile(
            username="alex", plex_account_id=99, user_type=UserType.SHARED, slug="alex", history=list(history)
        )

    def test_a_dropped_title_is_credited_by_the_sweep_with_no_snapshot(self, world):
        """The standalone watch sweep passes no `live_picks` — 6 of the 7 reconciles a day. This is
        the exact path the event credit used to be gated out of."""
        deliver(world, 1, [10])
        deliver(world, 2, [11])  # the row dropped it
        play(world, 10, NOW - timedelta(days=1, hours=6))

        reconcile_watched(world, [self._profile()])

        with world() as s:
            pick = s.query(PickRow).filter_by(rating_key=10).one()
        assert pick.watched_at is not None, "the play log said it was on screen; the reconcile ignored it"

    def test_a_partial_watch_is_credited_though_plex_never_flagged_it(self, world):
        """Twenty minutes of a film sets no watched flag, so `latest_watch` is empty and every other
        path in the reconcile skips this person entirely."""
        deliver(world, 1, [10])
        with world() as s:
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=10,
                    media_type="movie",
                    started_at=NOW - timedelta(days=1, hours=6),
                    last_seen_at=NOW - timedelta(days=1, hours=5),
                    ended_at=NOW - timedelta(days=1, hours=5),
                    max_offset_ms=1_200_000,
                    duration_ms=6_000_000,
                    end_reason="stopped",
                )
            )
            s.commit()

        reconcile_watched(world, [self._profile()])

        with world() as s:
            pick = s.query(PickRow).filter_by(rating_key=10).one()
        assert pick.watched_at is not None, "a start is a credit even with no completion"
        assert pick.max_percent == 20

    def test_the_credit_and_the_percentage_land_on_the_SAME_rows(self, world):
        """The report intersects `created_at` and `watched_at` at row level, so a credit on the older
        delivery and a percentage on the newer one is invisible to it — the split counts neither."""
        deliver(world, 1, [10])
        deliver(world, 2, [10])  # same title, redelivered
        with world() as s:
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=10,
                    media_type="movie",
                    started_at=NOW - timedelta(days=1, hours=6),
                    last_seen_at=NOW - timedelta(days=1, hours=5),
                    ended_at=NOW - timedelta(days=1, hours=5),
                    max_offset_ms=1_200_000,
                    duration_ms=6_000_000,
                    end_reason="stopped",
                )
            )
            s.commit()

        reconcile_watched(world, [self._profile()])

        with world() as s:
            rows = s.query(PickRow).filter_by(rating_key=10).all()
        credited = [r for r in rows if r.watched_at is not None]
        assert credited, "nothing credited at all"
        for row in credited:
            assert row.max_percent == 20, "every credited row must carry the percentage too"

    def test_progress_is_never_read_from_a_tmdb_id(self, world):
        """`progress` is keyed by PLEX rating keys, and the id spaces overlap — rating keys on a real
        server reach 654,993, well inside TMDB's range. Looking a tmdb_id up in it reads an unrelated
        title's progress onto the pick."""
        # A pick whose own rating key was never played, whose TMDB id collides with a key that WAS.
        with world() as s:
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    tmdb_id=777,
                    media_type="movie",
                    rating_key=12,
                    rank=1,
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(hours=3),
                )
            )
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="9",
                    rating_key=777,
                    media_type="movie",
                    started_at=NOW - timedelta(hours=2),
                    last_seen_at=NOW - timedelta(hours=1),
                    ended_at=NOW - timedelta(hours=1),
                    max_offset_ms=5_940_000,
                    duration_ms=6_000_000,
                    end_reason="stopped",
                )
            )
            s.commit()

        # Real history, so the candidate loop actually runs. With an empty history the reconcile
        # skips the person entirely and this test would pass without ever reaching the lookup —
        # which is exactly how it passed the first time it was written.
        history = [
            WatchedItem(
                title="Something else",
                media_type=MediaType.MOVIE,
                watched_at=NOW - timedelta(hours=4),
                tmdb_id=512,
            )
        ]
        reconcile_watched(world, [self._profile(history)])

        with world() as s:
            pick = s.query(PickRow).filter_by(rating_key=12).one()
        assert pick.max_percent is None, "99% belonged to a different title that shares the number"


class TestBothCreditPathsAreNeeded:
    """The event path does not replace the snapshot path, and nearly was deleted as though it did.

    A title Plex flags as watched with no play behind it — marked by hand, bulk-marked, or watched
    before the log existed — generates no event ever. On the maintainer's server that is 893 of one
    user's 1,840 watched titles. The snapshot is the only thing that can credit them.
    """

    def test_a_hand_marked_watch_with_no_play_event_is_still_credited(self, world):
        deliver(world, 2, [10])  # in the CURRENT delivery, so the snapshot can see it
        history = [
            WatchedItem(
                title="Marked watched",
                media_type=MediaType.MOVIE,
                watched_at=NOW - timedelta(hours=2),
                tmdb_id=510,
            )
        ]
        profile = UserProfile(
            username="alex", plex_account_id=99, user_type=UserType.SHARED, slug="alex", history=history
        )

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}, "no play event exists for this title"

        reconcile_watched(world, [profile])

        with world() as s:
            assert s.query(PickRow).filter_by(rating_key=10).one().watched_at is not None
