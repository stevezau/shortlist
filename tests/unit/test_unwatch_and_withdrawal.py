"""A credit can be taken back — un-watching, resuming, and the percentage that outlives neither.

Plex's watched flag is a toggle a person can flip off, and a partial watch has no flag at all. So a
credit is not a one-way write: `_withdraw_unwatched` removes the ones the evidence no longer
supports, and every test here is about what it must NOT touch — settled history, a percentage we
watched happen, and a credit written after the read that would have justified it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.server.db.models import (
    Base,
    Collection,
    Delivery,
    PickRow,
    Run,
    RunSharedRow,
    SharedRowWatch,
    User,
    WatchSession,
)
from shortlist.server.services.report_service import (
    resolve_outcomes,
)
from shortlist.server.services.run_persistence import reconcile_watched

# The real clock, deliberately not a pinned date. Every fixture here places its data RELATIVE to
# this instant, and the code under test reads `datetime.now(UTC)` — so a pinned NOW is a second clock
# that drifts away from the first one day at a time. The withdrawal boundary tests sit ±5 days from
# `UNWATCH_WITHDRAW_DAYS`, so they began failing 5 days after the date last pinned here, reporting a
# bug in code nobody had touched. Nothing in this file needs a fixed calendar date; it needs the
# same "now" the SUT sees.
NOW = datetime.now(UTC)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


@pytest.fixture
def world(sessions):
    """One shared row, delivered yesterday, carrying one film. Two people can see it."""
    with sessions() as s:
        s.add(User(id=1, plex_account_id=99, username="alex", slug="alex"))
        s.add(User(id=2, plex_account_id=77, username="sam", slug="sam"))
        s.add(Collection(id=1, slug="staff", name="Staff Picks", enabled=True, build="shared"))
        s.add(Run(id=1, trigger="schedule", status="ok", started_at=NOW - timedelta(days=1)))
        # A shared row files its delivery under `shared_<slug>` — that is the on-Plex gate.
        s.add(Delivery(collection_slug="staff", user_slug="shared_staff", library_key="1", rating_key=500))
        s.add(
            RunSharedRow(
                run_id=1,
                collection_slug="staff",
                row_title="Staff Picks",
                status="ok",
                picks=[{"tmdb_id": 550, "media_type": "movie", "title": "Fight Club"}],
                audience=None,
            )
        )
        s.commit()
    return sessions


def watch_session(sessions, account, *, started, offset, rating_key=9001, duration=6_000_000):
    with sessions() as s:
        s.add(
            WatchSession(
                plex_account_id=account,
                session_key=f"{account}-{started.timestamp()}",
                rating_key=rating_key,
                media_type="movie",
                started_at=started,
                last_seen_at=started + timedelta(minutes=10),
                ended_at=started + timedelta(minutes=10),
                max_offset_ms=offset,
                duration_ms=duration,
                end_reason="stopped",
            )
        )
        s.commit()


def a_pick_so_the_rating_key_resolves(sessions, *, tmdb_id=550, rating_key=9001):
    """`tmdb_by_rating_key` is built from `picks`, so a session's rating key needs SOME pick row to
    become a tmdb id. Belongs to the OTHER person, on no row this test is about."""
    with sessions() as s:
        s.add(
            PickRow(
                run_id=1,
                user_id=2,
                collection_slug="other",
                section_key="1",
                library="Movies",
                tmdb_id=tmdb_id,
                media_type="movie",
                rating_key=rating_key,
                rank=1,
                created_at=NOW - timedelta(days=1),
            )
        )
        s.commit()


def profile(history=(), *, slug="alex", account=99, complete=True):
    """`complete=True` by default: these tests are about what withdrawal decides once it is allowed
    to run at all. Pass False to model a read that could not prove it saw everything."""
    return UserProfile(
        username=slug,
        plex_account_id=account,
        user_type=UserType.SHARED,
        slug=slug,
        history=list(history),
        history_complete=complete,
    )


class TestTheWithdrawalLogNamesWhatItTook:
    """`watched_at`/`finished_at` have no other copy, so the log line is the only forensic trail if
    withdrawal ever takes back something it should not have.

    Live on a real server the first version read `Rabbit Hole, Rabbit Hole, Rabbit Hole, ...` eight
    times — one entry per pick ROW, because a title is delivered by many runs. The count is
    row-based, because that is what was written; the names are distinct, because that is what a
    person reads.
    """

    def test_one_title_delivered_by_many_runs_is_named_once(self, world):
        from shortlist.server.services.run_persistence import _withdraw_unwatched

        with world() as session:
            for rank in range(1, 4):  # the same show, credited by three different runs
                session.add(
                    PickRow(
                        run_id=1,
                        user_id=1,
                        collection_slug="staff",
                        section_key="1",
                        library="Movies",
                        tmdb_id=610,
                        media_type="movie",
                        rating_key=0,
                        rank=rank,
                        title="Rabbit Hole",
                        created_at=NOW - timedelta(days=2),
                        watched_at=NOW - timedelta(days=1),
                    )
                )
            session.commit()
        with world() as session:
            user = session.query(User).filter_by(id=1).one()
            gone = _withdraw_unwatched(session, user, {}, set(), now=NOW)

        assert len(gone) == 3, "the count must stay row-based — that is what was actually written"
        assert sorted(set(gone)) == ["Rabbit Hole"], "the log would repeat the same title once per row"


class TestUnwatchingWithdrawsOnlyAFlagBackedCredit:
    """Someone can un-watch a title, and Plex marks things watched wrongly often enough that
    correcting it is normal housekeeping. Nothing withdrew a credit, so one bad flag counted toward
    the hit rate for ever and the headline number could only ever drift upward.

    But a credit we WATCHED HAPPEN is a fact about a moment, not a mirror of a checkbox — and a
    partial watch never sets the flag at all, so withdrawing on absence alone would delete the exact
    signal this whole feature exists to capture."""

    def a_credited_pick(self, world, *, tmdb, rating_key=0, title="T"):
        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=tmdb,
                    media_type="movie",
                    rating_key=rating_key,
                    rank=1,
                    title=title,
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(days=1),
                )
            )
            s.commit()

    def sync(self, world, history, *, full):
        reconcile_watched(world, [profile(history, complete=full)])

    def test_a_flag_only_credit_is_withdrawn_when_the_flag_goes(self, world):
        self.a_credited_pick(world, tmdb=510)
        # Their history no longer contains it, but DOES contain something — a real read that came back.
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        self.sync(world, other, full=True)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).one().watched_at is None

    def test_a_credit_we_watched_happen_survives(self, world):
        """The partial watch. It never sets Plex's flag, so it is absent from every history read —
        withdrawing on absence would delete it the moment after it was credited."""
        self.a_credited_pick(world, tmdb=550, rating_key=9001, title="Fight Club")
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        self.sync(world, other, full=True)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=550).one().watched_at is not None

    def test_a_caller_that_cannot_prove_a_complete_read_withdraws_nothing(self, world):
        """Absence is only evidence when the read can prove it saw everything.

        The caller that cannot is not hypothetical: a run passes profiles it filled in place, and a
        watch sync whose read fail-softed past an unreadable library returns that library's titles as
        absent. Believing either would withdraw credit for something the person still has watched,
        and `watched_at`/`finished_at` have no other copy."""
        self.a_credited_pick(world, tmdb=510)
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        self.sync(world, other, full=False)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).one().watched_at is not None

    def test_an_empty_history_withdraws_nothing(self, world):
        """A read that failed and a person who has watched nothing are indistinguishable here, and
        wrongly wiping real history is far worse than one stale credit."""
        self.a_credited_pick(world, tmdb=510)

        self.sync(world, [], full=True)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).one().watched_at is not None

    def test_a_title_still_watched_is_untouched(self, world):
        self.a_credited_pick(world, tmdb=510)
        still = [WatchedItem(title="T", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=510)]

        self.sync(world, still, full=True)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).one().watched_at is not None

    def test_withdrawing_clears_the_completion_too(self, world):
        """A finish is a stronger claim than a watch. Leaving it behind would give a pick that is
        finished but not watched — which the report reads as a segment wider than its own bar."""
        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=510,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="T",
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(days=1),
                    finished_at=NOW - timedelta(hours=12),
                )
            )
            s.commit()
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        self.sync(world, other, full=True)

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=510).one()
            assert row.watched_at is None and row.finished_at is None


class TestSettledHistoryIsNeverErased:
    """A title is missing from a watched-titles read for two very different reasons: the person
    un-watched it, or it is no longer in the library. Nothing distinguishes them — the read is
    "everything in this section with the watched flag set", and a deleted file is in no section.

    Unbounded, the withdrawal would erase a year of hit-rate history the first time the owner tidied
    up their movies folder — silently, on the weekly pass."""

    def a_credit_aged(self, world, *, tmdb, days):
        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=tmdb,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="T",
                    created_at=NOW - timedelta(days=days + 1),
                    watched_at=NOW - timedelta(days=days),
                )
            )
            s.commit()

    def test_a_settled_credit_survives_a_title_leaving_the_library(self, world):
        from shortlist.server.services.run_persistence import UNWATCH_WITHDRAW_DAYS

        self.a_credit_aged(world, tmdb=510, days=UNWATCH_WITHDRAW_DAYS + 5)
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        reconcile_watched(world, [profile(other)])

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).one().watched_at is not None

    def test_a_recent_credit_is_still_withdrawn(self, world):
        """The case this exists for: Plex flags something watched wrongly and the person corrects it,
        which happens within days."""
        from shortlist.server.services.run_persistence import UNWATCH_WITHDRAW_DAYS

        self.a_credit_aged(world, tmdb=511, days=UNWATCH_WITHDRAW_DAYS - 5)
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        reconcile_watched(world, [profile(other)])

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=511).one().watched_at is None


class TestAWithdrawnCreditLeavesNothingBehind:
    def test_a_pick_carrying_a_percentage_is_not_withdrawn_at_all(self, world):
        """This once asserted that withdrawal CLEARED the percentage. The rule got stronger instead:
        a percentage is playback we watched happen, recorded on the pick itself rather than derived
        from a snapshot that can be stale — so such a pick is never withdrawn in the first place, and
        there is nothing left to clear.

        What still protects against a percentage outliving its credit (the credited row deleted and
        its history cleared) is `resolve_outcomes` refusing to call one an outcome, covered by
        `TestClearingHistoryLeavesNoOrphanedPercentage`."""
        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=510,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="T",
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(days=1),
                    max_percent=42,
                )
            )
            s.commit()
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        reconcile_watched(world, [profile(other)])

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=510).one()
            assert row.watched_at is not None, "a percentage is playback we watched happen"
            assert row.max_percent == 42


class TestUnwatchingAndSharedRows:
    """Withdrawal never touches `shared_row_watches`, and that is correct rather than an oversight —
    but it is only correct because of a property of `shared_credits` that nothing pinned."""

    def test_a_shared_credit_always_has_playback_behind_it(self, world):
        """`shared_credits` has no snapshot path: it reads `_scan_plays` and nothing else. So every
        shared credit is one we WATCHED HAPPEN, which is exactly the class `_withdraw_unwatched`
        refuses to take back. If a snapshot path were ever added here, shared credits would become
        withdrawable and this would need revisiting."""
        a_pick_so_the_rating_key_resolves(world)
        # Plex says watched, but there is no session and no play-log entry.
        history = [WatchedItem(title="Fight Club", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=550)]

        reconcile_watched(world, [profile(history)])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0, (
                "a flag alone must never credit a shared row — everyone can see one, so a merely "
                "popular title would credit for everybody"
            )

    def test_un_watching_leaves_a_shared_credit_that_was_really_played(self, world):
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])
        with world() as s:
            assert s.query(SharedRowWatch).count() == 1

        # They un-watch it: it is absent from a full history read.
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]
        reconcile_watched(world, [profile(other)])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 1, "we saw them press play; that still happened"


class TestClearingHistoryLeavesNoOrphanedPercentage:
    """`_apply_outcomes` stamps `max_percent` onto EVERY delivery row of a title with no bound at
    all — safe only while some other row carries the credit. Delete the credited row and clear its
    history, and the percentage outlives it: an abandonment with no start, and a Bounced tile
    reading higher than the Watched tile above it."""

    def test_a_percentage_with_no_credit_is_not_an_outcome(self, world):
        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            # The row that earned the credit has been deleted and its history cleared; this is the
            # sibling delivery that kept the percentage.
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=550,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="Fight Club",
                    created_at=NOW - timedelta(days=1),
                    watched_at=None,
                    max_percent=3,
                )
            )
            s.commit()

        with world() as s:
            assert resolve_outcomes(s, None) == {}, "no credit, no outcome"

    def test_the_tiles_cannot_invert(self, world):
        from shortlist.server.services.report_service import _watched_count

        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=550,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="Fight Club",
                    created_at=NOW - timedelta(days=1),
                    watched_at=None,
                    max_percent=3,
                )
            )
            s.commit()

        with world() as s:
            abandoned = sum(1 for e in resolve_outcomes(s, None).values() if e["outcome"] in ("bounced", "dropped"))
            assert abandoned <= _watched_count(s, None)


class TestTheMonotonicPercentageGuard:
    """Retention deletes sessions at six months, so the maximum a session can report SHRINKS over
    time. A fresh short sitting must never overwrite a real earlier one — the mutation sweep found
    nothing testing either half of that, personal or shared."""

    def test_a_later_shorter_session_does_not_lower_a_personal_percentage(self, world):
        from shortlist.server.services.run_persistence import reconcile_from_events

        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=550,
                    media_type="movie",
                    rating_key=9001,
                    rank=1,
                    title="Fight Club",
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(days=1),
                    max_percent=62,
                )
            )
            s.commit()
        # A brief re-open today: 5%.
        watch_session(world, 99, started=NOW - timedelta(hours=1), offset=300_000)

        reconcile_from_events(world)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=550).one().max_percent == 62

    def test_a_later_shorter_session_does_not_lower_a_shared_percentage(self, world):
        from shortlist.server.services.run_persistence import reconcile_from_events

        a_pick_so_the_rating_key_resolves(world)
        with world() as s:
            s.add(
                SharedRowWatch(
                    user_id=1,
                    collection_slug="staff",
                    tmdb_id=550,
                    media_type="movie",
                    title="Fight Club",
                    watched_at=NOW - timedelta(days=1),
                    max_percent=62,
                )
            )
            s.commit()
        watch_session(world, 99, started=NOW - timedelta(hours=1), offset=300_000)

        reconcile_from_events(world)

        with world() as s:
            assert s.query(SharedRowWatch).one().max_percent == 62


class TestALiveCreditIsNotWithdrawnByAResyncThatMissedIt:
    """`observed` is built once at the top of `reconcile_watched`; `_withdraw_unwatched` runs per
    user, much later. A partial watch sets no Plex flag and writes no history-log row, so a credit
    that commits inside that window looks unjustified — and its percentage went with it.

    It healed on the next session end, but a partial watch is the one signal with no other source,
    so it should not need healing."""

    def test_a_percentage_is_evidence_even_when_the_snapshot_missed_the_session(self, world):
        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            # Credited with a percentage, but no session or event row exists for it — exactly the
            # state a live credit leaves when the resync read the tables a moment earlier.
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=510,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="T",
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(hours=1),
                    max_percent=42,
                )
            )
            s.commit()
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        reconcile_watched(world, [profile(other)])

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=510).one()
            assert row.watched_at is not None, "a percentage is playback we watched happen"
            assert row.max_percent == 42

    def test_a_flag_only_credit_with_no_percentage_is_still_withdrawn(self, world):
        """The guard must not become a blanket amnesty — a credit Plex's flag alone justified still
        goes when the flag does."""
        with world() as s:
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=511,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="T",
                    created_at=NOW - timedelta(days=2),
                    watched_at=NOW - timedelta(hours=1),
                )
            )
            s.commit()
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        reconcile_watched(world, [profile(other)])

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=511).one().watched_at is None
