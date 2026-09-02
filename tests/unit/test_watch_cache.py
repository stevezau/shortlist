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

from shortlist.engine.clients.plex_pms import WatchedRead
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


def sync(
    cache,
    sessions,
    user_id,
    items,
    *,
    force_full=False,
    reconcile=False,
    capture=None,
    section=SECTION,
    covers_window=True,
    library="",
):
    """Run one section sync against a reader that returns `items` verbatim, whatever it was asked for.

    Use `sync_pms` unless the point of the test IS the mismatch: a real incremental read returns
    everything at or after `since`, including the deliberate overlap, so a reader that answers with
    only the newest titles is claiming every other title in the window was un-watched.

    `covers_window` is the reader's claim that it returned everything it was asked for — the only
    thing that lets the cache delete on absence. It is the claim for BOTH read shapes: for an
    incremental read, everything at or after `since`; for a complete one, the whole library. Pass
    False to model a truncated walk of either.
    """

    def read(since):
        if capture is not None:
            capture.append(since)
        return WatchedRead(items=list(items), covers_window=covers_window)

    with sessions() as session:
        outcome = cache.sync_section(
            session,
            profile(),
            user_id,
            section,
            MediaType.MOVIE,
            read,
            library=library,
            force_full=force_full,
            reconcile=reconcile,
        )
        session.commit()
    return outcome


def sync_pms(
    cache, sessions, user_id, library, *, force_full=False, reconcile=False, capture=None, section=SECTION, name=""
):
    """Run one section sync against a reader modelling a HEALTHY PMS: `library` is what the server
    holds NOW, the walk returns everything in it viewed at or after `since`, and it claims to have
    covered the window.

    Un-watching is therefore expressed the way it really happens — the title is simply absent from
    the library on the next call — rather than by hand-picking what the reader returns.

    This models the IDEAL reader. Whether the real `PlexClient.watched_titles` can actually deliver
    that coverage — and correctly refuses to claim it when it cannot — is the contract between the
    client and this cache, and is covered against real HTTP in
    `test_clients.py::TestWatchedWindowCoverage`.
    """

    def read(since):
        if capture is not None:
            capture.append(since)
        return WatchedRead(
            items=[item for item in library if since is None or item.watched_at >= since],
            covers_window=True,
        )

    with sessions() as session:
        outcome = cache.sync_section(
            session,
            profile(),
            user_id,
            section,
            MediaType.MOVIE,
            read,
            library=name,
            force_full=force_full,
            reconcile=reconcile,
        )
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
    def test_a_reconcile_pass_drops_a_title_that_was_un_watched(self, sessions, user_id):
        """The whole reason the periodic reconcile exists — nothing else can notice this."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2)])

        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)], force_full=True, reconcile=True)

        with sessions() as session:
            assert [row.title for row in session.query(WatchedTitle).all()] == ["Heat"]

    def test_a_complete_read_alone_does_NOT_drop_anything(self, sessions, user_id):
        """Every sync reads the whole library now (issue #108). Only the periodic pass may DELETE.

        Keeping those separate is what stopped the read getting 42x more frequent from making the
        destructive path 42x more frequent with it — `covers_window` is derived from the same
        response it validates, so a server that under-reports consistently proves itself complete.
        """
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2)])

        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)], force_full=True)

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Heat", "Dune"}

    def test_a_read_that_loses_most_of_a_section_is_confirmed_before_anything_is_deleted(self, sessions, user_id):
        """`covers_window` is derived from the response it validates, so a server that under-reports
        `totalSize` proves itself complete — and `totalSize="0"` is the extreme of that, erasing the
        section while reporting success. Reproduced in review against the real client.

        A second read is asked for before dropping most of a library. Here it disagrees, so nothing
        goes. One extra request, only on the rare pass that would delete half a library.
        """
        cache = WatchCache(sessions)
        full = [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2), watched("Alien", tmdb_id=3)]
        sync(cache, sessions, user_id, full)

        answers = [[], full]  # the truncated answer first, the truth on the confirming read
        with sessions() as session:
            cache.sync_section(
                session,
                profile(),
                user_id,
                SECTION,
                MediaType.MOVIE,
                lambda since: WatchedRead(items=list(answers.pop(0)), covers_window=True),
                force_full=True,
                reconcile=True,
            )
            session.commit()

        with sessions() as session:
            assert {r.title for r in session.query(WatchedTitle).all()} == {"Heat", "Dune", "Alien"}
        assert answers == [], "the confirming read was never made"

    def test_a_section_that_really_did_empty_is_swept_once_a_second_read_agrees(self, sessions, user_id):
        """The guard must not become a reason stale titles live for ever. Two reads agreeing that a
        library is empty is the answer being consistent, not a blip — and `watching_account`'s undo
        no longer depends on this, it deletes its own rows."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2)])

        sync(cache, sessions, user_id, [], force_full=True, reconcile=True)

        with sessions() as session:
            assert session.query(WatchedTitle).count() == 0

    def test_transferred_rows_do_not_make_the_shrink_guard_fire(self, sessions, user_id):
        """The guard compares the read against the DELETABLE rows, not against every cached row.

        The replace only ever touches `source_viewed_at IS NULL`, so counting transferred rows on the
        other side of the comparison put the two on different populations. On a watching account
        carrying a transfer that inflated the count permanently: the guard fired on every reconcile
        for ever, bought a second full page-walk each time, and told the operator that titles had
        vanished when nothing had.
        """
        from datetime import UTC, datetime

        cache = WatchCache(sessions)
        ordinary = [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2)]
        sync(cache, sessions, user_id, ordinary)
        with sessions() as session:
            for n in range(8):
                session.add(
                    WatchedTitle(
                        user_id=user_id,
                        section_key=SECTION,
                        rating_key=900 + n,
                        tmdb_id=900 + n,
                        media_type="movie",
                        title=f"Transferred {n}",
                        viewed_at=datetime.now(UTC),
                        source_viewed_at=datetime.now(UTC),
                    )
                )
            session.commit()

        reads = []

        def read(since):
            reads.append(since)
            return WatchedRead(items=ordinary, covers_window=True)

        with sessions() as session:
            cache.sync_section(
                session, profile(), user_id, SECTION, MediaType.MOVIE, read, force_full=True, reconcile=True
            )
            session.commit()

        assert len(reads) == 1, "the guard fired on an account that had merely been transferred to"
        with sessions() as session:
            assert session.query(WatchedTitle).count() == 10, "nothing was gone, so nothing should go"

    def test_a_confirming_read_that_RAISES_keeps_the_cached_titles(self, sessions, user_id):
        """The likeliest real outcome of the second read: a full library re-read failing against a
        PMS that just answered short. That is evidence against the first answer, not for it."""
        cache = WatchCache(sessions)
        full = [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2), watched("Alien", tmdb_id=3)]
        sync(cache, sessions, user_id, full)

        calls = []

        def read(since):
            calls.append(since)
            if len(calls) > 1:
                raise TimeoutError("PMS went away")
            return WatchedRead(items=[], covers_window=True)

        with sessions() as session:
            cache.sync_section(
                session, profile(), user_id, SECTION, MediaType.MOVIE, read, force_full=True, reconcile=True
            )
            session.commit()

        assert len(calls) == 2
        with sessions() as session:
            assert session.query(WatchedTitle).count() == 3

    def test_a_confirming_read_with_no_coverage_claim_keeps_the_cached_titles(self, sessions, user_id):
        """A bare list carries no coverage claim, so it cannot corroborate a mass deletion — and it
        is what every test double and any non-`WatchedRead` reader hands back."""
        cache = WatchCache(sessions)
        full = [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2), watched("Alien", tmdb_id=3)]
        sync(cache, sessions, user_id, full)

        answers = [WatchedRead(items=[], covers_window=True), []]

        with sessions() as session:
            cache.sync_section(
                session,
                profile(),
                user_id,
                SECTION,
                MediaType.MOVIE,
                lambda since: answers.pop(0),
                force_full=True,
                reconcile=True,
            )
            session.commit()

        with sessions() as session:
            assert session.query(WatchedTitle).count() == 3

    def test_a_section_halving_exactly_is_below_the_bar(self, sessions, user_id):
        """The boundary. `len(items) * 2 < cached` — 2 of 4 is not MORE than half gone, so it needs
        no second read. Pinned because this is the one place an off-by-one would live."""
        cache = WatchCache(sessions)
        full = [watched(t, tmdb_id=i) for i, t in enumerate(["Heat", "Dune", "Alien", "Solaris"], start=1)]
        sync(cache, sessions, user_id, full)

        reads = []

        def read(since):
            reads.append(since)
            return WatchedRead(items=full[:2], covers_window=True)

        with sessions() as session:
            cache.sync_section(
                session, profile(), user_id, SECTION, MediaType.MOVIE, read, force_full=True, reconcile=True
            )
            session.commit()

        assert len(reads) == 1, "an exact halving triggered a confirming read"
        with sessions() as session:
            assert {r.title for r in session.query(WatchedTitle).all()} == {"Heat", "Dune"}

    def test_a_small_shrink_needs_no_confirmation(self, sessions, user_id):
        """One or two un-watches is the ordinary case and must not cost a second request every time."""
        cache = WatchCache(sessions)
        full = [watched("Heat", tmdb_id=1), watched("Dune", tmdb_id=2), watched("Alien", tmdb_id=3)]
        sync(cache, sessions, user_id, full)

        reads = []

        def read(since):
            reads.append(since)
            return WatchedRead(items=full[:2], covers_window=True)

        with sessions() as session:
            cache.sync_section(
                session, profile(), user_id, SECTION, MediaType.MOVIE, read, force_full=True, reconcile=True
            )
            session.commit()

        assert len(reads) == 1, "a routine un-watch triggered a confirming read"
        with sessions() as session:
            assert {r.title for r in session.query(WatchedTitle).all()} == {"Heat", "Dune"}

    def test_an_incremental_read_keeps_what_it_did_not_ask_about(self, sessions, user_id):
        """The opposite failure: an incremental top-up must not be mistaken for the whole truth. It
        merges into what is already cached; only titles inside the window it actually covered are
        ever removed."""
        cache = WatchCache(sessions)
        library = [watched("Heat", tmdb_id=1, days_ago=30), watched("Dune", tmdb_id=2, days_ago=2)]
        sync_pms(cache, sessions, user_id, library)

        library.append(watched("Arrival", tmdb_id=3, days_ago=1))
        asked: list[datetime | None] = []
        sync_pms(cache, sessions, user_id, library, capture=asked)

        assert asked[0] is not None and asked[0] > library[0].watched_at, "Heat sits outside the window"
        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Heat", "Dune", "Arrival"}

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
        sync_pms(cache, sessions, user_id, everything[:6])
        sync_pms(cache, sessions, user_id, everything)
        with sessions() as session:
            incremental = {item.tmdb_id for item in cache.watched_set(session, user_id)}

        with sessions() as session:
            session.query(WatchedTitle).delete()
            session.query(WatchSyncState).delete()
            session.commit()
        sync_pms(cache, sessions, user_id, everything, force_full=True)
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


class TestUnwatching:
    """People un-watch things — a mis-click, the kid's play on the wrong profile, a reset for a
    rewatch. Removal happens at three scopes and none of them subsumes the next, so each is pinned
    here separately."""

    def test_an_incremental_read_drops_a_title_un_watched_inside_its_window(self, sessions, user_id):
        """The read covers its window completely, so a cached title it did not return is not watched."""
        cache = WatchCache(sessions)
        library = [watched("Heat", tmdb_id=1, days_ago=2), watched("Dune", tmdb_id=2, days_ago=2)]
        sync_pms(cache, sessions, user_id, library)

        library.pop()  # Dune un-watched: no lastViewedAt any more, so the walk cannot return it
        sync_pms(cache, sessions, user_id, library)

        with sessions() as session:
            assert [row.title for row in session.query(WatchedTitle).all()] == ["Heat"]

    def test_an_un_watch_older_than_the_cursor_waits_for_the_full_read(self, sessions, user_id):
        """The limit of any incremental scheme, pinned so nobody later mistakes it for a bug: nothing
        in a response covering the last day points at a title watched a month ago."""
        cache = WatchCache(sessions)
        library = [watched("Heat", tmdb_id=1, days_ago=30), watched("Dune", tmdb_id=2, days_ago=1)]
        sync_pms(cache, sessions, user_id, library)

        library.pop(0)
        sync_pms(cache, sessions, user_id, library)

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Heat", "Dune"}

        sync_pms(cache, sessions, user_id, library, force_full=True, reconcile=True)

        with sessions() as session:
            assert [row.title for row in session.query(WatchedTitle).all()] == ["Dune"]

    def test_a_title_with_no_watch_timestamp_is_never_dropped_by_the_window(self, sessions, user_id):
        """A title Plex reports with no `lastViewedAt` is stamped 1970 and SKIPPED by the incremental
        walk — so it is absent from every incremental response without having been un-watched. 1970
        sits outside every window, which is the only thing stopping it being deleted on sight."""
        cache = WatchCache(sessions)
        no_stamp = WatchedItem(
            title="Solaris",
            media_type=MediaType.MOVIE,
            watched_at=datetime(1970, 1, 1, tzinfo=UTC),
            tmdb_id=9,
            rating_key=9,
        )
        sync(cache, sessions, user_id, [no_stamp, watched("Heat", tmdb_id=1, days_ago=1)])

        # The reader is right to omit it: the walk skips a no-timestamp title rather than returning it.
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=1)])

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Solaris", "Heat"}

    def test_a_title_outside_the_window_survives_a_read_that_returns_nothing(self, sessions, user_id):
        """The delete must be scoped to the window, not to 'everything the read didn't mention'."""
        cache = WatchCache(sessions)
        sync(
            cache,
            sessions,
            user_id,
            [watched("Heat", tmdb_id=1, days_ago=90), watched("Dune", tmdb_id=2, days_ago=1)],
        )

        sync(cache, sessions, user_id, [])

        with sessions() as session:
            titles = {row.title for row in session.query(WatchedTitle).all()}
        assert titles == {"Heat"}, "the old title was outside the window and must survive"

    def test_a_read_that_cannot_prove_it_covered_the_window_deletes_nothing(self, sessions, user_id):
        """The guard that keeps a truncated read from reading as a mass un-watch.

        A walk that stopped early looks identical from here: titles are simply absent. On a PMS that
        omits `totalSize` and caps the container below our page size, an ordinary quiet night would
        otherwise delete every cached title in the window. Unproven coverage tops up and removes
        nothing — a stale row, never a deleted one.
        """
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=2), watched("Dune", tmdb_id=2, days_ago=2)])

        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=1)], covers_window=False)

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Heat", "Dune"}

    def test_a_reader_that_makes_no_coverage_claim_deletes_nothing(self, sessions, user_id):
        """A reader returning a bare list — a test double, or any future source that isn't the PMS
        client — has claimed nothing about how much of the window it read. Silently treating that as
        full coverage is how this delete path would get re-enabled by accident."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=2), watched("Dune", tmdb_id=2, days_ago=2)])

        with sessions() as session:
            cache.sync_section(
                session,
                profile(),
                user_id,
                SECTION,
                MediaType.MOVIE,
                lambda since: [watched("Heat", tmdb_id=1, days_ago=1)],
            )
            session.commit()

        with sessions() as session:
            assert {row.title for row in session.query(WatchedTitle).all()} == {"Heat", "Dune"}

    def test_one_section_un_watch_does_not_touch_another(self, sessions, user_id):
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1, days_ago=1)], section="1")
        sync(cache, sessions, user_id, [watched("Dune", tmdb_id=2, days_ago=1)], section="2")

        sync(cache, sessions, user_id, [], section="1")

        with sessions() as session:
            assert [row.title for row in session.query(WatchedTitle).all()] == ["Dune"]


class TestDeadLibraries:
    """A library removed from the server is swept by nothing else: the periodic full read only ever
    replaces sections it successfully READ."""

    def test_a_library_no_longer_on_the_server_is_forgotten(self, sessions, user_id):
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)], section="1")
        sync(cache, sessions, user_id, [watched("Dune", tmdb_id=2)], section="2")

        with sessions() as session:
            dropped = cache.forget_dead_sections(session, user_id, {"1"})
            session.commit()

        assert dropped == 1
        with sessions() as session:
            assert [row.title for row in session.query(WatchedTitle).all()] == ["Heat"]
            assert [state.section_key for state in session.query(WatchSyncState).all()] == ["1"]

    def test_the_cursor_goes_with_the_titles(self, sessions, user_id):
        """Dropping rows but keeping the cursor would leave `needs_full` answering False against an
        empty cache, so a library that came back would stay thin until its next scheduled full read."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Dune", tmdb_id=2)], section="2")
        with sessions() as session:
            assert cache.needs_full(session, user_id, "2") is False

            cache.forget_dead_sections(session, user_id, {"1"})
            session.commit()

        with sessions() as session:
            assert cache.needs_full(session, user_id, "2") is True

    def test_an_empty_library_list_is_treated_as_a_blip_not_an_empty_server(self, sessions, user_id):
        """Acting on it would wipe every cached watch for everyone on one bad PMS response."""
        cache = WatchCache(sessions)
        sync(cache, sessions, user_id, [watched("Heat", tmdb_id=1)])

        with sessions() as session:
            assert cache.forget_dead_sections(session, user_id, set()) == 0
            session.commit()

        with sessions() as session:
            assert session.query(WatchedTitle).count() == 1


class TestUserRatingSurvivesTheCache:
    """The rating has to make it PMS -> cache -> engine, or the feature silently does nothing.

    Everything else about issue #69 is tested on in-memory items. This is the one seam where a
    correct engine and a correct client can still add up to nothing: the cache is what the run
    actually reads from, and a column that is written but never read back (or read but never
    written) fails invisibly — every rating would simply look absent, which is indistinguishable
    from the 99.7% of titles nobody rated.
    """

    def _rated(self, title: str, *, tmdb_id: int, rating: float | None) -> WatchedItem:
        item = watched(title, tmdb_id=tmdb_id)
        return WatchedItem(**{**item.__dict__, "user_rating": rating})

    def test_a_rating_written_by_a_sync_is_read_back_by_the_engine(self, sessions, user_id):
        cache = WatchCache(sessions)

        sync_pms(cache, sessions, user_id, [self._rated("Hated It", tmdb_id=1, rating=2.0)])

        with sessions() as session:
            (item,) = cache.watched_set(session, user_id)
        assert item.user_rating == 2.0
        assert item.is_human_rating is True

    def test_an_unrated_title_reads_back_as_unrated_rather_than_zero(self, sessions, user_id):
        """0.0 is a rating someone can give, so the round trip must preserve None as None."""
        cache = WatchCache(sessions)

        sync_pms(cache, sessions, user_id, [self._rated("Never Rated", tmdb_id=1, rating=None)])

        with sessions() as session:
            (item,) = cache.watched_set(session, user_id)
        assert item.user_rating is None

    def test_changing_a_rating_updates_the_cached_row(self, sessions, user_id):
        """An upsert, not an insert — otherwise the first rating a person ever gives is permanent."""
        cache = WatchCache(sessions)
        sync_pms(cache, sessions, user_id, [self._rated("Changed My Mind", tmdb_id=1, rating=2.0)])

        sync_pms(
            cache,
            sessions,
            user_id,
            [self._rated("Changed My Mind", tmdb_id=1, rating=10.0)],
            force_full=True,
        )

        with sessions() as session:
            (item,) = cache.watched_set(session, user_id)
        assert item.user_rating == 10.0

    def test_withdrawing_a_rating_clears_it(self, sessions, user_id):
        """The case a guarded write would break. Someone thumbs-downs a title, then removes the
        rating in Plex — if the cache only wrote non-None values, the withdrawn judgement would keep
        shaping their row for ever and nothing on any screen would explain why."""
        cache = WatchCache(sessions)
        sync_pms(cache, sessions, user_id, [self._rated("Took It Back", tmdb_id=1, rating=2.0)])

        sync_pms(cache, sessions, user_id, [self._rated("Took It Back", tmdb_id=1, rating=None)], force_full=True)

        with sessions() as session:
            (item,) = cache.watched_set(session, user_id)
        assert item.user_rating is None, "an un-rating must clear the column, not be skipped"


class TestLibraryNameIsCached:
    """The library's DISPLAY name is cached beside its key, because nothing else can supply it later.

    The watched page groups a title's library copies into one row and names the libraries on it
    (issue #111), and that page is deliberately a pure DB read — it never talks to Plex. So if the
    sync doesn't record the name, no later read can recover it.
    """

    def _names(self, sessions, user_id) -> list[str]:
        with sessions() as session:
            return [row.library for row in session.query(WatchedTitle).filter(WatchedTitle.user_id == user_id)]

    def test_the_name_the_sync_was_given_lands_on_the_row(self, sessions, user_id):
        cache = WatchCache(sessions)

        sync_pms(cache, sessions, user_id, [watched("Dune", tmdb_id=1)], name="4K Movies")

        assert self._names(sessions, user_id) == ["4K Movies"]

    def test_renaming_the_library_in_plex_updates_the_cached_name(self, sessions, user_id):
        """The name is Plex's to change, and a stale one would mislabel every row from that library."""
        cache = WatchCache(sessions)
        sync_pms(cache, sessions, user_id, [watched("Dune", tmdb_id=1)], name="4K Movies")

        sync_pms(cache, sessions, user_id, [watched("Dune", tmdb_id=1)], name="Movies (4K)", force_full=True)

        assert self._names(sessions, user_id) == ["Movies (4K)"]

    def test_a_sync_that_does_not_know_the_name_leaves_the_recorded_one_alone(self, sessions, user_id):
        """Writing "" unconditionally would let any caller without a name blank one already on record,
        and the page would lose the library line until the next sync put it back."""
        cache = WatchCache(sessions)
        sync_pms(cache, sessions, user_id, [watched("Dune", tmdb_id=1)], name="4K Movies")

        sync_pms(cache, sessions, user_id, [watched("Dune", tmdb_id=1)], force_full=True)

        assert self._names(sessions, user_id) == ["4K Movies"]

    def test_an_incremental_read_renames_only_the_rows_it_returned(self, sessions, user_id):
        """The other half of the rename matrix, stated rather than implied away.

        `_upsert` only rewrites rows the read RETURNED, and an incremental read stops at the cursor —
        so a title watched before it keeps the old library name while a recent one gets the new one,
        and the same library briefly appears under two names in the filter. It has a one-sync ceiling
        in production, because every real sync reads FULL (issue #108: `watch_sync.refresh_watched`
        and `prefill_history` both pass `force_full=True`), which is the case the test above covers.
        Pinned so nobody reads that test as a promise this path does not make.
        """
        cache = WatchCache(sessions)
        old_watch = watched("Watched Last Month", tmdb_id=1, days_ago=30)
        recent = watched("Watched Yesterday", tmdb_id=2, days_ago=1)
        sync_pms(cache, sessions, user_id, [old_watch, recent], name="4K Movies")

        sync_pms(cache, sessions, user_id, [old_watch, recent], name="Movies (4K)")

        with sessions() as session:
            names = {
                row.title: row.library for row in session.query(WatchedTitle).filter(WatchedTitle.user_id == user_id)
            }
        assert names == {"Watched Last Month": "4K Movies", "Watched Yesterday": "Movies (4K)"}

    def test_a_row_cached_before_the_name_existed_gets_one_on_the_next_sync(self, sessions, user_id):
        """The upgrade path: 0087 backfills nothing, because the name lives on the PMS."""
        cache = WatchCache(sessions)
        sync_pms(cache, sessions, user_id, [watched("Dune", tmdb_id=1)])
        assert self._names(sessions, user_id) == [""]

        sync_pms(cache, sessions, user_id, [watched("Dune", tmdb_id=1)], name="Movies", force_full=True)

        assert self._names(sessions, user_id) == ["Movies"]
