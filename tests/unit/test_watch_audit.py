"""Adversarial audit of watch tracking: idempotency, boundaries, empties, and time.

Separate from `test_watch_events.py`, which tests the feature as designed. This file tests it as
ATTACKED — the states a real server reaches that a happy-path fixture never does. Several of these
were written to fail, and did.
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
    Delivery,
    PickRow,
    Run,
    RunSharedRow,
    User,
    WatchEvent,
    WatchSession,
)
from shortlist.server.services.report_service import BOUNCE_PERCENT, engagement
from shortlist.server.services.run_persistence import reconcile_watched
from shortlist.server.services.watch_events import (
    RowMembership,
    event_credits,
    ingest_play_history,
    session_progress,
    tmdb_by_rating_key,
)

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


@pytest.fixture
def world(sessions):
    with sessions() as s:
        s.add(User(id=1, plex_account_id=99, username="alex", slug="alex"))
        s.add(Collection(id=1, slug="picked", name="Picked", enabled=True))
        s.add(Run(id=1, trigger="schedule", status="ok", started_at=NOW - timedelta(days=2)))
        s.add(Run(id=2, trigger="schedule", status="ok", started_at=NOW - timedelta(days=1)))
        s.add(Delivery(collection_slug="picked", user_slug="alex", library_key="1", rating_key=1))
        s.commit()
    return sessions


def pick(sessions, run_id, tmdb_id, *, rating_key, created=None, **kw):
    with sessions() as s:
        s.add(
            PickRow(
                run_id=run_id,
                user_id=1,
                collection_slug="picked",
                section_key="1",
                library="Movies",
                tmdb_id=tmdb_id,
                media_type="movie",
                rating_key=rating_key,
                rank=1,
                created_at=created or NOW - timedelta(days=2),
                **kw,
            )
        )
        s.commit()


def session_row(sessions, rating_key, *, started, offset, duration=6_000_000, **kw):
    with sessions() as s:
        s.add(
            WatchSession(
                plex_account_id=99,
                session_key="1",
                rating_key=rating_key,
                media_type="movie",
                started_at=started,
                last_seen_at=started + timedelta(minutes=10),
                ended_at=started + timedelta(minutes=10),
                max_offset_ms=offset,
                duration_ms=duration,
                end_reason="stopped",
                **kw,
            )
        )
        s.commit()


def profile(history=()):
    return UserProfile(
        username="alex", plex_account_id=99, user_type=UserType.SHARED, slug="alex", history=list(history)
    )


class TestIdempotency:
    """The reconcile runs seven times a day forever. Anything it does twice, it must do once."""

    def test_running_the_reconcile_twice_changes_nothing_the_second_time(self, world):
        pick(world, 1, 510, rating_key=10)
        session_row(world, 10, started=NOW - timedelta(days=1, hours=6), offset=1_200_000)

        reconcile_watched(world, [profile()])
        with world() as s:
            first = [(p.id, p.watched_at, p.max_percent, p.finished_at) for p in s.query(PickRow).all()]
        reconcile_watched(world, [profile()])
        with world() as s:
            second = [(p.id, p.watched_at, p.max_percent, p.finished_at) for p in s.query(PickRow).all()]

        assert first == second

    def test_the_credit_timestamp_never_moves_once_set(self, world):
        """A later play must not re-date the credit. The hit belongs to the week the row worked."""
        pick(world, 1, 510, rating_key=10)
        first_play = NOW - timedelta(days=1, hours=6)
        session_row(world, 10, started=first_play, offset=1_200_000)
        reconcile_watched(world, [profile()])
        with world() as s:
            original = s.query(PickRow).filter_by(tmdb_id=510).first().watched_at

        session_row(world, 10, started=NOW - timedelta(minutes=5), offset=5_000_000)
        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).first().watched_at == original

    def test_progress_never_walks_backwards(self, world):
        """They watch 80%, then start it again from the beginning. The furthest they got is 80%."""
        pick(world, 1, 510, rating_key=10)
        session_row(world, 10, started=NOW - timedelta(days=1, hours=8), offset=4_800_000)
        reconcile_watched(world, [profile()])
        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).first().max_percent == 80

        session_row(world, 10, started=NOW - timedelta(hours=1), offset=60_000)
        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).first().max_percent == 80

    def test_ingesting_an_overlapping_window_adds_nothing(self, sessions):
        store = MagicMock()
        store.get.return_value = None
        plex = MagicMock()
        plex.play_history.return_value = [PlayEvent(99, 10, None, "movie", NOW, "h1")]
        for _ in range(3):
            with sessions() as s:
                ingest_play_history(s, plex, store)
                s.commit()

        with sessions() as s:
            assert s.query(WatchEvent).count() == 1


class TestBoundaries:
    """Every threshold, at the exact value. Off-by-one here mislabels a real outcome."""

    @pytest.mark.parametrize(
        ("percent", "expected"),
        [(0, "bounced"), (BOUNCE_PERCENT - 1, "bounced"), (BOUNCE_PERCENT, "dropped"), (100, "dropped")],
    )
    def test_the_bounce_threshold_is_exclusive_at_the_bottom(self, world, percent, expected):
        """`< BOUNCE_PERCENT` is a bounce; exactly at it is a drop. A pick at 100% with no
        `finished_at` is still "dropped" — for a SERIES that is correct, since finishing means every
        episode, not one episode played to the end."""
        pick(world, 1, 510, rating_key=10, watched_at=NOW - timedelta(hours=2), max_percent=percent)

        with world() as s:
            outcomes = {p["outcome"] for p in engagement(s, "30")["people"][0]["picks"]}

        assert outcomes == {expected}

    def test_a_play_at_the_exact_delivery_instant_counts(self, world):
        """`<=`, not `<`. A run that delivers at 17:30:00 and a play stamped 17:30:00 is the row
        working, not a race to be discarded."""
        delivered = NOW - timedelta(days=1)  # run 2's started_at exactly
        pick(world, 2, 510, rating_key=10)
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=delivered,
                    source="history",
                    history_key="exact",
                )
            )
            s.commit()

        with world() as s:
            assert (1, 510, "movie") in event_credits(s, RowMembership(s))

    def test_a_play_one_second_before_the_delivery_does_not(self, world):
        pick(world, 2, 510, rating_key=10)
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(days=1, seconds=1),
                    source="history",
                    history_key="early",
                )
            )
            s.commit()

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_a_zero_length_item_yields_no_percentage_rather_than_dividing_by_zero(self, world):
        pick(world, 1, 510, rating_key=10)
        session_row(world, 10, started=NOW - timedelta(hours=6), offset=1000, duration=0)

        with world() as s:
            assert session_progress(s)[(99, 510, "movie")][1] is None


class TestEmptyAndNullStates:
    """A fresh install, a pruned database, a half-configured server."""

    def test_everything_survives_a_database_with_no_picks_at_all(self, world):
        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}
            assert session_progress(s) == {}
            assert tmdb_by_rating_key(s) == {}
            assert engagement(s, "30")["people"] == []
        reconcile_watched(world, [profile()])  # must not raise

    def test_an_event_for_an_account_we_do_not_know_is_ignored(self, world):
        pick(world, 1, 510, rating_key=10)
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=123456,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(hours=6),
                    source="history",
                    history_key="stranger",
                )
            )
            s.commit()

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_a_pick_with_no_run_cannot_be_placed_in_time(self, world):
        pick(world, None, 510, rating_key=10)
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(hours=6),
                    source="history",
                    history_key="detached",
                )
            )
            s.commit()

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_a_shared_row_with_no_ids_in_its_json_matches_nothing(self, world):
        """Every row written before 0076 is in this state — title and year only."""
        with world() as s:
            s.add(Collection(id=2, slug="popular", name="Popular", enabled=True, build="shared"))
            s.add(RunSharedRow(run_id=1, collection_slug="popular", picks=[{"title": "T", "year": 2020}]))
            s.commit()

        with world() as s:
            user = s.get(User, 1)
            assert RowMembership(s).visible_rows(user, {510}, NOW) == []


class TestTimeHandling:
    """Naive vs aware, and the cursor. SQLite returns naive UTC; Plex sends epochs."""

    def test_naive_timestamps_from_sqlite_do_not_crash_the_comparison(self, world):
        """Everything read back from SQLite is naive. A bare `<` against an aware datetime raises
        TypeError, which inside the reconcile would take the whole pass down."""
        pick(world, 1, 510, rating_key=10)
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=(NOW - timedelta(hours=6)).replace(tzinfo=None),
                    source="history",
                    history_key="naive",
                )
            )
            s.commit()

        with world() as s:
            assert (1, 510, "movie") in event_credits(s, RowMembership(s))

    def test_the_cursor_walks_backwards_when_a_page_is_full(self, sessions):
        """A full page means more is behind it. Advancing to the newest event would step over the
        remainder for ever, because the next read starts from there."""
        store = MagicMock()
        store.get.return_value = None
        plex = MagicMock()
        oldest = NOW - timedelta(days=5)
        plex.play_history.return_value = [
            PlayEvent(99, i, None, "movie", NOW - timedelta(days=d), f"h{i}")
            for i, d in enumerate([1, 2, 3, 4, 5], start=1)
        ]

        with sessions() as s:
            ingest_play_history(s, plex, store, limit=5)
            s.commit()

        assert store.set.call_args.args[1] == oldest.isoformat(), "parked at the oldest, to continue downward"


class TestTheEngineIsUnaffected:
    """This whole change is read-only with respect to Plex and must not touch what the engine does."""

    def test_the_reconcile_writes_only_to_picks_and_prefs(self, world):
        pick(world, 1, 510, rating_key=10)
        session_row(world, 10, started=NOW - timedelta(days=1, hours=6), offset=1_200_000)
        with world() as s:
            before = {
                "collections": s.query(Collection).count(),
                "runs": s.query(Run).count(),
                "deliveries": s.query(Delivery).count(),
                "events": s.query(WatchEvent).count(),
                "sessions": s.query(WatchSession).count(),
            }

        reconcile_watched(
            world,
            [
                profile(
                    [
                        WatchedItem(
                            title="T", media_type=MediaType.MOVIE, watched_at=NOW - timedelta(hours=2), tmdb_id=510
                        )
                    ]
                )
            ],
        )

        with world() as s:
            after = {
                "collections": s.query(Collection).count(),
                "runs": s.query(Run).count(),
                "deliveries": s.query(Delivery).count(),
                "events": s.query(WatchEvent).count(),
                "sessions": s.query(WatchSession).count(),
            }
        assert before == after, "the reconcile must not create or destroy anything but pick stamps"


class TestTmdbIdsAreNamespacedPerMediaType:
    """Movie 1399 and show 1399 are different titles.

    Both TMDB id sequences start at 1 and overlap heavily, so a bare number is not a title. This is
    the same key-space collision as keying on a Plex rating key, one layer down — and it was
    introduced BY the fix for that one, which swapped rating keys for bare tmdb ids.
    """

    def test_a_film_and_a_show_sharing_a_number_are_never_confused(self, world):
        with world() as s:
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    tmdb_id=1399,
                    media_type="movie",
                    rating_key=10,
                    rank=1,
                    title="A film",
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="2",
                    library="TV",
                    tmdb_id=1399,
                    media_type="show",
                    rating_key=20,
                    rank=1,
                    title="A series",
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(hours=6),
                    source="history",
                    history_key="film",
                )
            )
            s.commit()

        with world() as s:
            credits = event_credits(s, RowMembership(s))

        assert (1, 1399, "movie") in credits
        assert (1, 1399, "show") not in credits, "playing the film must not credit the series"

    def test_a_films_progress_does_not_land_on_a_show_with_the_same_number(self, world):
        with world() as s:
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    tmdb_id=1399,
                    media_type="movie",
                    rating_key=10,
                    rank=1,
                    title="A film",
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="2",
                    library="TV",
                    tmdb_id=1399,
                    media_type="show",
                    rating_key=20,
                    rank=1,
                    title="A series",
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.commit()
        session_row(world, 10, started=NOW - timedelta(hours=6), offset=3_000_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            film = s.query(PickRow).filter_by(media_type="movie").one()
            show = s.query(PickRow).filter_by(media_type="show").one()
        assert film.max_percent == 50
        assert show.max_percent is None, "the series was never played"


class TestARowPlexNoLongerHasCreditsNothing:
    """The delivery ledger is the only record of what is actually ON the server.

    `collections` says a row is CONFIGURED. The ledger says the collection exists for that person, and
    the two diverge whenever a row is muted for someone, they leave its audience, or a cold start
    skips them — the run DELETES their collection while the row definition stays enabled. The snapshot
    path has always joined the ledger; the event path did not, and `_contained_at` never expires a
    timeline, so the last contents of a row that stopped being delivered stayed creditable for ever.
    """

    def test_a_row_whose_collection_was_removed_stops_crediting(self, world):
        pick(world, 1, 510, rating_key=10)
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(hours=6),
                    source="history",
                    history_key="after-removal",
                )
            )
            s.commit()
        with world() as s:
            assert (1, 510, "movie") in event_credits(s, RowMembership(s)), "sanity: creditable while present"
            s.query(Delivery).delete()  # what `_forget_removed_deliveries` does on a mute or a skip
            s.commit()

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}

    def test_another_persons_delivery_does_not_make_it_visible(self, world):
        """The ledger is per (row, user, library) — someone else still having the row is not this
        person having it."""
        pick(world, 1, 510, rating_key=10)
        with world() as s:
            s.add(User(id=2, plex_account_id=100, username="sam", slug="sam"))
            s.query(Delivery).delete()
            s.add(Delivery(collection_slug="picked", user_slug="sam", library_key="1", rating_key=9))
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(hours=6),
                    source="history",
                    history_key="someone-else",
                )
            )
            s.commit()

        with world() as s:
            assert event_credits(s, RowMembership(s)) == {}


class TestOutcomesAreDecidedPerTitleNotPerRow:
    """A title has one pick row per delivery. The outcome belongs to the (person, title).

    The stamps are bounded to rows delivered at or before the play, so a run firing AFTER someone
    finished something leaves a row with no `finished_at` but a `max_percent` — and that row alone
    reads as "dropped at 96%". Precondition: the play landed between the last watch-sync and the next
    run, which is the ordinary 4-hour window before every nightly rebuild.
    """

    def _finished_then_redelivered(self, world):
        watched = NOW - timedelta(days=1, hours=3)
        # Delivered and finished...
        pick(
            world,
            1,
            510,
            rating_key=10,
            created=NOW - timedelta(days=2),
            watched_at=watched,
            finished_at=watched,
            max_percent=96,
        )
        # ...then redelivered by a run that started after the play, so this row carries no stamps.
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=1), max_percent=96)

    def test_a_finished_title_is_not_also_counted_as_dropped(self, world):
        from shortlist.server.services.report_service import effectiveness

        self._finished_then_redelivered(world)

        with world() as s:
            overall = effectiveness(s, "30")["overall"]

        assert overall["finished"] == 1
        assert overall["dropped"] == 0, "the same title cannot be both"
        assert overall["bounced"] == 0

    def test_the_person_page_reports_it_as_finished(self, world):
        self._finished_then_redelivered(world)

        with world() as s:
            picks = engagement(s, "30")["people"][0]["picks"]

        assert [p["outcome"] for p in picks] == ["finished"]

    def test_an_abandoned_title_counts_once_however_many_nights_it_was_delivered(self, world):
        """`stop_points` used to count delivery ROWS: one abandonment redelivered five nights read as
        five, and the error scales with how long a title lingers — which for an abandoned title is
        exactly the ones that linger longest."""
        for run_id, day in ((1, 2), (2, 1)):
            pick(
                world,
                run_id,
                510,
                rating_key=10,
                created=NOW - timedelta(days=day),
                watched_at=NOW - timedelta(days=1, hours=6),
                max_percent=30,
            )

        with world() as s:
            data = engagement(s, "30")

        assert sum(b["count"] for b in data["stop_points"]) == 1
        assert len(data["people"][0]["picks"]) == 1


class TestFinishedAtIsWhenTheyFinished:
    def test_a_series_is_not_dated_from_the_night_they_started_it(self, world):
        """`finished_at` used to take the EARLIEST play, which for a 60-episode show is episode 1 —
        months early, and possibly before the row's own `watched_at`."""
        started = NOW - timedelta(days=1, hours=8)
        completed = NOW - timedelta(hours=1)
        with world() as s:
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="TV",
                    tmdb_id=700,
                    media_type="show",
                    rating_key=70,
                    rank=1,
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=70,
                    media_type="episode",
                    viewed_at=started,
                    source="history",
                    history_key="ep1",
                )
            )
            s.commit()

        reconcile_watched(
            world,
            [
                profile(
                    [
                        WatchedItem(
                            title="A series",
                            media_type=MediaType.SHOW,
                            watched_at=completed,
                            tmdb_id=700,
                            viewed_leaf_count=10,
                            leaf_count=10,
                        )
                    ]
                )
            ],
        )

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=700).one()
        assert row.watched_at.replace(tzinfo=UTC) == started, "credited when the row got them to start"
        assert row.finished_at.replace(tzinfo=UTC) == completed, "finished when the last episode landed"
        assert row.finished_at >= row.watched_at
