"""Adversarial audit of watch tracking: idempotency, boundaries, empties, and time.

Separate from `test_watch_events.py`, which tests the feature as designed. This file tests it as
ATTACKED — the states a real server reaches that a happy-path fixture never does. Several of these
were written to fail, and did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
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
from shortlist.server.services.report_service import BOUNCE_PERCENT, engagement, resolve_outcomes
from shortlist.server.services.run_persistence import FINISHED_PERCENT, reconcile_watched
from shortlist.server.services.watch_events import (
    RowMembership,
    _attribution_floor,
    _scan_plays,
    _session_starts,
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
        [
            (0, "bounced"),
            (BOUNCE_PERCENT - 1, "bounced"),
            (BOUNCE_PERCENT, "dropped"),
            (FINISHED_PERCENT - 1, "dropped"),
        ],
    )
    def test_the_bounce_threshold_is_exclusive_at_the_bottom(self, world, percent, expected):
        """`< BOUNCE_PERCENT` is a bounce; exactly at it is a drop; up to `FINISHED_PERCENT` it stays
        a drop.

        This writes the pick row DIRECTLY, so it tests how `resolve_outcomes` READS a stored row, not
        how one comes to be stored. It used to carry a 100% row asserting "dropped", justified by
        "for a SERIES that is correct, since finishing means every episode" — a state the code cannot
        reach, because `session_progress` returns None for a show and a percentage is therefore always
        a film. Where a finished film is decided is `_decide_outcomes`, and
        `TestAFilmPlayedToTheEndIsFinished` covers it."""
        pick(world, 1, 510, rating_key=10, watched_at=NOW - timedelta(hours=2), max_percent=percent)

        with world() as s:
            outcomes = {p["outcome"] for p in engagement(s, "30")["people"][0]["picks"]}

        assert outcomes == {expected}

    def test_a_play_at_the_exact_delivery_instant_counts(self, world):
        """`<=`, not `<`. A run that delivers at 17:30:00 and a play stamped 17:30:00 is the row
        working, not a race to be discarded.

        `created=delivered` is the whole test. Without it this called `pick()` with the default
        `created_at` of `NOW - 2 days` against a play at `NOW - 1 day` — a full day apart, so it
        passed just as happily with the rule flipped to `<`. It was written when membership keyed on
        `Run.started_at`, and was never updated when `_load_per_person` moved to `PickRow.created_at`;
        the mutation audit of 2026-08-24 caught it asserting nothing about its own name.
        """
        delivered = NOW - timedelta(days=1)
        pick(world, 2, 510, rating_key=10, created=delivered)
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
        # `created` is the delivery moment now, not the run's start — see `_load_per_person`.
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=1))
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

    def test_the_cursor_only_ever_moves_forward(self, sessions):
        """It used to park at the OLDEST row of a full page, meaning to walk backwards through the
        backlog. It cannot: `since` is a lower bound and the read is newest-first, so the next call
        returns the same newest page again — the cursor regresses and never advances, re-reading the
        limit six times a day and inserting nothing."""
        store = MagicMock()
        store.get.return_value = None
        plex = MagicMock()
        newest = NOW - timedelta(days=1)
        plex.play_history.return_value = [
            PlayEvent(99, i, None, "movie", NOW - timedelta(days=d), f"h{i}")
            for i, d in enumerate([1, 2, 3, 4, 5], start=1)
        ]

        with sessions() as s:
            ingest_play_history(s, plex, store, limit=5)
            s.commit()

        assert store.set.call_args.args[1] == newest.isoformat()


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


class TestDeliveryTimeIsWhenTheRowActuallyChanged:
    """A run persists each person as they finish, so a delivery lands minutes to tens of minutes after
    the run starts — a TV collection write alone costs ~16.5s on the maintainer's server, times 47
    people. Timing membership from `Run.started_at` while the stamps are bounded by `created_at` put
    the two on different clocks, and BOTH directions lost.
    """

    def test_a_credit_is_not_computed_and_then_silently_discarded(self, world):
        """`event_credits` said the title was in the row; `_credit_from_events` then found no pick old
        enough to stamp, because every row for it was created after the play. Permanent: a watched
        title is never re-delivered, so `created_at` never moves."""
        run_started = NOW - timedelta(days=1)
        played = run_started + timedelta(minutes=15)
        persisted = run_started + timedelta(minutes=30)
        pick(world, 2, 510, rating_key=10, created=persisted)
        # The row was delivered by run 1 before all this, and run 2 re-delivered it late.
        pick(world, 1, 510, rating_key=10, created=NOW - timedelta(days=2))
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=played,
                    source="history",
                    history_key="mid-run",
                )
            )
            s.commit()

        reconcile_watched(world, [profile()])

        with world() as s:
            credited = s.query(PickRow).filter(PickRow.watched_at.isnot(None)).all()
        assert credited, "the credit was computed and then had nowhere to land"

    def test_a_play_during_a_run_is_judged_against_the_row_plex_was_still_serving(self, world):
        """Run 2 starts at 11:00 and drops the title, but does not rewrite THIS person's collection
        until 11:30. A play at 11:15 saw run 1's row, not run 2's."""
        run_started = NOW - timedelta(days=1)
        pick(world, 1, 510, rating_key=10, created=run_started - timedelta(days=1))
        # Run 2 dropped 510 and delivered something else — persisted 30 minutes in.
        pick(world, 2, 511, rating_key=11, created=run_started + timedelta(minutes=30))
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=run_started + timedelta(minutes=15),
                    source="history",
                    history_key="during",
                )
            )
            s.commit()

        with world() as s:
            assert (1, 510, "movie") in event_credits(s, RowMembership(s))


class TestACreditLandsOnlyOnTheRowThatShowedIt:
    """`visible_rows` works out exactly which shelves were showing the title. Throwing that away and
    stamping every pick for the person+title inflates the hit rate of rows that had already dropped it
    — `row_effectiveness` filters on `collection_slug`, so it counts a play its shelf could not have
    caused.
    """

    def test_a_row_that_dropped_the_title_does_not_collect_the_hit(self, world):
        with world() as s:
            s.add(Collection(id=2, slug="rewatch", name="Watch again", enabled=True))
            s.add(Delivery(collection_slug="rewatch", user_slug="alex", library_key="1", rating_key=2))
            s.commit()
        # `picked` had it two days ago and dropped it; `rewatch` has it now.
        with world() as s:
            for slug, created, run_id in (
                ("picked", NOW - timedelta(days=3), 1),
                ("rewatch", NOW - timedelta(days=1), 2),
            ):
                s.add(
                    PickRow(
                        run_id=run_id,
                        user_id=1,
                        collection_slug=slug,
                        section_key="1",
                        library="Movies",
                        tmdb_id=510,
                        media_type="movie",
                        rating_key=10,
                        rank=1,
                        created_at=created,
                    )
                )
            # `picked` re-delivered something else yesterday, so its newest delivery lacks 510.
            s.add(
                PickRow(
                    run_id=2,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    tmdb_id=511,
                    media_type="movie",
                    rating_key=11,
                    rank=1,
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(hours=2),
                    source="history",
                    history_key="rw",
                )
            )
            s.commit()

        with world() as s:
            credits = event_credits(s, RowMembership(s))
        assert credits[(1, 510, "movie")][1] == frozenset({"rewatch"})

        reconcile_watched(world, [profile()])

        with world() as s:
            by_slug = {
                (p.collection_slug, p.tmdb_id): p.watched_at
                for p in s.query(PickRow).filter(PickRow.tmdb_id == 510).all()
            }
        assert by_slug[("rewatch", 510)] is not None, "the row that showed it gets the credit"
        assert by_slug[("picked", 510)] is None, "the row that had dropped it must not"


class TestASeriesGetsNoPercentageFromOneEpisode:
    def test_one_full_episode_is_not_a_finished_series(self, world):
        """`row.percent` is how far through that EPISODE they got, and the pick it resolves to is the
        whole show. One episode of sixty arrived as `max_percent = 100`, which the report rendered as
        "stops at 100%" and filed under 75%+ — stating as fact that people abandon the show near the
        end when they quit after episode one."""
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
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=7001,
                    show_rating_key=70,
                    media_type="episode",
                    started_at=NOW - timedelta(hours=6),
                    last_seen_at=NOW - timedelta(hours=5),
                    ended_at=NOW - timedelta(hours=5),
                    max_offset_ms=2_700_000,
                    duration_ms=2_700_000,
                    end_reason="stopped",
                )
            )
            s.commit()

        with world() as s:
            started, percent = session_progress(s)[(99, 700, "show")]

        assert started is not None, "the START still counts — the row got them to press play"
        assert percent is None, "how far through the SERIES they are is unknown, not 100%"


class TestTheIngestCannotWedgeItself:
    def test_a_future_dated_play_does_not_park_the_cursor_ahead_of_now(self, sessions):
        """A PMS whose clock was ahead — a NAS booting before NTP — leaves one history row stamped in
        the future. Parking the cursor there means every later read asks for `viewedAt >` a date that
        has not happened: 0 rows for ever, logged as "0 new play(s)", with no UI to reset it."""
        store = MagicMock()
        store.get.return_value = None
        plex = MagicMock()
        plex.play_history.return_value = [
            PlayEvent(99, 10, None, "movie", NOW - timedelta(hours=1), "sane"),
            PlayEvent(99, 11, None, "movie", NOW + timedelta(days=400), "from-the-future"),
        ]

        with sessions() as s:
            ingest_play_history(s, plex, store)
            s.commit()

        parked = datetime.fromisoformat(store.set.call_args.args[1])
        assert parked <= datetime.now(UTC) + timedelta(minutes=1)

    def test_an_event_with_no_history_key_is_not_re_inserted_every_sync(self, sessions):
        """The cursor is deliberately rewound a minute, so overlap is normal — and SQLite allows
        unlimited NULLs in a UNIQUE column, so the constraint does not dedupe these."""
        store = MagicMock()
        store.get.return_value = None
        plex = MagicMock()
        plex.play_history.return_value = [PlayEvent(99, 10, None, "movie", NOW - timedelta(hours=2), None)]

        for _ in range(3):
            with sessions() as s:
                ingest_play_history(s, plex, store)
                s.commit()

        with sessions() as s:
            assert s.query(WatchEvent).count() == 1


class TestWatchHistoryAgesOutOnItsOwnSchedule:
    def test_it_is_pruned_even_when_run_history_is_kept_forever(self, world):
        """`runs.retention = 0` is a supported setting, and these tables grow with every play by every
        account on the server — not with Shortlist's own activity. Behind the run guards they grew
        without bound whenever no run was old enough, retention was off, or runs had been cleared."""
        from shortlist.server.services.run_persistence import prune_runs

        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=1,
                    media_type="movie",
                    viewed_at=NOW - timedelta(days=800),
                    source="history",
                    history_key="ancient",
                )
            )
            s.commit()

        with world() as s:
            prune_runs(s, 0)  # keep run history for ever
            s.commit()

        with world() as s:
            assert s.query(WatchEvent).count() == 0, "watch history has its own ceiling"


class TestSeriesPercentagesAreCleared:
    def test_the_migration_clears_a_series_percentage_and_leaves_films_alone(self, world):
        """`_stamp_percent` never walks a percentage backwards — a guard that is right for its own
        reason (retention shrinks what sessions can report) and which would have preserved these wrong
        values for ever. Both picks carrying a percentage on the maintainer's server were series."""
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
                    max_percent=100,
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    tmdb_id=510,
                    media_type="movie",
                    rating_key=10,
                    rank=1,
                    max_percent=40,
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.commit()
            s.execute(sa.text("UPDATE picks SET max_percent = NULL WHERE media_type = 'show'"))
            s.commit()

        with world() as s:
            assert s.query(PickRow).filter_by(media_type="show").one().max_percent is None
            assert s.query(PickRow).filter_by(media_type="movie").one().max_percent == 40


class TestAPercentageNeverInventsACredit:
    """A percentage is a fact ABOUT a credited watch, never a reason to invent one.

    The concrete case the property test could not reliably generate: the person sampled the film
    before it was ever recommended, so the snapshot path explicitly refuses to credit it — and a bare
    `defaultdict` read on that reject path minted an outcome anyway, which then collected a percentage
    and surfaced as a "dropped" pick dated to today's delivery.
    """

    def test_a_watch_predating_the_row_does_not_come_back_as_a_drop(self, world):
        # An unrelated pick from long ago, so `_attribution_floor` reaches back far enough for the old
        # session below to be in scope at all. Without it the floor is today and the session is simply
        # never read — which is what made an earlier version of this test pass either way.
        pick(world, 1, 999, rating_key=99, created=NOW - timedelta(days=60))
        # The title under test is delivered for the FIRST time today.
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(hours=2))
        # But they sampled it 40 days ago — BEFORE the row ever carried it — and Plex has flagged it
        # watched since. The session is real; it just had nothing to do with any recommendation.
        with world() as s:
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=10,
                    media_type="movie",
                    started_at=NOW - timedelta(days=40),
                    last_seen_at=NOW - timedelta(days=40),
                    ended_at=NOW - timedelta(days=40),
                    max_offset_ms=1_200_000,
                    duration_ms=6_000_000,
                    end_reason="stopped",
                )
            )
            s.commit()
        history = [WatchedItem(title="T", media_type=MediaType.MOVIE, watched_at=NOW - timedelta(days=40), tmdb_id=510)]

        reconcile_watched(world, [profile(history)])

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=510).one()
        assert row.watched_at is None, "the watch predates the row — not a hit"
        assert row.max_percent is None, "and so it cannot be an abandonment of that row either"


class TestEverySittingIsConsidered:
    def test_a_later_sitting_is_credited_even_though_the_first_predated_the_row(self, world):
        """`session_progress` collapses a title to its EARLIEST start — right for reporting a
        percentage, wrong for deciding a credit. Once someone had any session predating the row, the
        title could never be start-credited again, however many times they played it off the row
        afterwards. That is the population this feature exists to measure: a partial watch sets no
        Plex flag, so the engine keeps recommending the title and no history-log row exists either."""
        pick(world, 1, 999, rating_key=99, created=NOW - timedelta(days=60))  # gives the floor reach
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=3))
        with world() as s:
            # Sitting one: 40 days ago, long before the row carried it.
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=10,
                    media_type="movie",
                    started_at=NOW - timedelta(days=40),
                    last_seen_at=NOW - timedelta(days=40),
                    ended_at=NOW - timedelta(days=40),
                    max_offset_ms=1_200_000,
                    duration_ms=6_000_000,
                    end_reason="stopped",
                )
            )
            # Sitting two: yesterday, straight off the row.
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="2",
                    rating_key=10,
                    media_type="movie",
                    started_at=NOW - timedelta(days=1),
                    last_seen_at=NOW - timedelta(days=1),
                    ended_at=NOW - timedelta(days=1),
                    max_offset_ms=3_600_000,
                    duration_ms=6_000_000,
                    end_reason="stopped",
                )
            )
            s.commit()

        with world() as s:
            credits = event_credits(s, RowMembership(s))

        assert (1, 510, "movie") in credits, "the second sitting happened while the row was showing it"


class TestAFilmPlayedToTheEndIsFinished:
    """A film someone watched to the very end was reported as "gave up on it after 100%", under a
    heading reading "where the picks are not landing", from the moment the credits rolled until the
    nightly sync at 04:17 the next morning.

    `reconcile_from_events` — the pass whose whole point is to put the outcome up the moment playback
    stops — passed no `finished_keys`, so only the nightly Plex read could ever stamp `finished_at`.
    Decided in `_decide_outcomes` rather than inferred at read time, so `overall.finished` (which
    counts the stamp) and the engagement detail (which reads the outcome) cannot disagree."""

    def test_a_full_watch_is_finished_not_abandoned(self, world):
        from shortlist.server.services.run_persistence import FINISHED_PERCENT, reconcile_from_events

        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=1))
        session_row(world, 10, started=NOW - timedelta(hours=3), offset=6_000_000, duration=6_000_000)

        reconcile_from_events(world)

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=510).one()
            assert row.max_percent == 100
            assert row.finished_at is not None, "they watched it to the end"
            assert resolve_outcomes(s, None)[(1, 510, "movie")]["outcome"] == "finished"
        assert FINISHED_PERCENT <= 100

    def test_stopping_just_short_is_still_a_drop(self, world):
        """Plex's own watched bar is around 90%, so below it we have no basis to claim a finish."""
        from shortlist.server.services.run_persistence import FINISHED_PERCENT, reconcile_from_events

        pick(world, 2, 511, rating_key=11, created=NOW - timedelta(days=1))
        short = int(6_000_000 * (FINISHED_PERCENT - 5) / 100)
        session_row(world, 11, started=NOW - timedelta(hours=3), offset=short, duration=6_000_000)

        reconcile_from_events(world)

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=511).one()
            assert row.finished_at is None
            assert resolve_outcomes(s, None)[(1, 511, "movie")]["outcome"] == "dropped"

    def test_the_headline_and_the_detail_agree(self, world):
        """Decided once and stored, so the Finished tile (which counts the stamp) and the engagement
        list (which reads the outcome) cannot say different things about the same watch."""
        from shortlist.server.services.report_service import effectiveness
        from shortlist.server.services.run_persistence import reconcile_from_events

        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=1))
        session_row(world, 10, started=NOW - timedelta(hours=3), offset=6_000_000, duration=6_000_000)

        reconcile_from_events(world)

        with world() as s:
            report = effectiveness(s, "all")
            assert report["overall"]["finished"] == 1
            assert report["overall"]["dropped"] + report["overall"]["bounced"] == 0


class TestTheClocksAndTheMaxima:
    """Four guards the mutation sweep found nothing was testing. Each picks one value out of several
    candidates, and each flips silently: the number stays plausible, it is just the wrong one."""

    def test_a_delivery_is_dated_by_its_EARLIEST_pick(self, world):
        """A run persists a row's picks over seconds to minutes. The delivery landed when the FIRST
        one did — dating it by the last means a play in between is judged against a row Plex was not
        yet serving."""
        with world() as s:
            for rank, created in ((1, NOW - timedelta(hours=6)), (2, NOW - timedelta(hours=5))):
                s.add(
                    PickRow(
                        run_id=2,
                        user_id=1,
                        collection_slug="picked",
                        section_key="1",
                        library="Movies",
                        tmdb_id=600 + rank,
                        media_type="movie",
                        rating_key=0,
                        rank=rank,
                        created_at=created,
                    )
                )
            s.commit()

        with world() as s:
            timeline = RowMembership(s)._per_person[(1, "picked", "1")]
        landed = min(at for at, _keys in timeline)
        assert landed == NOW - timedelta(hours=6), "the earliest pick, not the latest"

    def test_a_shared_rows_audience_comes_from_its_NEWEST_delivery(self, world):
        """Audiences change. The one that applies is the one in force when they pressed play — the
        newest delivery at or before that moment, not the oldest on record."""
        with world() as s:
            s.add(Collection(id=2, slug="staff", name="Staff", enabled=True, build="shared"))
            s.add(Delivery(collection_slug="staff", user_slug="shared_staff", library_key="1", rating_key=9))
            s.add(Run(id=3, trigger="schedule", status="ok", started_at=NOW - timedelta(days=3)))
            s.add(Run(id=4, trigger="schedule", status="ok", started_at=NOW - timedelta(days=1)))
            # Old delivery: alex could see it. New delivery: only sam.
            s.add(RunSharedRow(run_id=3, collection_slug="staff", status="ok", picks=[], audience=[99]))
            s.add(RunSharedRow(run_id=4, collection_slug="staff", status="ok", picks=[], audience=[77]))
            s.commit()

        with world() as s:
            alex = s.query(User).filter_by(id=1).one()
            membership = RowMembership(s)
            assert membership._shared_visible_to("staff", alex, NOW) is False, (
                "the NEWEST delivery excluded them; taking the oldest would still say yes"
            )
            assert membership._shared_visible_to("staff", alex, NOW - timedelta(days=2)) is True, (
                "back then the old delivery was in force"
            )

    def test_a_titles_watch_time_is_the_LATEST_of_several(self, world):
        """Plex reports a title once per play. The snapshot path bounds a credit by "no earlier than
        the row first showed it", so taking the earliest of several plays can refuse a credit the
        latest one earns."""
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=2))
        history = [
            WatchedItem(title="T", media_type=MediaType.MOVIE, watched_at=NOW - timedelta(days=5), tmdb_id=510),
            WatchedItem(title="T", media_type=MediaType.MOVIE, watched_at=NOW - timedelta(hours=1), tmdb_id=510),
        ]

        reconcile_watched(world, [profile(history)])

        with world() as s:
            watched = s.query(PickRow).filter_by(tmdb_id=510).one().watched_at
        assert watched is not None, "the later play is after the delivery and earns the credit"
        assert watched.replace(tzinfo=UTC) == NOW - timedelta(hours=1)


class TestTheAttributionFloorIsCorrectnessNotJustSpeed:
    """`_attribution_floor` bounds every scan in the credit path, and its docstring leads with the
    performance case — a table with no ceiling, re-read six times a day.

    A verification pass on 2026-08-25 found that framing dangerously incomplete: an earlier audit
    dismissed all four floor filters as "pure guard-clause optimisations", and every one of them
    changes what gets CREDITED. Nothing tested that, so the filters could have been removed as dead
    weight by anyone who believed the comment.

    The reason they bite is a mismatch nobody had written down: the floor is
    `min(PickRow.created_at)`, but a SHARED row's delivery time is `RunSharedRow.delivered_at` — not
    a pick row at all. So membership does not independently reject every pre-floor play, and the
    floor is the only thing standing between the credit path and the whole event log.
    """

    def test_a_sitting_before_the_floor_cannot_set_a_percentage(self, world):
        """The sharpest one. `session_progress` returns the MAX percentage across all sittings, so
        widening its scan lets an ancient sitting decide a recent pick's fate — and at 95% that is
        past `FINISHED_PERCENT`, flipping an abandonment into "they finished it"."""
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=2))
        # Long before the floor: 95% of the runtime.
        session_row(world, 10, started=NOW - timedelta(days=40), offset=5_700_000)
        # After it: they gave up at 10%.
        session_row(world, 10, started=NOW - timedelta(hours=6), offset=600_000)

        with world() as s:
            progress = session_progress(s, _attribution_floor(s), tmdb_by_rating_key(s))

        assert progress, "the recent sitting should be measured"
        _started, percent = next(iter(progress.values()))
        assert percent == 10, f"a pre-floor sitting decided this pick's percentage ({percent}%)"

    def test_a_play_before_the_floor_is_not_scanned(self, world):
        """`_scan_plays` feeds both the credit pass and `observed`, the set `_withdraw_unwatched`
        refuses to touch. Widening it therefore also suppresses withdrawals."""
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=2))
        with world() as s:
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type="movie",
                    viewed_at=NOW - timedelta(days=40),  # before the floor
                    source="history",
                    history_key="ancient",
                )
            )
            s.commit()

        with world() as s:
            scanned = _scan_plays(s, tmdb_by_rating_key(s))

        assert scanned == [], "a play from before the first pick was scanned as creditable"

    def test_a_session_before_the_floor_is_not_a_start(self, world):
        """The same boundary on the session path, which is the other half of what `_scan_plays`
        unions together."""
        pick(world, 2, 510, rating_key=10, created=NOW - timedelta(days=2))
        session_row(world, 10, started=NOW - timedelta(days=40), offset=1_800_000)

        with world() as s:
            starts = _session_starts(s, _attribution_floor(s), tmdb_by_rating_key(s))

        assert starts == [], "a sitting from before the first pick counted as a start"
