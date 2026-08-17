"""shortlist/server/services/watching_account.py — moving the owner's watching to a second account.

The load-bearing behaviour here is the DATE. Plex stamps a scrobble `now` and cannot be told
otherwise, so a transfer that only wrote `viewed_at` would hand the new account a history where
everything was watched today — and since seeds come off the most-recent end of that list, its
recommendations would be an arbitrary sample. `source_viewed_at` is what stops that, so most of
these tests are about it surviving.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shortlist.engine.models import UserType
from shortlist.server.db.models import User, WatchedTitle, utcnow
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.watch_cache import WatchCache
from shortlist.server.services.watching_account import (
    candidate_home_users,
    transfer_watch_history,
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


class TestTransferWatchHistory:
    def test_copies_the_owners_titles_onto_the_target(self, session):
        watched(session, 1, "Dune", key=100, viewed_at=OLD)
        watched(session, 1, "Arrival", key=101, viewed_at=NEWER)

        report = transfer_watch_history(session, from_user_id=1, to_user_id=2)
        session.commit()

        assert (report.copied, report.already_present) == (2, 0)
        titles = {r.title for r in session.query(WatchedTitle).filter(WatchedTitle.user_id == 2)}
        assert titles == {"Dune", "Arrival"}

    def test_records_the_true_date_so_a_later_scrobble_cannot_erase_it(self, session):
        """The whole point. Without `source_viewed_at`, the sync that runs after the scrobbles would
        overwrite `viewed_at` with today and flatten every watch into one timestamp."""
        watched(session, 1, "Dune", key=100, viewed_at=OLD)

        transfer_watch_history(session, from_user_id=1, to_user_id=2)
        session.commit()

        copy = session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one()
        assert copy.source_viewed_at.replace(tzinfo=UTC) == OLD

    def test_a_scrobbling_transfer_dates_viewed_at_now_but_keeps_the_real_date_apart(self, session):
        """A scrobble makes Plex report TODAY, so `viewed_at` is written as today to match what the
        next sync will say — while `source_viewed_at` keeps the date the person actually watched."""
        watched(session, 1, "Dune", key=100, viewed_at=OLD)
        plex = MagicMock()
        plex.scrobble_as.return_value = True

        transfer_watch_history(session, from_user_id=1, to_user_id=2, plex=plex, target_token="tok", scrobble=True)
        session.commit()

        copy = session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one()
        assert copy.source_viewed_at.replace(tzinfo=UTC) == OLD
        assert copy.viewed_at.replace(tzinfo=UTC) > NEWER  # stamped now, like Plex will report it
        plex.scrobble_as.assert_called_once_with(100, "tok", dry_run=False)

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

        cache.sync_section(session, profile, 2, "1", MediaType.MOVIE, lambda since: [], force_full=True)
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

        cache.sync_section(session, profile, 2, "1", MediaType.MOVIE, lambda since: [], force_full=True)
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
        cache.sync_section(session, profile, 2, "1", MediaType.MOVIE, lambda since: [], force_full=True)
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

    def test_the_targets_own_watches_are_never_replaced_by_a_copy(self, session):
        """Their real viewing is more truthful than a copy of somebody else's."""
        watched(session, 1, "Dune", key=100, viewed_at=OLD)
        watched(session, 2, "Dune", key=100, viewed_at=NEWER)

        report = transfer_watch_history(session, from_user_id=1, to_user_id=2)
        session.commit()

        assert (report.copied, report.already_present) == (0, 1)
        assert (
            session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).one().viewed_at.replace(tzinfo=UTC) == NEWER
        )

    def test_an_empty_source_history_is_reported_as_such_not_as_a_copy_of_nothing(self, session):
        """ "Copied 0" has two opposite meanings and the caller must be able to tell them apart.

        "They already had everything" is success; "Shortlist has never read your watch history" is
        the copy being impossible. Reported as the same bare 0, the setup wizard offered a transfer
        that could not do anything and said nothing about why — the reporter in #88 concluded the
        feature was broken and wrote their own script.
        """
        report = transfer_watch_history(session, from_user_id=1, to_user_id=2)

        assert report.copied == 0
        assert report.source_empty is True

    def test_a_source_that_has_history_is_never_reported_empty(self, session):
        watched(session, 1, "Dune", key=100, viewed_at=OLD)

        report = transfer_watch_history(session, from_user_id=1, to_user_id=2)

        assert (report.copied, report.source_empty) == (1, False)

    def test_dry_run_writes_nothing_but_still_counts(self, session):
        watched(session, 1, "Dune", key=100, viewed_at=OLD)
        plex = MagicMock()
        plex.scrobble_as.return_value = True

        report = transfer_watch_history(
            session, from_user_id=1, to_user_id=2, plex=plex, target_token="tok", scrobble=True, dry_run=True
        )
        session.commit()

        assert (report.copied, report.dry_run) == (1, True)
        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).count() == 0
        plex.scrobble_as.assert_called_once_with(100, "tok", dry_run=True)

    def test_an_unreachable_title_does_not_abandon_the_rest(self, session):
        """A title in a library the account was never shared is the normal case, not a failure —
        the copy into Shortlist has already happened, so a missing checkmark is cosmetic."""
        watched(session, 1, "Dune", key=100, viewed_at=OLD)
        watched(session, 1, "Arrival", key=101, viewed_at=OLD)
        plex = MagicMock()
        plex.scrobble_as.side_effect = [RuntimeError("boom"), True]

        report = transfer_watch_history(
            session, from_user_id=1, to_user_id=2, plex=plex, target_token="tok", scrobble=True
        )
        session.commit()

        assert (report.copied, report.scrobbled, report.scrobble_skipped) == (2, 1, 1)
        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 2).count() == 2

    def test_scrobbling_without_a_client_is_refused_rather_than_silently_skipped(self, session):
        watched(session, 1, "Dune", key=100, viewed_at=OLD)

        with pytest.raises(ValueError, match="needs a Plex client"):
            transfer_watch_history(session, from_user_id=1, to_user_id=2, scrobble=True)

    def test_transferring_onto_the_same_account_is_refused(self, session):
        with pytest.raises(ValueError, match="same account"):
            transfer_watch_history(session, from_user_id=1, to_user_id=1)

    def test_an_unknown_user_is_a_lookup_error(self, session):
        with pytest.raises(LookupError):
            transfer_watch_history(session, from_user_id=1, to_user_id=999)

    def test_refuses_to_copy_the_owners_history_onto_a_shared_user(self, session):
        """A watching account is one of the OWNER'S OWN Home profiles. Copying onto a friend would
        build that person's Picked-for-You from the owner's taste and exclude titles they have never
        seen — one person's data silently wearing another's name. The UI only offers Home users; this
        is the same rule at the layer that actually writes."""
        session.add(User(id=3, plex_account_id=30, username="sarah", slug="sarah", user_type="shared"))
        session.commit()
        watched(session, 1, "Dune", key=100, viewed_at=OLD)

        with pytest.raises(ValueError, match="Plex Home users"):
            transfer_watch_history(session, from_user_id=1, to_user_id=3)

        assert session.query(WatchedTitle).filter(WatchedTitle.user_id == 3).count() == 0

    def test_a_re_run_tops_up_the_scrobbles_it_missed(self, session):
        """Resume safety (rule 6). An interrupted transfer leaves rows copied but unmarked; counting
        those as `already_present` and SKIPPING their scrobble would strand them for ever, because a
        re-run is the only tool the owner has."""
        watched(session, 1, "Dune", key=100, viewed_at=OLD)
        transfer_watch_history(session, from_user_id=1, to_user_id=2)  # copied, never scrobbled
        session.commit()
        plex = MagicMock()
        plex.scrobble_as.return_value = True

        report = transfer_watch_history(
            session, from_user_id=1, to_user_id=2, plex=plex, target_token="tok", scrobble=True
        )

        assert (report.copied, report.already_present, report.scrobbled) == (0, 1, 1)
        plex.scrobble_as.assert_called_once_with(100, "tok", dry_run=False)


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
