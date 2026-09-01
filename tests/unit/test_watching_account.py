"""shortlist/server/services/watching_account.py — moving the owner's watching to a second account.

The load-bearing behaviour here is the DATE. Plex stamps a scrobble `now` and cannot be told
otherwise, so a transfer that only wrote `viewed_at` would hand the new account a history where
everything was watched today — and since seeds come off the most-recent end of that list, its
recommendations would be an arbitrary sample. `source_viewed_at` is what stops that, so most of
these tests are about it surviving.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shortlist.engine.clients.plex_pms import PlayEvent, WatchedRead
from shortlist.engine.models import UserType
from shortlist.engine.watch_replica import ItemState, OpKind, WatchState
from shortlist.server.db.models import User, WatchedTitle, WatchEvent, WatchStateSnapshot, utcnow
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.watch_cache import WatchCache
from shortlist.server.services.watching_account import (
    candidate_home_users,
    stamp_true_dates,
    take_snapshot,
    transfer_watch_history,
    undo_transfer,
)

OLD = datetime(2024, 3, 1, tzinfo=UTC)
NEWER = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def session(sessions):
    with sessions() as db:
        db.add(User(id=1, plex_account_id=10, username="owner", slug="owner", user_type="owner"))
        db.add(User(id=2, plex_account_id=20, username="steve", slug="steve", user_type="managed"))
        db.commit()
        yield db


def watched(session, user_id: int, title: str, *, key: int, viewed_at: datetime, source: datetime | None = None):
    session.add(
        WatchedTitle(
            user_id=user_id,
            section_key="1",
            rating_key=key,
            tmdb_id=key,
            media_type="movie",
            title=title,
            year=2020,
            watch_count=1,
            viewed_at=viewed_at,
            source_viewed_at=source,
        )
    )
    session.commit()


class TestTransferredRowsSurviveTheSync:
    """The cache half: a replicated row must keep its true date through every later read.

    Plex reports every replicated title as watched TODAY — it accepts no date on any write — so
    without `source_viewed_at` the first sync after a transfer flattens the whole history into one
    timestamp, and seeds (taken off the most-recent end) become an arbitrary sample.
    """

    def test_recency_order_follows_the_true_dates_not_the_scrobble_dates(self, session):
        """The consequence that matters: seeds come off the front of `watched_set`, so a transferred
        history must rank by when the person actually watched — not by when the scrobbles landed.

        The two dates are deliberately INVERTED here. Ordering on `viewed_at` gives exactly the
        wrong answer, which is what makes this test able to fail at all: a real scrobbling transfer
        writes near-identical `viewed_at` values, so a sort over those can come out right by luck
        and hide the bug (it did, the first time this was written).
        """
        watched(session, 2, "Recent Film", key=101, viewed_at=OLD, source=NEWER)
        watched(session, 2, "Old Film", key=100, viewed_at=NEWER, source=OLD)

        assert [i.title for i in WatchCache(lambda: session).watched_set(session, 2)] == [
            "Recent Film",
            "Old Film",
        ]

    def test_the_engine_reads_the_true_date_off_a_transferred_watch(self, session):
        """Ordering is only half of it — the recency WINDOWS ("watched in the last N days") read the
        date off each item, so `_to_item` has to hand back the true one too."""
        watched(session, 2, "Dune", key=100, viewed_at=NEWER, source=OLD)

        assert WatchCache(lambda: session).watched_set(session, 2)[0].watched_at == OLD

    def test_a_full_sync_does_not_erase_a_scrobbled_transfer(self, session):
        """The FULL read replaces a section wholesale (DELETE + re-insert). A transferred row must
        survive that and keep its true date — otherwise the very first sync after a transfer undoes
        it, because `needs_full` is True for a brand-new watching account.

        This runs the real `sync_section`, not `_upsert`. An earlier version of this test called
        `_upsert` directly — the one code path where the claim held — and so was blind to the DELETE
        sitting above it.
        """
        from shortlist.engine.models import MediaType, UserProfile, WatchedItem

        watched(session, 2, "Dune", key=100, viewed_at=utcnow(), source=OLD)
        cache = WatchCache(lambda: session)
        profile = UserProfile(username="steve", plex_account_id=20, user_type=UserType.MANAGED, slug="steve")

        # Plex reports it (it was scrobbled), stamped today — as the PMS always will.
        cache.sync_section(
            session,
            profile,
            2,
            "1",
            MediaType.MOVIE,
            lambda since: [
                WatchedItem(
                    title="Dune",
                    media_type=MediaType.MOVIE,
                    watched_at=utcnow(),
                    tmdb_id=100,
                    rating_key=100,
                )
            ],
            force_full=True,
        )
        session.commit()

        row = session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one()
        assert row.source_viewed_at.replace(tzinfo=UTC) == OLD

    def test_a_full_sync_does_not_erase_a_transfer_plex_knows_nothing_about(self, session):
        """The DEFAULT transfer does not scrobble, so the PMS has never heard of these rows and the
        full read returns nothing for them. A blind replace deletes the whole transfer, and the only
        symptom is the new account's picks quietly reverting to cold-start."""
        from shortlist.engine.models import MediaType, UserProfile

        watched(session, 2, "Dune", key=100, viewed_at=OLD, source=OLD)
        watched(session, 2, "Arrival", key=101, viewed_at=OLD, source=NEWER)
        cache = WatchCache(lambda: session)
        profile = UserProfile(username="steve", plex_account_id=20, user_type=UserType.MANAGED, slug="steve")

        cache.sync_section(
            session,
            profile,
            2,
            "1",
            MediaType.MOVIE,
            lambda since: WatchedRead(items=[], covers_window=True),
            force_full=True,
            reconcile=True,
        )
        session.commit()

        assert {r.title for r in session.query(WatchedTitle).filter(WatchedTitle.user_id == 2)} == {
            "Dune",
            "Arrival",
        }

    def test_a_full_sync_still_drops_the_users_own_un_watched_titles(self, session):
        """The exemption must not blunt the thing the full read exists for: a row Plex no longer
        reports, and that no transfer put there, is still an un-watch and still goes."""
        from shortlist.engine.models import MediaType, UserProfile

        watched(session, 2, "Ordinary Watch", key=100, viewed_at=OLD)  # source_viewed_at stays NULL
        cache = WatchCache(lambda: session)
        profile = UserProfile(username="steve", plex_account_id=20, user_type=UserType.MANAGED, slug="steve")

        cache.sync_section(
            session,
            profile,
            2,
            "1",
            MediaType.MOVIE,
            lambda since: WatchedRead(items=[], covers_window=True),
            force_full=True,
            reconcile=True,
        )
        session.commit()

        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).count() == 0

    def test_an_incremental_read_does_not_treat_a_transfer_as_an_un_watch(self, session):
        """`_drop_vanished_since` deletes cached rows the window should have returned. A transferred
        row whose TRUE date falls inside that window is never returned by Plex, so without the
        exemption the transfer is eaten piecemeal, one nightly sync at a time."""
        from shortlist.engine.clients.plex_pms import WatchedRead
        from shortlist.engine.models import MediaType, UserProfile

        recent = utcnow() - timedelta(hours=1)
        watched(session, 2, "Transferred", key=100, viewed_at=recent, source=recent)
        cache = WatchCache(lambda: session)
        profile = UserProfile(username="steve", plex_account_id=20, user_type=UserType.MANAGED, slug="steve")
        # Seed a cursor so the next read is incremental, and prove the window was covered.
        cache.sync_section(
            session,
            profile,
            2,
            "1",
            MediaType.MOVIE,
            lambda since: WatchedRead(items=[], covers_window=True),
            force_full=True,
            reconcile=True,
        )
        session.commit()

        cache.sync_section(
            session,
            profile,
            2,
            "1",
            MediaType.MOVIE,
            lambda since: WatchedRead(items=[], covers_window=True),
        )
        session.commit()

        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).count() == 1

    def test_the_watch_sync_never_overwrites_the_true_date(self, session):
        """`_apply` writes every column Plex reports — and must leave this one alone, or the first
        nightly sync after a transfer undoes it."""
        from shortlist.engine.models import MediaType, WatchedItem
        from shortlist.server.services.watch_cache import _upsert

        watched(session, 2, "Dune", key=100, viewed_at=datetime.now(UTC), source=OLD)

        _upsert(
            session,
            2,
            "1",
            MediaType.MOVIE,
            WatchedItem(
                title="Dune",
                media_type=MediaType.MOVIE,
                watched_at=datetime.now(UTC) + timedelta(days=1),
                tmdb_id=100,
                rating_key=100,
            ),
        )
        session.commit()

        assert (
            session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one().source_viewed_at.replace(tzinfo=UTC)
            == OLD
        )


class TestCandidateHomeUsers:
    def test_lists_home_users_and_flags_the_ones_that_cannot_be_used(self, session):
        plextv = MagicMock()
        plextv.home_users.return_value = [
            {"id": 10, "title": "Owner", "admin": True},
            {"id": 20, "title": "Steve"},
            {"id": 30, "title": "Kids", "protected": True},
        ]

        out = candidate_home_users(plextv, session)

        # The admin account is the thing being escaped FROM, so it is never a candidate.
        assert [c["plex_account_id"] for c in out] == [20, 30]
        # PIN-protected: `canary_server_token` cannot switch to it, so a scrobbling transfer can't
        # mint the token it needs. Listed with the reason rather than silently dropped.
        assert [c["protected"] for c in out] == [False, True]

    def test_flags_an_account_that_already_has_its_own_row(self, session):
        """Transferring onto someone who already has a Picked-for-You merges two people's taste."""
        with session.no_autoflush:
            session.query(User).filter(User.id == 2).update({"enabled": True})
            session.commit()
        plextv = MagicMock()
        plextv.home_users.return_value = [{"id": 20, "title": "Steve"}, {"id": 40, "title": "Spare"}]

        by_id = {c["plex_account_id"]: c for c in candidate_home_users(plextv, session)}

        assert by_id[20]["already_a_shortlist_user"] is True
        assert by_id[40]["already_a_shortlist_user"] is False


class FakeSection:
    def __init__(self, key: str, type_: str) -> None:
        self.key = key
        self.type = type_


class FakePlex:
    """A PMS that behaves the way the real one was measured to behave.

    Deliberately no easier than the server (testing rules): a scrobble ADDS one rather than setting a
    total, an un-scrobble zeroes the count AND the offset (they are one call — `/:/unscrobble`), and a
    read returns only leaves. Getting any of those wrong here hides the bug it models — this fake used
    to zero only one field per op, which made `test_it_puts_the_account_back_exactly` pass for the
    wrong reason and hid an undo that silently dropped view offsets.
    """

    def __init__(self, accounts: dict[str, dict[int, ItemState]] | None = None, history=()) -> None:
        self.state: dict[str, dict[int, ItemState]] = accounts or {}
        self.token = "ADMIN"
        self._history = list(history)
        self.writes: list[tuple] = []

    def sections(self, types=("movie", "show")):
        return [FakeSection("1", "movie"), FakeSection("2", "show")]

    def read_watch_state(self, sections, token):
        return WatchState(items=dict(self.state.get(token, {})))

    def play_history(self, since=None):
        return list(self._history)

    def unscrobble_as(self, rating_key, token, *, dry_run=False):
        """The show-key clear. Same call the real client makes, recorded like any other write."""
        self.writes.append((OpKind.UNMARK, rating_key, 0, 0, dry_run))
        if dry_run:
            return True
        account = self.state.setdefault(token, {})
        for leaf_state in self.episodes_of(rating_key):
            account.pop(leaf_state, None)
        account.pop(rating_key, None)
        return True

    def episodes_of(self, show_rating_key):
        return [k for k, v in self.state.get("TARGET", {}).items() if v.show_rating_key == show_rating_key]

    def apply_watch_op(self, op, token, *, dry_run=False):
        self.writes.append((op.kind, op.rating_key, op.scrobbles, op.offset_ms, dry_run))
        if dry_run:
            return True
        account = self.state.setdefault(token, {})
        current = account.get(
            op.rating_key,
            ItemState(rating_key=op.rating_key, media_type=op.media_type, show_rating_key=op.show_rating_key),
        )
        if op.kind is OpKind.MARK:
            # ADDS, and CLEARS any existing offset — both measured on a real server. A scrobble
            # cannot set a total (hence `scrobbles`), and it wipes the position (hence the
            # reposition the planner emits after every mark).
            current = replace(current, view_count=current.view_count + max(1, op.scrobbles), view_offset_ms=0)
        elif op.kind is OpKind.SET_OFFSET:
            current = replace(current, view_offset_ms=op.offset_ms)
        else:
            # UNMARK and CLEAR_OFFSET are the SAME call and it zeroes both fields. Modelling them as
            # touching one field each let a plan that reset an item keep an offset it had really lost.
            current = replace(current, view_count=0, view_offset_ms=0)
        if current.view_count or current.view_offset_ms:
            account[op.rating_key] = current
        else:
            account.pop(op.rating_key, None)
        return True


def leaf(key: int, *, count=0, offset=0, at=0, show=None, kind="movie", title="") -> ItemState:
    return ItemState(
        rating_key=key,
        media_type=kind,
        view_count=count,
        view_offset_ms=offset,
        last_viewed_at=at,
        show_rating_key=show,
        title=title or f"title {key}",
    )


def replicate(session, plex, **kw):
    """The service under test, with a REAL session factory.

    The factory is not decoration: the snapshot has to commit in its own transaction before the first
    Plex write, so passing a stub here would test a path production does not take.
    """
    return transfer_watch_history(
        session,
        sessions=_factory_for(session),
        from_user_id=1,
        to_user_id=2,
        plex=plex,
        source_token="ADMIN",
        target_token="TARGET",
        **kw,
    )


def _factory_for(session):
    """A REAL factory over the same database file, so the snapshot commits independently.

    Handing back the caller's own session was the easy version and it tested nothing: `take_snapshot`
    committing the caller's transaction would have passed identically, which is precisely the bug the
    parameter exists to prevent. The "it would deadlock" justification was also wrong — measured, a
    separate connection committing while the caller holds an open read completes in about a
    millisecond, because WAL plus `busy_timeout` (`db/session.py`) is what makes it safe.
    """
    from shortlist.server.db.session import make_session_factory

    return make_session_factory(session.get_bind())


class TestReplicatingWatchState:
    def test_a_partly_watched_show_is_replicated_episode_by_episode(self, session):
        """The One Piece case, end to end. Two of a hundred episodes watched must land as two
        episodes — the old code scrobbled the SHOW key and marked all hundred."""
        plex = FakePlex(
            {"ADMIN": {11: leaf(11, count=1, show=9, kind="episode"), 12: leaf(12, count=1, show=9, kind="episode")}}
        )

        report = replicate(session, plex)
        session.commit()

        assert {rating_key for _, rating_key, _, _, _ in plex.writes} == {11, 12}
        assert report.marks == 2
        assert plex.state["TARGET"][11].view_count == 1

    def test_a_rewatched_title_lands_on_the_exact_count(self, session):
        """A scrobble adds one, so the shortfall — not the total — is what gets sent. Sending the
        total against a film already watched once lands on four and climbs on every re-run."""
        plex = FakePlex({"ADMIN": {100: leaf(100, count=3)}, "TARGET": {100: leaf(100, count=1)}})

        replicate(session, plex)

        assert plex.state["TARGET"][100].view_count == 3

    def test_a_film_started_and_never_finished_keeps_its_position(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, offset=490_509)}})

        replicate(session, plex)

        assert plex.state["TARGET"][100].view_offset_ms == 490_509
        assert plex.state["TARGET"][100].view_count == 0

    def test_a_watch_the_owner_does_not_have_is_removed(self, session):
        """Mirroring. Add-only would leave it, and the result would not be a replica."""
        plex = FakePlex({"ADMIN": {}, "TARGET": {77: leaf(77, count=1)}})

        report = replicate(session, plex)

        assert report.unmarks == 1
        assert 77 not in plex.state["TARGET"]

    def test_it_repairs_an_account_the_old_show_key_transfer_spoiled(self, session):
        """The reason mirroring is not optional. The old transfer marked all 100 episodes from one
        write; only two were really watched. Add-only changes nothing at all here."""
        source = {k: leaf(k, count=1, show=9, kind="episode") for k in (11, 12)}
        spoiled = {k: leaf(k, count=1, show=9, kind="episode") for k in range(11, 111)}
        plex = FakePlex({"ADMIN": source, "TARGET": spoiled})

        report = replicate(session, plex)

        assert report.unmarks == 98
        assert set(plex.state["TARGET"]) == {11, 12}

    def test_a_second_run_asks_for_nothing(self, session):
        """The fixed point. A count that climbed, or an offset rewritten on rounding, would show up
        here as a plan that never empties."""
        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=2, offset=5_000), 11: leaf(11, count=1, show=9, kind="episode")}}
        )
        replicate(session, plex)
        session.commit()

        assert replicate(session, plex).planned == 0


class TestTheSnapshot:
    def test_the_snapshot_is_taken_before_any_write(self, session):
        """Rule 2. Taken afterwards it would record OUR changes as the account's own state, which is
        worse than none — the undo would restore the damage."""
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=2, offset=900)}})

        report = replicate(session, plex)
        session.commit()

        snapshot = session.get(WatchStateSnapshot, report.snapshot_id)
        assert snapshot.state == [[77, 2, 900, "movie", None]]

    def test_the_snapshot_keeps_counts_and_offsets_not_just_watched(self, session):
        """Restoring a rewatched film as watched-once, or a part-watched episode as finished, gives a
        third state that existed on neither account — a failure that looks like success."""
        plex = FakePlex({"ADMIN": {}, "TARGET": {77: leaf(77, count=4, offset=1234)}})

        report = replicate(session, plex)
        session.commit()

        assert session.get(WatchStateSnapshot, report.snapshot_id).state == [[77, 4, 1234, "movie", None]]

    def test_a_dry_run_takes_no_snapshot_because_it_changes_nothing(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=1)}})

        report = replicate(session, plex, dry_run=True)
        session.commit()

        assert report.snapshot_id is None
        assert session.query(WatchStateSnapshot).count() == 0


class TestDryRun:
    def test_it_plans_everything_and_writes_nothing(self, session):
        """Rule 8."""
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=1)}})

        report = replicate(session, plex, dry_run=True)

        assert (report.planned, report.marks, report.unmarks) == (2, 1, 1)
        assert all(dry for *_, dry in plex.writes)
        assert plex.state["TARGET"] == {77: leaf(77, count=1)}

    def test_it_names_the_titles_it_would_remove(self, session):
        """A count is not something anyone can check, and this is the only destructive path here."""
        plex = FakePlex({"ADMIN": {}, "TARGET": {77: leaf(77, count=1, title="Jaws")}})

        assert replicate(session, plex, dry_run=True).removals_preview == ["Jaws"]


class TestVerify:
    def test_a_write_that_did_not_stick_is_reported_as_mismatched(self, session):
        """The old transfer reported counts it never checked. A write the PMS ACCEPTS is not a write
        that took effect, so the verify pass re-reads rather than trusting the write results."""

        class Deaf(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                return True  # accepted, and silently did nothing

        plex = Deaf({"ADMIN": {100: leaf(100, count=1)}})

        assert replicate(session, plex).verify_mismatched == 1

    def test_a_clean_run_reports_no_mismatches(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}})

        assert replicate(session, plex).verify_mismatched == 0

    def test_a_title_the_account_cannot_see_is_not_counted_as_a_mismatch(self, session):
        """A target shared fewer libraries is the normal case. Counting its 404s twice — once as
        unreachable, once as a mismatch — would make a correct run look broken."""

        class Narrow(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                return False  # 404: not visible to this account

        plex = Narrow({"ADMIN": {100: leaf(100, count=1)}})

        report = replicate(session, plex)

        assert (report.unreachable, report.verify_mismatched) == (1, 0)


class TestFailureIsolation:
    def test_one_failing_title_does_not_abandon_the_rest(self, session):
        """A run is eleven thousand writes on a real account. One raising must not lose the others."""

        class Flaky(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                if op.rating_key == 100:
                    raise RuntimeError("boom")
                return super().apply_watch_op(op, token, dry_run=dry_run)

        plex = Flaky({"ADMIN": {100: leaf(100, count=1), 101: leaf(101, count=1)}})

        report = replicate(session, plex)

        assert report.applied == 1
        # `failed`, not `unreachable`: it RAISED. "That title isn't there for them" and "we don't
        # know what happened" are opposite claims and are counted apart.
        assert (report.failed, report.unreachable) == (1, 0)
        assert 101 in plex.state["TARGET"]


class TestCopiedPlayEvents:
    def test_the_true_dates_are_copied_onto_the_target_account(self, session):
        """A scrobble writes NO row to Plex's history log — probed live — so the target's own log
        stays empty however much we write. This copy is the only dated history it will ever have."""
        when = datetime(2021, 5, 4, tzinfo=UTC)
        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=1)}},
            history=[PlayEvent(10, 100, None, "movie", when, "h1"), PlayEvent(999, 555, None, "movie", when, "h2")],
        )

        report = replicate(session, plex)
        session.commit()

        rows = session.query(WatchEvent).filter(WatchEvent.plex_account_id == 20).all()
        assert report.events_copied == 1
        assert [r.rating_key for r in rows] == [100]  # the other account's play is not ours to copy
        assert rows[0].viewed_at.replace(tzinfo=UTC) == when

    def test_copied_events_earn_no_pick_credit(self, session):
        """The assertion that matters, and the one the first version of this test did not make.

        It asserted only that the rows carried `source='transfer'` — which they did — while nothing
        anywhere actually filtered on it. The design claimed the filter existed; it did not. So every
        copied play was credited to whatever row happened to contain that title at the copied
        timestamp, inflating the effectiveness report for rows that did nothing, and (through
        `_CreditInputs.observed`) making those credits impossible to withdraw afterwards.

        Marking a row is not the same as acting on the mark. This asserts the outcome.
        """
        from shortlist.server.services.watch_events import _scan_plays

        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=1)}},
            history=[PlayEvent(10, 100, None, "movie", datetime(2021, 5, 4, tzinfo=UTC), "h1")],
        )
        replicate(session, plex)
        session.commit()

        assert session.query(WatchEvent).count() == 1
        assert session.query(WatchEvent).one().source == "transfer"
        # `tmdb_of` is supplied so the key RESOLVES. Without it the scan drops the event for want of a
        # tmdb id and the test passes with the filter deleted — which is how the first version of this
        # assertion was itself bug-blind.
        assert _scan_plays(session, tmdb_of={100: (555, "movie")}) == []

    def test_a_genuine_play_on_the_same_account_is_still_scanned(self, session):
        """The filter must not blind us to the account's own real watching afterwards — which is the
        entire point of moving them onto it."""
        from shortlist.server.services.watch_events import _scan_plays

        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}}, history=[])
        replicate(session, plex)
        session.add(
            WatchEvent(
                plex_account_id=20,
                rating_key=100,
                media_type="movie",
                viewed_at=utcnow(),
                source="history",
                history_key="real-1",
            )
        )
        session.commit()

        scanned = _scan_plays(session, tmdb_of={100: (555, "movie")})

        assert [acct for acct, _when, _keys in scanned] == [20]

    def test_re_running_does_not_duplicate_them(self, session):
        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=1)}},
            history=[PlayEvent(10, 100, None, "movie", datetime(2021, 5, 4, tzinfo=UTC), "h1")],
        )
        replicate(session, plex)
        session.commit()

        replicate(session, plex)
        session.commit()

        assert session.query(WatchEvent).count() == 1


class TestGuards:
    def test_transferring_onto_the_same_account_is_refused(self, session):
        with pytest.raises(ValueError, match="same account"):
            transfer_watch_history(
                session,
                sessions=_factory_for(session),
                from_user_id=1,
                to_user_id=1,
                plex=FakePlex(),
                source_token="A",
                target_token="B",
            )

    def test_an_unknown_user_is_a_lookup_error(self, session):
        with pytest.raises(LookupError):
            transfer_watch_history(
                session,
                sessions=_factory_for(session),
                from_user_id=1,
                to_user_id=999,
                plex=FakePlex(),
                source_token="A",
                target_token="B",
            )

    def test_refuses_to_replicate_onto_a_shared_user(self, session):
        """A watching account is one of the OWNER'S OWN Home profiles. Onto a friend this would build
        their Picked-for-You from the owner's taste — and now also DELETE their real watch history,
        since the mirror removes whatever the source lacks."""
        session.add(User(id=3, plex_account_id=30, username="sarah", slug="sarah", user_type="shared"))
        session.commit()
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=1)}})

        with pytest.raises(ValueError, match="Plex Home users"):
            transfer_watch_history(
                session,
                sessions=_factory_for(session),
                from_user_id=1,
                to_user_id=3,
                plex=plex,
                source_token="ADMIN",
                target_token="TARGET",
            )

        assert plex.writes == []

    def test_an_owner_with_nothing_watched_is_reported_as_such(self, session):
        """ "Planned 0" has two opposite meanings. "They already match" is success; "there is nothing
        to copy" is the transfer being impossible, and the wizard has to say something different (#88)."""
        report = replicate(session, FakePlex({"ADMIN": {}}))

        assert (report.planned, report.source_empty) == (0, True)

    def test_a_source_with_history_is_never_reported_empty(self, session):
        report = replicate(session, FakePlex({"ADMIN": {100: leaf(100, count=1)}}))

        assert report.source_empty is False


class TestUndo:
    def _transferred(self, session):
        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=3, offset=8_000)}},
            history=[PlayEvent(10, 100, None, "movie", datetime(2021, 5, 4, tzinfo=UTC), "h1")],
        )
        report = replicate(session, plex)
        session.commit()
        return plex, report

    def test_it_puts_the_account_back_exactly(self, session):
        """Counts and offsets, not just watched/unwatched — restoring a rewatched film as watched-once
        leaves a third state that existed on neither account."""
        plex, report = self._transferred(session)

        undo_transfer(
            session, sessions=_factory_for(session), snapshot_id=report.snapshot_id, plex=plex, target_token="TARGET"
        )
        session.commit()

        # Compared on the state, not on titles: the snapshot stores counts and offsets only, since
        # those are what a restore has to put back and a title cannot be restored wrong.
        restored = plex.state["TARGET"]
        assert set(restored) == {77}
        assert (restored[77].view_count, restored[77].view_offset_ms) == (3, 8_000)

    def test_it_removes_the_copied_play_events(self, session):
        """They describe watches the account no longer has. Only ours — Plex's own rows are untouched."""
        plex, report = self._transferred(session)
        session.add(
            WatchEvent(plex_account_id=20, rating_key=1, media_type="movie", viewed_at=utcnow(), source="history")
        )
        session.commit()

        undo_transfer(
            session, sessions=_factory_for(session), snapshot_id=report.snapshot_id, plex=plex, target_token="TARGET"
        )
        session.commit()

        assert [r.source for r in session.query(WatchEvent).all()] == ["history"]

    def test_undoing_twice_is_refused_rather_than_replayed(self, session):
        """The second press would plan against a state the snapshot no longer describes."""
        plex, report = self._transferred(session)
        undo_transfer(
            session, sessions=_factory_for(session), snapshot_id=report.snapshot_id, plex=plex, target_token="TARGET"
        )
        session.commit()

        again = undo_transfer(
            session, sessions=_factory_for(session), snapshot_id=report.snapshot_id, plex=plex, target_token="TARGET"
        )

        assert again.planned == 0
        assert "already been undone" in again.errors[0]

    def test_an_unknown_snapshot_is_a_lookup_error(self, session):
        with pytest.raises(LookupError):
            undo_transfer(
                session, sessions=_factory_for(session), snapshot_id=999, plex=FakePlex(), target_token="TARGET"
            )

    def test_a_dry_run_undo_writes_nothing_and_leaves_the_snapshot_usable(self, session):
        plex, report = self._transferred(session)
        before = dict(plex.state["TARGET"])

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=report.snapshot_id,
            plex=plex,
            target_token="TARGET",
            dry_run=True,
        )
        session.commit()

        assert plex.state["TARGET"] == before
        assert session.get(WatchStateSnapshot, report.snapshot_id).restored_at is None


class TestStampingTheTrueDates:
    """The ordering problem that makes this a table read rather than an in-memory one.

    On a FIRST transfer the target has no `watched_titles` rows at all — they are created by the next
    watch sync, reading back what the transfer just wrote to Plex. Stamping from the state read would
    find nothing, and the true dates would never land: every title would read as watched today, seeds
    would come from a set sharing one timestamp, and the new account's picks would be noise.
    """

    def _transfer_with_history(self, session):
        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=1)}},
            history=[PlayEvent(10, 100, None, "movie", OLD, "h1")],
        )
        report = replicate(session, plex)
        session.commit()
        return report

    def test_a_first_transfer_has_no_rows_to_stamp_yet(self, session):
        report = self._transfer_with_history(session)

        assert report.events_copied == 1
        assert report.titles_cached == 0  # nothing exists to stamp — the sync creates them

    def test_the_sync_stamps_them_once_the_rows_exist(self, session):
        """This is the call `WatchSync.refresh_watched` makes after reading the account back."""
        self._transfer_with_history(session)
        watched(session, 2, "Dune", key=100, viewed_at=utcnow())  # as the sync would create it

        assert stamp_true_dates(session, 2) == 1
        session.commit()

        row = session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one()
        assert row.source_viewed_at.replace(tzinfo=UTC) == OLD

    def test_a_show_is_stamped_with_the_newest_of_its_episodes(self, session):
        """`watched_titles` is keyed at show level; the replica and the play log both work in leaves."""
        plex = FakePlex(
            {"ADMIN": {11: leaf(11, count=1, show=9, kind="episode")}},
            history=[
                PlayEvent(10, 11, 9, "episode", OLD, "h1"),
                PlayEvent(10, 12, 9, "episode", NEWER, "h2"),
            ],
        )
        replicate(session, plex)
        session.commit()
        watched(session, 2, "The Show", key=9, viewed_at=utcnow())

        stamp_true_dates(session, 2)
        session.commit()

        row = session.query(WatchedTitle).filter(WatchedTitle.rating_key == 9).one()
        assert row.source_viewed_at.replace(tzinfo=UTC) == NEWER

    def test_a_row_that_already_has_a_true_date_is_left_alone(self, session):
        """Their own later watching must never be overwritten by a copy of somebody else's."""
        self._transfer_with_history(session)
        watched(session, 2, "Dune", key=100, viewed_at=utcnow(), source=NEWER)

        assert stamp_true_dates(session, 2) == 0
        assert (
            session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one().source_viewed_at.replace(tzinfo=UTC)
            == NEWER
        )

    def test_it_is_a_no_op_for_an_account_that_never_had_a_transfer(self, session):
        """Runs on every user on every sync, so the 99% case has to cost nothing and change nothing."""
        watched(session, 2, "Dune", key=100, viewed_at=utcnow())

        assert stamp_true_dates(session, 2) == 0

    def test_only_transferred_events_are_used_never_the_persons_own_plays(self, session):
        """A `source='history'` row is that person really watching, today. Treating it as a "true
        date" would stamp today's date as if it were an older watch and re-order their own seeds."""
        session.add(
            WatchEvent(
                plex_account_id=20, rating_key=100, media_type="movie", viewed_at=OLD, source="history", history_key="x"
            )
        )
        watched(session, 2, "Dune", key=100, viewed_at=utcnow())
        session.commit()

        assert stamp_true_dates(session, 2) == 0


class TestDatesWhenThereIsNoPlayLog:
    """The case a live run exposed, and the one that matters most in practice.

    The maintainer's own account carries 10,948 watched titles and effectively NO play-log rows: the
    log does not reach back far enough, and a bulk "mark as watched" never writes one at all. Copying
    the log alone therefore carried zero dates, `source_viewed_at` stayed NULL, and every replicated
    title read as watched today — exactly the failure that column exists to prevent.
    """

    def test_the_last_viewed_date_is_used_when_the_log_has_nothing(self, session):
        stamp = int(OLD.timestamp())
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1, at=stamp)}}, history=[])

        report = replicate(session, plex)
        session.commit()

        assert report.events_copied == 1
        event = session.query(WatchEvent).one()
        assert event.viewed_at.replace(tzinfo=UTC) == OLD
        assert event.source == "transfer"

    def test_a_title_the_log_covers_is_not_duplicated(self, session):
        """A play-log row is the stronger fact — an exact play time rather than the latest view — so
        a title the log covers must not also get a weaker synthesised row beside it."""
        stamp = int(NEWER.timestamp())
        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=1, at=stamp)}},
            history=[PlayEvent(10, 100, None, "movie", OLD, "h1")],
        )

        replicate(session, plex)
        session.commit()

        rows = session.query(WatchEvent).all()
        assert len(rows) == 1
        assert rows[0].viewed_at.replace(tzinfo=UTC) == OLD  # the log's exact time, not lastViewedAt

    def test_a_title_plex_reported_no_date_for_is_skipped(self, session):
        """`last_viewed_at` is 0 when Plex reported none. Writing epoch 1970 as a watch date would put
        it at the very bottom of every recency read — a worse lie than having no date at all."""
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1, at=0)}}, history=[])

        assert replicate(session, plex).events_copied == 0

    def test_the_dates_reach_watched_titles_on_the_next_sync(self, session):
        """End to end for the thing that actually matters: seeds come off the most-recent end."""
        stamp = int(OLD.timestamp())
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1, at=stamp)}}, history=[])
        replicate(session, plex)
        session.commit()
        watched(session, 2, "Dune", key=100, viewed_at=utcnow())  # as the sync creates it: dated today

        stamp_true_dates(session, 2)
        session.commit()

        row = session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one()
        assert row.source_viewed_at.replace(tzinfo=UTC) == OLD

    def test_re_running_does_not_duplicate_the_synthesised_rows(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1, at=int(OLD.timestamp()))}}, history=[])
        replicate(session, plex)
        session.commit()

        replicate(session, plex)
        session.commit()

        assert session.query(WatchEvent).count() == 1


class TestAPartialReadNeverDeletes:
    """The most dangerous shape in the feature, found by reviewing rather than by a failure.

    `build_plan` treats the source as authoritative and REMOVES whatever it does not contain. A source
    read that quietly skipped a library — a 403, an unshared library, a token that lost access — would
    therefore un-mark every title that library holds on the target. On a real account that is 10,995
    episodes, and the verify pass would report a CLEAN run, because the target genuinely would match
    the truncated source.

    `watched_titles` has carried the same guard since a partial cache was once served as a complete
    one (`WatchedRead.covers_window`). The leaf read had none.
    """

    def _partial_source(self, session, unreadable=("2",)):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}, "TARGET": {11: leaf(11, count=1, show=9, kind="episode")}})
        real_read = plex.read_watch_state

        def truncated(sections, token):
            state = real_read(sections, token)
            return WatchState(items=state.items, unreadable=unreadable) if token == "ADMIN" else state

        plex.read_watch_state = truncated
        return plex

    def test_a_source_that_cannot_see_a_library_is_refused(self, session):
        plex = self._partial_source(session)

        with pytest.raises(ValueError, match="cannot see every library"):
            replicate(session, plex)

    def test_the_refusal_names_the_library_and_what_to_do(self, session):
        """ "It failed" is not actionable. The message has to say which library and how to fix it."""
        plex = self._partial_source(session, unreadable=("2",))

        with pytest.raises(ValueError) as caught:
            replicate(session, plex)

        assert "library 2" in str(caught.value)
        assert "Share it" in str(caught.value)

    def test_nothing_is_written_when_the_source_is_partial(self, session):
        plex = self._partial_source(session)

        with pytest.raises(ValueError):
            replicate(session, plex)

        assert plex.writes == []
        assert session.query(WatchStateSnapshot).count() == 0

    def test_a_complete_source_is_not_refused(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}})

        assert replicate(session, plex).planned == 1


class TestAPartialTargetReadPoisonsTheSnapshot:
    """A target shared FEWER libraries is legitimate — nothing can be written there anyway, so those
    writes 404 and land in `unreachable`. But the snapshot is then partial, and the undo is a MIRROR
    of the snapshot: restoring from it would un-mark every watch it never recorded.
    """

    def _partial_target(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=1)}})
        real_read = plex.read_watch_state

        def truncated(sections, token):
            state = real_read(sections, token)
            return WatchState(items=state.items, unreadable=("12",)) if token == "TARGET" else state

        plex.read_watch_state = truncated
        return plex

    def test_the_transfer_still_runs(self, session):
        report = replicate(session, self._partial_target(session))

        assert report.planned > 0
        assert report.target_unreadable == ["12"]

    def test_the_snapshot_records_that_it_is_incomplete(self, session):
        report = replicate(session, self._partial_target(session))
        session.commit()

        assert session.get(WatchStateSnapshot, report.snapshot_id).complete is False

    def test_undoing_from_an_incomplete_snapshot_is_refused(self, session):
        plex = self._partial_target(session)
        report = replicate(session, plex)
        session.commit()
        plex.writes.clear()

        undone = undo_transfer(
            session, sessions=_factory_for(session), snapshot_id=report.snapshot_id, plex=plex, target_token="TARGET"
        )

        assert "incomplete" in undone.errors[0]
        assert plex.writes == []

    def test_a_complete_snapshot_still_undoes(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=1)}})
        report = replicate(session, plex)
        session.commit()

        assert session.get(WatchStateSnapshot, report.snapshot_id).complete is True
        assert (
            undo_transfer(
                session,
                sessions=_factory_for(session),
                snapshot_id=report.snapshot_id,
                plex=plex,
                target_token="TARGET",
            ).errors
            == []
        )


class TestVerifyCountsTheRightThings:
    def test_a_real_failure_is_not_hidden_by_unreachable_titles(self, session):
        """Subtracting the unreachable COUNT from the mismatch count was right only when the two
        populations coincided. Eight unreachable and two real failures reported a clean run."""

        class Mixed(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                # False = 404, genuinely not visible to this account.
                # True with no state change = accepted and silently did nothing — a REAL failure.
                return op.rating_key not in (101, 102)

        plex = Mixed({"ADMIN": {k: leaf(k, count=1) for k in (100, 101, 102, 103)}})

        report = replicate(session, plex)

        assert report.unreachable == 2
        # 100 and 103 were accepted but never landed. Both must be reported.
        assert report.verify_mismatched == 2


class TestTheSnapshotSurvivesAFailure:
    """Rule 2 is not "write a snapshot", it is "the snapshot exists once anything has been mutated".

    It was only FLUSHED, and the caller's transaction commits after all ~11,000 Plex writes — so
    anything raising in between (the verify read hitting a 500, the play-log copy, a stray DB error)
    rolled the snapshot back while the writes stayed on Plex. The job then retried, re-read a
    half-mirrored target, found the plan already converged, and took no snapshot at all: the un-marked
    watches were gone with no record anywhere.
    """

    class _FailsAfterWriting(FakePlex):
        """Applies every write, then dies on the verify read-back — the real failure window."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._reads = 0

        def read_watch_state(self, sections, token):
            self._reads += 1
            if self._reads > 2:  # source, target, then the verify read
                raise RuntimeError("PMS 500 on the verify read")
            return super().read_watch_state(sections, token)

    def test_a_failure_after_the_writes_still_leaves_a_snapshot(self, session):
        plex = self._FailsAfterWriting(
            {"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=2, offset=900)}}
        )

        with pytest.raises(RuntimeError, match="500"):
            replicate(session, plex)

        # The writes landed, so the record of what was there before MUST have landed too.
        assert plex.writes
        snapshot = session.query(WatchStateSnapshot).one()
        assert snapshot.state == [[77, 2, 900, "movie", None]]

    def test_a_retry_reuses_the_first_attempts_snapshot(self, session):
        """The second half of the same bug. A retry re-reads a target the failed attempt already
        half-mirrored, so a fresh snapshot would record OUR damage as the user's own state — and the
        real "before" would be lost for good."""
        original = WatchState(items={77: leaf(77, count=2, offset=900)})
        first = take_snapshot(_factory_for(session), 2, original, job_id=42)
        session.commit()

        again = take_snapshot(_factory_for(session), 2, WatchState(items={}), job_id=42)

        assert again == first
        assert session.get(WatchStateSnapshot, first).state == [[77, 2, 900, "movie", None]]

    def test_a_different_job_takes_its_own_snapshot(self, session):
        """Reuse is keyed on the JOB, not the user — a genuine second transfer must record the state
        it actually found, not the state some earlier transfer found."""
        take_snapshot(_factory_for(session), 2, WatchState(items={77: leaf(77, count=1)}), job_id=1)
        session.commit()

        second = take_snapshot(_factory_for(session), 2, WatchState(items={88: leaf(88, count=1)}), job_id=2)
        session.commit()

        assert session.get(WatchStateSnapshot, second).state == [[88, 1, 0, "movie", None]]
        assert session.query(WatchStateSnapshot).count() == 2

    def test_a_restored_snapshot_is_never_reused(self, session):
        """Once restored it describes a state that has been put back — reusing it would let a later
        undo remove everything done since."""
        first = take_snapshot(_factory_for(session), 2, WatchState(items={77: leaf(77, count=1)}), job_id=7)
        session.get(WatchStateSnapshot, first).restored_at = utcnow()
        session.commit()

        again = take_snapshot(_factory_for(session), 2, WatchState(items={88: leaf(88, count=1)}), job_id=7)

        assert again != first


class TestAShowWeEmptiedIsCleared:
    """Un-scrobbling an episode does not clear its show.

    The show row keeps its own `viewCount`, so it still comes back from `?type=2&unwatched=0` — the
    read `watched_titles` is built from — now reading 0/N. The target then looks like someone who has
    watched a show with none of it watched, and the engine stops offering it. Same residue that
    explains the 63 zero-episode shows found on a real account.
    """

    def test_the_show_key_is_cleared_when_all_its_episodes_are_removed(self, session):
        plex = FakePlex(
            {"ADMIN": {}, "TARGET": {11: leaf(11, count=1, show=9, kind="episode")}},
        )

        replicate(session, plex)

        assert (OpKind.UNMARK, 9) in [(k, rk) for k, rk, *_ in plex.writes] or 9 in {rk for _k, rk, *_ in plex.writes}

    def test_a_show_the_source_still_watches_keeps_its_row(self, session):
        """Those episodes were re-marked, not removed — clearing the show would undo the transfer's
        own work and make the show invisible to the cache."""
        plex = FakePlex(
            {
                "ADMIN": {11: leaf(11, count=1, show=9, kind="episode")},
                "TARGET": {12: leaf(12, count=1, show=9, kind="episode")},
            },
        )

        replicate(session, plex)

        assert 9 not in {rk for _k, rk, *_ in plex.writes}

    def test_nothing_extra_is_written_when_no_show_was_emptied(self, session):
        plex = FakePlex({"ADMIN": {100: leaf(100, count=1)}})

        replicate(session, plex)

        assert {rk for _k, rk, *_ in plex.writes} == {100}

    def test_a_dry_run_reports_the_show_clear_without_performing_it(self, session):
        """A preview that omitted them understated what a real run does (rule 8 and rule 10)."""
        plex = FakePlex({"ADMIN": {}, "TARGET": {11: leaf(11, count=1, show=9, kind="episode")}})

        report = replicate(session, plex, dry_run=True)

        assert report.shows_cleared == 1
        # Recorded as a DRY write — the flag is the assertion, not the absence of the call.
        assert all(dry for *_rest, dry in plex.writes if _rest[1] == 9)


class TestUndoNeverClearsAShowItIsRestoring:
    """The destructive cell that three review passes of `_clear_emptied_shows` tests never covered.

    `_clear_emptied_shows` spares a show by looking it up in the SOURCE's items — and on the undo path
    the source is the snapshot. A snapshot written before the show key was added carries
    `show_rating_key=None` for every row, so "spare these" comes out empty, while "these were emptied"
    (built from the live read) carries real show keys. It would un-scrobble a show the snapshot
    explicitly asked to keep — and un-scrobbling a show key clears every episode under it. The restore
    would destroy the very watches it was restoring, and report success.

    Every other test of this goes through the transfer, and both integration snapshots are movie-only,
    which is exactly why the cell was invisible.
    """

    def _legacy_snapshot(self, session, state_rows):
        row = WatchStateSnapshot(user_id=2, state=state_rows, taken_at=utcnow(), complete=True)
        session.add(row)
        session.commit()
        return row.id

    def test_a_four_element_snapshot_clears_no_show(self, session):
        # The snapshot wants episodes 11 and 12 kept; the account now also has 13 and 14.
        snapshot_id = self._legacy_snapshot(session, [[11, 1, 0, "episode"], [12, 1, 0, "episode"]])
        plex = FakePlex(
            {"TARGET": {k: leaf(k, count=1, show=9, kind="episode") for k in (11, 12, 13, 14)}},
        )

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot_id,
            plex=plex,
            target_token="TARGET",
        )

        assert 9 not in {rk for _k, rk, *_ in plex.writes}
        # And the episodes it was restoring are still there.
        assert set(plex.state["TARGET"]) == {11, 12}

    def test_a_five_element_snapshot_still_clears_an_emptied_show(self, session):
        """The capability is not lost — only withheld where it cannot be applied safely."""
        snapshot_id = self._legacy_snapshot(session, [[100, 1, 0, "movie", None]])
        plex = FakePlex({"TARGET": {11: leaf(11, count=1, show=9, kind="episode"), 100: leaf(100, count=1)}})

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot_id,
            plex=plex,
            target_token="TARGET",
        )

        assert 9 in {rk for _k, rk, *_ in plex.writes}

    def test_an_empty_snapshot_still_clears_the_show(self, session):
        """The normal setup: a transfer onto a brand-new account records an EMPTY snapshot.

        `any(...)` is False for `[]`, so undoing the most common case skipped the show clear entirely
        and left the scrobbled-show residue this rewrite exists to repair. `all([])` is True.
        """
        snapshot_id = self._legacy_snapshot(session, [])
        plex = FakePlex({"TARGET": {11: leaf(11, count=1, show=9, kind="episode")}})

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot_id,
            plex=plex,
            target_token="TARGET",
        )

        assert 9 in {rk for _k, rk, *_ in plex.writes}

    def test_a_mixed_format_snapshot_clears_no_show(self, session):
        """One 5-element row made the whole snapshot look trustworthy under `any`, while the
        4-element rows still contributed nothing to "spare these" — so a show the snapshot asked to
        keep was un-scrobbled and every episode under it destroyed."""
        snapshot_id = self._legacy_snapshot(session, [[11, 1, 0, "episode"], [100, 1, 0, "movie", None]])
        plex = FakePlex(
            {
                "TARGET": {
                    11: leaf(11, count=1, show=9, kind="episode"),
                    12: leaf(12, count=1, show=9, kind="episode"),
                    100: leaf(100, count=1),
                }
            }
        )

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot_id,
            plex=plex,
            target_token="TARGET",
        )

        assert 9 not in {rk for _k, rk, *_ in plex.writes}
        assert 11 in plex.state["TARGET"]


class TestUndoEnforcesTheSameIdentityRuleAsTheTransfer:
    """`_check_pair` refuses a non-MANAGED target because mirroring onto a shared user deletes that
    person's real watch history, and its comment says "this is the same rule at the layer that
    actually writes". The undo IS that layer for the restore, and it trusted `snapshot.user_id`.

    It can drift: `user_sync` reassigns `user_type` on every roster sync, so a Home user removed from
    Home and re-invited as a shared account flips MANAGED → SHARED while their snapshot stays listed.
    """

    def test_it_refuses_once_the_account_is_no_longer_a_home_user(self, session):
        snapshot = WatchStateSnapshot(user_id=2, state=[[77, 1, 0, "movie", None]], taken_at=utcnow(), complete=True)
        session.add(snapshot)
        session.commit()
        session.query(User).filter(User.id == 2).update({"user_type": "shared"})
        session.commit()
        plex = FakePlex({"TARGET": {88: leaf(88, count=1)}})

        report = undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot.id,
            plex=plex,
            target_token="TARGET",
        )

        assert "no longer one of your own Plex Home users" in report.errors[0]
        assert plex.writes == []

    def test_a_still_managed_account_restores_normally(self, session):
        snapshot = WatchStateSnapshot(user_id=2, state=[[77, 1, 0, "movie", None]], taken_at=utcnow(), complete=True)
        session.add(snapshot)
        session.commit()
        plex = FakePlex({"TARGET": {}})

        report = undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot.id,
            plex=plex,
            target_token="TARGET",
        )

        assert report.errors == []
        assert plex.state["TARGET"][77].view_count == 1


class TestAFailedRestoreKeepsItsSnapshot:
    """`restored_at` was stamped unconditionally after the write loop.

    So a PMS that 500s mid-undo — or a library un-shared since the copy, which makes every write
    return False with no exception at all — left `applied=0` while the snapshot was marked used. It
    then dropped out of `/snapshots` and a retry answered "already been undone": the one recovery
    record rule 2 exists to preserve, consumed by an attempt that restored nothing.
    """

    def _pending(self, session):
        row = WatchStateSnapshot(user_id=2, state=[[77, 1, 0, "movie", None]], taken_at=utcnow(), complete=True)
        session.add(row)
        session.commit()
        return row

    def test_a_restore_that_raised_leaves_the_snapshot_undoable(self, session):
        class Broken(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                raise TimeoutError("PMS unreachable")

        snapshot = self._pending(session)
        report = undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot.id,
            plex=Broken({"TARGET": {}}),
            target_token="TARGET",
        )
        session.commit()

        assert report.failed > 0
        assert session.get(WatchStateSnapshot, snapshot.id).restored_at is None
        # FIRST, not last: the UI renders `errors[0]`, and the per-op entries behind it read
        # "12345: ReadTimeout" — a rating key and an exception class, which is not something anyone
        # can act on. The plain sentence has to be the one that reaches the screen.
        assert "did not complete" in report.errors[0]
        # And the per-op cause has to SURVIVE behind it. Asserting only `errors[0]` passes just as
        # well if the list is replaced wholesale, which would drop the only record of what actually
        # failed from the audit row (rule 10) — the job detail shows `errors[0]` and nothing else.
        assert any("TimeoutError" in e for e in report.errors[1:])

    def test_a_restore_the_account_could_not_take_also_keeps_it(self, session):
        """No exception at all — every write simply answers False, which is what an un-shared library
        looks like. The unconditional stamp treated that as a completed restore."""

        class Narrow(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                return False

        snapshot = self._pending(session)
        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot.id,
            plex=Narrow({"TARGET": {}}),
            target_token="TARGET",
        )
        session.commit()

        assert session.get(WatchStateSnapshot, snapshot.id).restored_at is None

    def test_a_failed_restore_keeps_the_copied_dates(self, session):
        """They are the only carrier of the true watch dates. Deleting them on a restore that never
        landed loses them with nothing put back in exchange."""

        class Broken(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                raise TimeoutError("PMS unreachable")

        snapshot = self._pending(session)
        session.add(
            WatchEvent(
                plex_account_id=20, rating_key=1, media_type="movie", viewed_at=OLD, source="transfer", history_key="t1"
            )
        )
        session.commit()

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot.id,
            plex=Broken({"TARGET": {}}),
            target_token="TARGET",
        )
        session.commit()

        assert session.query(WatchEvent).count() == 1

    def test_a_clean_restore_still_marks_it_used(self, session):
        snapshot = self._pending(session)
        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=snapshot.id,
            plex=FakePlex({"TARGET": {}}),
            target_token="TARGET",
        )
        session.commit()

        assert session.get(WatchStateSnapshot, snapshot.id).restored_at is not None


class TestAFailedWriteIsNotAnUnsharedLibrary:
    """They are opposite claims, and folding them together produced two false statements at once.

    Three timeouts rendered as "3 were in libraries that account can't see" AND "that account now
    matches yours" — the second because a failed key was excused out of the mismatch tally.
    """

    def test_a_raised_write_is_counted_apart_and_still_reported_as_a_mismatch(self, session):
        class Flaky(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                raise TimeoutError("boom")

        plex = Flaky({"ADMIN": {k: leaf(k, count=1) for k in (100, 101, 102)}})

        report = replicate(session, plex)

        assert (report.failed, report.unreachable) == (3, 0)
        assert report.verify_mismatched == 3  # not excused into a clean run

    def test_a_refused_write_is_still_unreachable_and_excused(self, session):
        class Narrow(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                return False

        plex = Narrow({"ADMIN": {100: leaf(100, count=1)}})

        report = replicate(session, plex)

        assert (report.failed, report.unreachable) == (0, 1)
        assert report.verify_mismatched == 0


class TestTheSnapshotLandsBeforeTheFirstWrite:
    """The ORDERING, not the contents — which is the whole of rule 2.

    `test_the_snapshot_is_taken_before_any_write` asserts what the snapshot CONTAINS, and its contents
    come from `target_state`, captured before the loop wherever `take_snapshot` is called. Moving the
    call to after the write loop left all 78 tests green: writes on someone's Plex account with no
    record of what was there before, and nothing to catch it. A container recreate mid-run is not
    hypothetical on the host this ships to.

    So this test observes the database FROM INSIDE the first write.
    """

    class _WatchingPlex(FakePlex):
        """Counts the snapshots visible on a separate connection at each write."""

        def __init__(self, *a, factory=None, **kw):
            super().__init__(*a, **kw)
            self._factory = factory
            self.snapshots_at_write: list[int] = []

        def apply_watch_op(self, op, token, *, dry_run=False):
            with self._factory() as probe:
                self.snapshots_at_write.append(probe.query(WatchStateSnapshot).count())
            return super().apply_watch_op(op, token, dry_run=dry_run)

    def test_the_snapshot_is_committed_before_the_first_write_lands(self, session):
        factory = _factory_for(session)
        plex = self._WatchingPlex(
            {"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=1)}},
            factory=factory,
        )

        replicate(session, plex)

        assert plex.snapshots_at_write, "no writes happened, so this proves nothing"
        # Visible on the FIRST write, not merely by the end.
        assert plex.snapshots_at_write[0] == 1

    def test_a_dry_run_writes_no_snapshot_at_all(self, session):
        """The counterweight: nothing is at risk, so nothing is recorded."""
        factory = _factory_for(session)
        plex = self._WatchingPlex(
            {"ADMIN": {100: leaf(100, count=1)}, "TARGET": {77: leaf(77, count=1)}},
            factory=factory,
        )

        replicate(session, plex, dry_run=True)

        assert plex.snapshots_at_write
        assert set(plex.snapshots_at_write) == {0}


class TestUndoLeavesNoPhantomWatchesBehind:
    """The undo restored Plex and left Shortlist's own record of the copied watches stamped.

    `watch_cache` exempts rows carrying a `source_viewed_at` from both the full-read replace and the
    incremental drop — right while the transfer stands, permanent once it is undone. So the phantom
    watches could never self-heal: Plex says the account has not watched the title, the cache keeps it
    anyway, and the engine's already-watched filter suppresses it for ever, on the one account this
    feature exists to set up.
    """

    def _transferred(self, session):
        plex = FakePlex(
            {"ADMIN": {100: leaf(100, count=1, at=int(OLD.timestamp()))}},
            history=[],
        )
        report = replicate(session, plex)
        session.commit()
        watched(session, 2, "Arrival", key=100, viewed_at=utcnow())
        stamp_true_dates(session, 2)
        session.commit()
        return plex, report

    def test_the_undo_deletes_its_own_cached_rows(self, session):
        """Undo clears up after itself rather than leaving it to the periodic sweep.

        The sweep only runs on the `sync.watch_full_days` cadence, only when the read can prove it
        saw the whole library, and never at all on a PMS that does not report `totalSize` — so a row
        left for it could sit there for ever on exactly the servers least able to recover. Deleting
        is safe because every sync reads each library complete (issue #108): anything removed that
        the account genuinely still watches is back within one sync.
        """
        plex, transfer = self._transferred(session)
        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one().source_viewed_at is not None

        # The UNDO's report, not the transfer's — the two are different objects and asserting on the
        # wrong one passes whatever the undo does.
        undone = undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=transfer.snapshot_id,
            plex=plex,
            target_token="TARGET",
        )
        session.commit()

        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).count() == 0
        assert undone.titles_cached == -1, "one row removed, reported as a removal not an addition"

    def test_a_title_the_account_watched_ITSELF_survives_the_undo(self, session):
        """The delete is scoped to the rating keys this transfer copied, never "every stamped row".

        `stamp_true_dates` matches on rating key alone, so a title the watching account had already
        watched on its own can end up stamped. An unscoped delete took those too — and the re-read
        that is supposed to heal it is not guaranteed: a library the account is no longer shared is
        skipped with its rows deliberately kept, and a managed account whose token cannot be minted
        is never refilled at all. On those shapes the over-delete is permanent.
        """
        plex, report = self._transferred(session)
        watched(session, 2, "Their Own Film", key=777, viewed_at=utcnow(), source=OLD)
        session.commit()

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=report.snapshot_id,
            plex=plex,
            target_token="TARGET",
        )
        session.commit()

        left = {r.rating_key for r in session.query(WatchedTitle).filter(WatchedTitle.user_id == 2)}
        assert left == {777}, "the undo deleted a watch it did not create"

    def test_a_later_sync_does_not_bring_the_row_back(self, session):
        """Undo deletes the rows itself now, so the sync has nothing left to sweep — and must not
        resurrect them either. Plex no longer reports the title for this account, so a complete read
        returns nothing for it and the cache stays empty."""
        from shortlist.engine.models import MediaType, UserProfile
        from shortlist.server.services.watch_cache import WatchCache

        plex, report = self._transferred(session)
        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=report.snapshot_id,
            plex=plex,
            target_token="TARGET",
        )
        session.commit()
        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).count() == 0

        profile = UserProfile(username="steve", plex_account_id=20, user_type=UserType.MANAGED, slug="steve")
        WatchCache(lambda: session).sync_section(
            session,
            profile,
            2,
            "1",
            MediaType.MOVIE,
            lambda since: WatchedRead(items=[], covers_window=True),
            force_full=True,
            reconcile=True,
        )
        session.commit()

        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).count() == 0

    def test_a_failed_undo_leaves_the_stamps_alone(self, session):
        """It only clears when the restore actually landed — the same gate as the snapshot."""

        class Broken(FakePlex):
            def apply_watch_op(self, op, token, *, dry_run=False):
                raise TimeoutError("boom")

        plex, report = self._transferred(session)
        broken = Broken(plex.state)

        undo_transfer(
            session,
            sessions=_factory_for(session),
            snapshot_id=report.snapshot_id,
            plex=broken,
            target_token="TARGET",
        )
        session.commit()

        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one().source_viewed_at is not None
