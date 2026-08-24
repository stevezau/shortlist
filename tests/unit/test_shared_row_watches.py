"""Shared rows count too — the owner's requirement, verbatim: "if they start something and its on a
current row of theirs (or shared)".

A shared row is built once for the whole server and writes NO `picks` rows, so before
`shared_row_watches` (migration 0078) every watch off a shared row was invisible: the credit path
intersected plays against the titles a person had a pick row for, and a shared row contributes none.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, literal
from sqlalchemy.orm import sessionmaker

from shortlist.engine.models import MediaType, UserProfile, UserType, WatchedItem
from shortlist.server.db.models import (
    Base,
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    Delivery,
    PickRow,
    Run,
    RunSharedRow,
    SharedRowWatch,
    User,
    WatchSession,
)
from shortlist.server.services.report_service import (
    _PERSON_TITLE,
    _SHARED_PERSON_TITLE,
    resolve_outcomes,
)
from shortlist.server.services.run_persistence import _shared_audience, reconcile_watched
from shortlist.server.services.watch_events import RowMembership, shared_credits

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


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


def an_old_pick_so_the_attribution_floor_reaches_back(sessions):
    """`_attribution_floor` is the oldest pick we hold, and NOTHING before it is ever scanned. Without
    a pick this old, a play from five days ago is dropped by the floor rather than by the rule under
    test — which is exactly how an earlier version of the "play predates the row" test below passed
    with the membership gate deleted."""
    with sessions() as s:
        s.add(
            PickRow(
                run_id=1,
                user_id=2,
                collection_slug="ancient",
                section_key="1",
                library="Movies",
                tmdb_id=111,
                media_type="movie",
                rating_key=111,
                rank=1,
                created_at=NOW - timedelta(days=60),
            )
        )
        s.commit()


def _request(sessions):
    """The least a FastAPI handler here needs: `request.app.state.sessions`."""
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(sessions=sessions)))


def profile(history=(), *, slug="alex", account=99):
    return UserProfile(
        username=slug, plex_account_id=account, user_type=UserType.SHARED, slug=slug, history=list(history)
    )


class TestASharedRowCanBeCredited:
    def test_a_start_on_a_shared_row_is_credited_with_no_pick_row_of_their_own(self, world):
        """The headline case, and the one that was silently impossible before."""
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            row = s.query(SharedRowWatch).filter_by(user_id=1, collection_slug="staff", tmdb_id=550).one()
            assert row.watched_at is not None
            assert row.max_percent == 30
            assert row.title == "Fight Club"
            # And it did NOT invent a pick row for them.
            assert s.query(PickRow).filter_by(user_id=1).count() == 0

    def test_a_partial_watch_that_finishes_later_keeps_its_original_credit(self, world):
        """The owner's rule: "if they are partial and they finish it later keep counting that as they
        initial started form the row"."""
        a_pick_so_the_rating_key_resolves(world)
        started = NOW - timedelta(hours=6)
        watch_session(world, 99, started=started, offset=1_200_000)
        reconcile_watched(world, [profile()])

        with world() as s:
            first_credit = s.query(SharedRowWatch).filter_by(user_id=1, tmdb_id=550).one().watched_at

        # Four days later they finish it — by which time the row has moved on.
        finish = [WatchedItem(title="Fight Club", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=550)]
        reconcile_watched(world, [profile(finish)])

        with world() as s:
            row = s.query(SharedRowWatch).filter_by(user_id=1, tmdb_id=550).one()
            assert row.watched_at == first_credit, "the credit belongs to when the row got them to press play"
            assert row.finished_at is not None

    def test_it_reaches_the_report(self, world):
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=180_000)  # 3% — a bounce
        reconcile_watched(world, [profile()])

        with world() as s:
            outcomes = resolve_outcomes(s, None)
            assert outcomes[(1, 550, "movie")]["outcome"] == "bounced"
            assert outcomes[(1, 550, "movie")]["row"] == "staff"


class TestASharedRowIsStillBounded:
    """Everyone sees a shared row, so a credit here is easier to earn than a personal one. These are
    the bounds that stop it becoming "count every watch on the server"."""

    def test_a_play_before_the_row_carried_the_title_is_not_credited(self, world):
        a_pick_so_the_rating_key_resolves(world)
        an_old_pick_so_the_attribution_floor_reaches_back(world)
        watch_session(world, 99, started=NOW - timedelta(days=5), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0, "the row did not exist yet — it cannot be why"

    def test_someone_outside_the_audience_is_not_credited(self, world):
        """A subset shared row is only visible to its audience, and the AUDIENCE SNAPSHOT on the run is
        what decides — not `collection_audience`, which is current state and would retroactively
        credit watches from before someone was added."""
        with world() as s:
            s.query(RunSharedRow).filter_by(run_id=1).one().audience = [99]
            s.add(CollectionAudience(collection_id=1, user_id=2))
            s.commit()
        a_pick_so_the_rating_key_resolves(world, rating_key=9001)
        watch_session(world, 77, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile(slug="sam", account=77)])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0, "sam could not see the row when it was delivered"

    def test_a_row_no_longer_on_plex_stops_crediting(self, world):
        """The delivery ledger is the on-Plex gate. Without it a shared row deleted a month ago keeps
        crediting for ever, because its last delivery stays the newest one in the timeline."""
        with world() as s:
            s.query(Delivery).filter_by(collection_slug="staff").delete()
            s.commit()
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0

    def test_a_disabled_row_stops_crediting(self, world):
        with world() as s:
            s.query(Collection).filter_by(slug="staff").one().enabled = False
            s.commit()
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0

    def test_running_twice_writes_one_row_and_does_not_move_the_credit(self, world):
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])
        with world() as s:
            before = s.query(SharedRowWatch).one().watched_at

        reconcile_watched(world, [profile()])

        with world() as s:
            rows = s.query(SharedRowWatch).all()
            assert len(rows) == 1
            assert rows[0].watched_at == before


class TestSeriesProgressIsNotWritten:
    def test_an_episode_percentage_is_never_recorded_as_the_series(self, world):
        """Same rule as `picks.max_percent` (migration 0077): one full episode of a sixty-episode
        series is not 100% of the series, and recording it as such told the dashboard people abandon
        shows just before the end."""
        with world() as s:
            s.query(RunSharedRow).filter_by(run_id=1).one().picks = [
                {"tmdb_id": 1399, "media_type": "show", "title": "Game of Thrones"}
            ]
            s.add(
                PickRow(
                    run_id=1,
                    user_id=2,
                    collection_slug="other",
                    section_key="1",
                    library="TV",
                    tmdb_id=1399,
                    media_type="show",
                    rating_key=7001,
                    rank=1,
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.commit()
        with world() as s:
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="ep",
                    rating_key=7002,
                    show_rating_key=7001,
                    media_type="episode",
                    started_at=NOW - timedelta(hours=3),
                    last_seen_at=NOW - timedelta(hours=2),
                    ended_at=NOW - timedelta(hours=2),
                    max_offset_ms=3_000_000,
                    duration_ms=3_000_000,
                    end_reason="stopped",
                )
            )
            s.commit()

        reconcile_watched(world, [profile()])

        with world() as s:
            row = s.query(SharedRowWatch).filter_by(tmdb_id=1399).one()
            assert row.watched_at is not None, "the start still credits — they pressed play from the row"
            assert row.max_percent is None, "one episode is not a percentage of the series"


class TestTheTwoPathsDoNotCrossCredit:
    def test_a_shared_row_does_not_stamp_a_personal_pick(self, world):
        """The reason the two are separate tables. If a shared row could satisfy PERSONAL membership,
        someone's own row would be credited for a title it had already dropped, because a different
        row was still showing it."""
        with world() as s:
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=600))
            s.add(Collection(id=2, slug="mine", name="Mine", enabled=True))
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
                    # Dropped from their personal row before the play below.
                    created_at=NOW - timedelta(days=30),
                )
            )
            s.add(Run(id=2, trigger="schedule", status="ok", started_at=NOW - timedelta(hours=12)))
            s.add(
                PickRow(
                    run_id=2,
                    user_id=1,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=999,
                    media_type="movie",
                    rating_key=9999,
                    rank=1,
                    created_at=NOW - timedelta(hours=12),
                )
            )
            s.commit()
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            personal = s.query(PickRow).filter_by(user_id=1, tmdb_id=550).one()
            assert personal.watched_at is None, "their own row had already dropped it"
            assert s.query(SharedRowWatch).filter_by(tmdb_id=550).count() == 1

    def test_one_title_on_both_kinds_of_row_is_one_outcome_in_the_report(self, world):
        """`resolve_outcomes` promises one outcome per person-title. Two tallies would double-count."""
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
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.commit()
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            keys = [k for k in resolve_outcomes(s, None) if k[1] == 550]
            assert keys == [(1, 550, "movie")], "one person-title, one outcome"


class TestSharedCreditsDirectly:
    def test_no_shared_rows_means_no_work(self, sessions):
        with sessions() as s:
            s.add(User(id=1, plex_account_id=99, username="alex", slug="alex"))
            s.commit()
        with sessions() as s:
            assert shared_credits(s, RowMembership(s)) == {}


class TestTheCreditIsTheEarliestPlay:
    def test_a_rewatch_does_not_move_the_credit_forward(self, world):
        """The credit belongs to the moment the row got them to press play. Taking the newest event
        would file the hit in the wrong week of the trend chart for ever."""
        a_pick_so_the_rating_key_resolves(world)
        first = NOW - timedelta(hours=20)
        watch_session(world, 99, started=first, offset=600_000)
        watch_session(world, 99, started=NOW - timedelta(hours=2), offset=3_000_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            row = s.query(SharedRowWatch).filter_by(tmdb_id=550).one()
            assert row.watched_at.replace(tzinfo=UTC) == first
            assert row.max_percent == 50, "progress is the FURTHEST any sitting got, not the earliest"


class TestPicksWrittenBeforeMediaTypeWasCarried:
    """The shape EVERY shared row on the maintainer's server carried when this shipped.

    `_pick_dicts` did not write `media_type`, so the pool keyed `(tmdb_id, "")` while the play log
    resolves to `(tmdb_id, "movie")` — the two never intersected and shared crediting was a silent
    no-op on real data. Every test above passed because they all set the field by hand. Found by
    probing the live server, which is the only place the real shape existed.
    """

    def a_row_with_no_media_type(self, sessions, *, rating_key):
        with sessions() as s:
            s.query(RunSharedRow).filter_by(run_id=1).one().picks = [
                {"tmdb_id": 550, "rating_key": rating_key, "rank": 1, "title": "Fight Club"}
            ]
            s.commit()

    def test_a_pick_with_no_media_type_credits_nothing(self, world):
        """A guess would be worse than silence: a tmdb id with the wrong type is a DIFFERENT title, so
        guessing credits the row for something nobody watched.

        Deriving the type from the pick's own rating key was tried and reverted — see `_shared_key`.
        The rows it rescued were precisely the rows with no audience snapshot, which is the hole the
        next test describes."""
        a_pick_so_the_rating_key_resolves(world)  # rating key 9001 IS resolvable — irrelevant here
        self.a_row_with_no_media_type(world, rating_key=9001)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0

    def test_a_subset_row_with_no_audience_snapshot_credits_nobody(self, world):
        """The reason the rating-key fallback had to go, pinned on its own.

        A row written before migration 0076 has `audience = NULL`, and `_shared_visible_to` reads NULL
        as "everyone" — it cannot tell "public" from "not recorded". So any path that makes a
        pre-0076 row creditable hands a SUBSET row's credits to people who were never in its audience.
        Here the row is alex-only and sam is the one who watches."""
        with world() as s:
            row = s.query(RunSharedRow).filter_by(run_id=1).one()
            row.audience = None  # pre-0076: not recorded
            row.picks = [{"tmdb_id": 550, "rating_key": 9001, "rank": 1, "title": "Fight Club"}]
            s.query(Collection).filter_by(slug="staff").one().audience = "subset"
            s.add(CollectionAudience(collection_id=1, user_id=1))  # alex only
            s.commit()
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 77, started=NOW - timedelta(hours=3), offset=1_800_000)  # sam

        reconcile_watched(world, [profile(slug="sam", account=77)])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0, "sam was never in this row's audience"

    def test_a_movie_and_a_show_sharing_a_tmdb_id_do_not_collide(self, world):
        """TMDB ids are namespaced per type. If the type were dropped, one watch would credit both."""
        with world() as s:
            s.query(RunSharedRow).filter_by(run_id=1).one().picks = [
                {"tmdb_id": 1399, "media_type": "movie", "rating_key": 8001, "title": "A Film"},
                {"tmdb_id": 1399, "media_type": "show", "rating_key": 8002, "title": "A Series"},
            ]
            s.add(
                PickRow(
                    run_id=1,
                    user_id=2,
                    collection_slug="other",
                    section_key="1",
                    library="Movies",
                    tmdb_id=1399,
                    media_type="movie",
                    rating_key=8001,
                    rank=1,
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.commit()
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000, rating_key=8001)

        reconcile_watched(world, [profile()])

        with world() as s:
            credited = s.query(SharedRowWatch).all()
            assert [(r.tmdb_id, r.media_type) for r in credited] == [(1399, "movie")]


class TestEachRowKeepsItsOwnEarliestPlay:
    def test_a_row_is_not_credited_for_a_play_made_before_it_carried_the_title(self, world):
        """Two shared rows, the same title, different windows, one person playing in both.

        An earlier structure kept ONE timestamp per title and unioned the row slugs, so the later row
        inherited the earlier play's date — credited for a play made before it carried the title."""
        with world() as s:
            s.add(Collection(id=2, slug="trending", name="Trending", enabled=True, build="shared"))
            s.add(Delivery(collection_slug="trending", user_slug="shared_trending", library_key="1", rating_key=501))
            s.add(Run(id=2, trigger="schedule", status="ok", started_at=NOW - timedelta(hours=6)))
            # `staff` has carried it since yesterday; `trending` only picked it up six hours ago.
            s.add(
                RunSharedRow(
                    run_id=2,
                    collection_slug="trending",
                    row_title="Trending",
                    status="ok",
                    picks=[{"tmdb_id": 550, "media_type": "movie", "rating_key": 9001, "title": "Fight Club"}],
                    audience=None,
                )
            )
            s.commit()
        a_pick_so_the_rating_key_resolves(world)
        early = NOW - timedelta(hours=20)  # only `staff` was showing it
        late = NOW - timedelta(hours=2)  # both were
        watch_session(world, 99, started=early, offset=600_000)
        watch_session(world, 99, started=late, offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            by_slug = {r.collection_slug: r.watched_at.replace(tzinfo=UTC) for r in s.query(SharedRowWatch).all()}
            assert by_slug["staff"] == early
            assert by_slug["trending"] == late, "trending did not carry it at the early play"


class TestTheExpensiveMapsAreBuiltOnce:
    def test_one_reconcile_builds_the_rating_key_map_once(self, world, monkeypatch):
        """`tmdb_by_rating_key` is a DISTINCT over the largest table in the schema — 158,737 pick rows
        on a real server — and `_scan_plays` walks the whole event log. Both were being rebuilt up to
        five times per pass, seven passes a day, for byte-identical results."""
        from shortlist.server.services import run_persistence, watch_events

        calls = {"map": 0, "scan": 0}
        real_map, real_scan = watch_events.tmdb_by_rating_key, watch_events._scan_plays

        def counted_map(*a, **k):
            calls["map"] += 1
            return real_map(*a, **k)

        def counted_scan(*a, **k):
            calls["scan"] += 1
            return real_scan(*a, **k)

        for mod in (watch_events, run_persistence):
            monkeypatch.setattr(mod, "tmdb_by_rating_key", counted_map, raising=False)
            monkeypatch.setattr(mod, "_scan_plays", counted_scan, raising=False)

        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])

        assert calls["map"] == 1, f"rating-key map rebuilt {calls['map']}x"
        assert calls["scan"] == 1, f"play scan rebuilt {calls['scan']}x"


class TestTheTilesCannotContradictEachOther:
    """`overall.dropped` came off `resolve_outcomes` (which includes shared rows) while
    `overall.watched` read `picks` alone — so a shared-only bounce landed in Dropped and in nothing
    else, and the Dropped tile could exceed the Watched tile beside it: an abandonment with no start.
    """

    def a_shared_only_bounce(self, world):
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=180_000)  # 3%
        reconcile_watched(world, [profile()])

    def test_a_shared_only_bounce_is_counted_as_watched_too(self, world):
        from shortlist.server.services.report_service import _watched_count, _watchers_count

        self.a_shared_only_bounce(world)

        with world() as s:
            watched = _watched_count(s, None)
            dropped_and_bounced = sum(
                1 for e in resolve_outcomes(s, None).values() if e["outcome"] in ("bounced", "dropped")
            )
            assert watched >= dropped_and_bounced, "the Dropped tile must never exceed the Watched tile"
            assert watched == 1
            assert _watchers_count(s, None) == 1

    def test_a_title_on_both_kinds_of_row_is_counted_once(self, world):
        """A UNION, not a sum — adding two counts would report one person-title twice."""
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
                    rating_key=9001,
                    rank=1,
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.commit()
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])

        with world() as s:
            assert _watched_count(s, None) == 1

    def test_it_appears_in_the_recently_watched_feed(self, world):
        """The feature this whole thread started from: "does Recently watched actually check the
        title was in their row"."""
        from shortlist.engine.models import DEFAULT_ROW_TEMPLATE
        from shortlist.server.services.report_service import _recent_watches, _RowNamer

        self.a_shared_only_bounce(world)

        with world() as s:
            users = {u.id: u for u in s.query(User).all()}
            feed = _recent_watches(s, users, _RowNamer(s, DEFAULT_ROW_TEMPLATE), None)
            assert [(f["username"], f["title"]) for f in feed] == [("alex", "Fight Club")]


class TestTheBreakdownsAgreeWithTheTiles:
    """`_watched_count` was widened to include shared rows while `per_user`/`per_row` were not — so
    "People watching: 4" could sit above a By-person section where every bar read 0."""

    def test_a_shared_only_watch_appears_under_that_person(self, world):
        """Through `effectiveness`, not a hand-written `_counts` call. Calling `_counts` directly with
        the shared arguments spelled out in the test re-implements the wiring instead of exercising
        it: drop those arguments at the call site and such a test still passes."""
        from shortlist.server.services.report_service import effectiveness

        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])

        with world() as s:
            line = next(p for p in effectiveness(s, "all")["per_user"] if p["username"] == "alex")
            assert line["watched"] == 1
            assert line["delivered"] == 0, "a shared row has no per-person delivery — the UI hides a zero"

    def test_a_shared_only_watch_appears_under_that_row(self, world):
        from shortlist.server.services.report_service import _counts

        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])

        with world() as s:
            per_row = _counts(
                s,
                [PickRow.collection_slug, PickRow.section_key, PickRow.library],
                _PERSON_TITLE,
                None,
                [SharedRowWatch.collection_slug, literal("").label("section_key"), literal("").label("library")],
                _SHARED_PERSON_TITLE,
            )
            assert per_row[("staff", "", "")][1] == 1

    def test_one_title_on_both_kinds_of_row_is_not_double_counted_for_a_person(self, world):
        """A SUM would report this person as watching two titles. The union counts the pair once."""
        from shortlist.server.services.report_service import effectiveness

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
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.commit()
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])

        with world() as s:
            line = next(p for p in effectiveness(s, "all")["per_user"] if p["username"] == "alex")
            assert line["watched"] == 1, "one person, one title, one watch"


class TestTheAudienceGateBothWays:
    """The gate only became load-bearing in this change — before it, the audience map was built and
    read by nothing. Both tested cells produced `count() == 0`, which a correct gate and a completely
    broken one produce alike. These pin the POSITIVE cells, so an id-space swap fails loudly."""

    def a_subset_row_containing(self, world, *account_ids):
        with world() as s:
            row = s.query(RunSharedRow).filter_by(run_id=1).one()
            row.audience = list(account_ids)
            s.query(Collection).filter_by(slug="staff").one().audience = "subset"
            s.commit()

    def test_someone_inside_the_audience_is_credited(self, world):
        """The cell that catches an id-space swap: `_shared_visible_to` compares
        `user.plex_account_id` against what `_shared_audience` snapshots. Store `users.id` on either
        side and the exclude test still passes while everyone silently stops being credited."""
        self.a_subset_row_containing(world, 99)  # alex's PLEX ACCOUNT id, not his user id
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(SharedRowWatch).filter_by(user_id=1, tmdb_id=550).count() == 1

    def test_a_public_row_muted_for_one_person_does_not_credit_them(self, world):
        """Muting is the OTHER way someone does not get a row, and it applies to a public row too —
        so `_shared_audience` snapshots the audience MINUS the mutes, not the audience alone."""
        with world() as s:
            s.add(CollectionUserOverride(collection_id=1, user_id=1, muted=True))
            s.commit()
        # Re-snapshot the way a run would, so the stored audience reflects the mute.
        with world() as s:
            s.query(RunSharedRow).filter_by(run_id=1).one().audience = _shared_audience(s, "staff")
            s.commit()
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0, "they switched this row off"


class TestTheDatabaseBranchesForSharedRows:
    def a_credit_on(self, world, slug):
        with world() as s:
            s.add(
                SharedRowWatch(
                    user_id=1,
                    collection_slug=slug,
                    tmdb_id=550,
                    media_type="movie",
                    title="Fight Club",
                    watched_at=NOW - timedelta(hours=1),
                )
            )
            s.commit()

    def test_a_deleted_shared_row_is_offered_for_clearing(self, world):
        from shortlist.server.api.report import _orphaned_slugs

        self.a_credit_on(world, "gone")  # no Collection with this slug

        with world() as s:
            assert "gone" in _orphaned_slugs(s)
            assert "staff" not in _orphaned_slugs(s), "a LIVE shared row is not an orphan"

    def test_the_endpoint_lists_a_shared_only_orphan_with_its_number(self, world):
        """Through the real handler. Counting the records is the endpoint's job — a line offering
        "0" to clear is the same as not offering it."""
        from shortlist.server.api.report import deleted_rows

        self.a_credit_on(world, "gone")

        listed = asyncio.run(deleted_rows(_request(world)))
        assert [(r["slug"], r["picks"]) for r in listed] == [("gone", 1)]

    def test_the_endpoint_clears_the_dead_rows_watches_and_spares_the_live_ones(self, world):
        """Through the real handler, not a hand-rolled copy of its query — an earlier version of this
        test re-implemented the DELETE in the test body, so removing the handler's shared-delete
        entirely left the whole suite green."""
        from shortlist.server.api.report import clear_deleted_rows

        self.a_credit_on(world, "gone")
        self.a_credit_on(world, "staff")

        result = asyncio.run(clear_deleted_rows(_request(world)))

        assert result["picks"] == 1, "the reported number must be what actually went"
        with world() as s:
            assert [r.collection_slug for r in s.query(SharedRowWatch).all()] == ["staff"]

    def test_removing_a_departed_user_drops_their_shared_watches(self, world):
        """Through the real handler. The previous version ran the DELETE itself and asserted the row
        was gone — it tested SQLAlchemy, and passed with the purge line deleted from the endpoint."""
        from shortlist.server.api.users import remove_departed_user

        self.a_credit_on(world, "staff")
        with world() as s:
            s.query(User).filter_by(id=1).one().departed_at = NOW
            s.commit()

        asyncio.run(remove_departed_user(1, _request(world)))

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0

    def test_a_removed_user_accrues_no_new_credits(self, world):
        """`shared_credits` filters `removed_at IS NULL`, so the purge is not racing the reconcile."""
        with world() as s:
            s.query(User).filter_by(id=1).one().removed_at = NOW
            s.commit()
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)

        reconcile_watched(world, [profile()])

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0


class TestADeletedSharedRowIsReachableInTheUi:
    """The API listed a deleted shared row as clearable, but the control that clears it renders only
    when `per_row` has a `deleted` entry — and `per_row` was picks-only, so a shared-only orphan was
    offered by the API and unreachable in the UI."""

    def test_it_reaches_per_row_flagged_deleted(self, world):
        from shortlist.server.services.report_service import effectiveness

        with world() as s:
            s.add(
                SharedRowWatch(
                    user_id=1,
                    collection_slug="gone",
                    tmdb_id=550,
                    media_type="movie",
                    title="Fight Club",
                    watched_at=NOW - timedelta(hours=1),
                )
            )
            s.commit()

        with world() as s:
            report = effectiveness(s, "all")
            gone = [r for r in report["per_row"] if r["deleted"]]
            assert [r["slug"] for r in gone] == ["gone"]
            assert gone[0]["watched"] == 1, "the disclosure needs a number to show"


class TestASlugCarryingBothKindsOfHistory:
    """A row switched per_person -> shared (`build` is patchable) accrues BOTH `picks` and
    `shared_row_watches` under one slug. The listing suppressed the shared side for such a slug to
    avoid a duplicate line, while the DELETE removed both — so the confirm dialog, whose stated job is
    to say what clearing costs, under-warned by exactly the shared count."""

    def test_what_the_listing_offers_is_what_the_clear_removes(self, world):
        from shortlist.server.api.report import clear_deleted_rows, deleted_rows

        with world() as s:
            for rank in (1, 2, 3):
                s.add(
                    PickRow(
                        run_id=1,
                        user_id=1,
                        collection_slug="switched",
                        section_key="1",
                        library="Movies",
                        tmdb_id=600 + rank,
                        media_type="movie",
                        rating_key=0,
                        rank=rank,
                        created_at=NOW - timedelta(days=2),
                    )
                )
            for tmdb in (700, 701):
                s.add(
                    SharedRowWatch(
                        user_id=1,
                        collection_slug="switched",
                        tmdb_id=tmdb,
                        media_type="movie",
                        title="T",
                        watched_at=NOW - timedelta(hours=1),
                    )
                )
            s.commit()

        listed = asyncio.run(deleted_rows(_request(world)))
        offered = next(r for r in listed if r["slug"] == "switched")["picks"]
        removed = asyncio.run(clear_deleted_rows(_request(world), slug="switched"))["picks"]

        assert offered == 5, "3 picks + 2 shared credits"
        assert removed == offered, "the dialog must not under-warn"
        with world() as s:
            assert s.query(SharedRowWatch).filter_by(collection_slug="switched").count() == 0
            assert s.query(PickRow).filter_by(collection_slug="switched").count() == 0

    def test_one_line_per_slug_not_two(self, world):
        from shortlist.server.api.report import deleted_rows

        with world() as s:
            s.add(
                PickRow(
                    run_id=1,
                    user_id=1,
                    collection_slug="switched",
                    section_key="1",
                    library="Movies",
                    tmdb_id=601,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    created_at=NOW - timedelta(days=2),
                )
            )
            s.add(
                SharedRowWatch(
                    user_id=1,
                    collection_slug="switched",
                    tmdb_id=700,
                    media_type="movie",
                    title="T",
                    watched_at=NOW - timedelta(hours=1),
                )
            )
            s.commit()

        listed = asyncio.run(deleted_rows(_request(world)))
        assert [r["slug"] for r in listed].count("switched") == 1


class TestTheChartAndTopTitlesCountSharedRowsToo:
    """Both are keyed on `watched_at`, so nothing about a shared row makes them inapplicable — they
    sit directly under the Watched tile, and a chart omitting what the tile counted reads as the same
    number failing to add up."""

    def a_shared_watch(self, world):
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=3), offset=1_800_000)
        reconcile_watched(world, [profile()])

    def test_the_weekly_trend_includes_it(self, world):
        from shortlist.server.services.report_service import effectiveness

        self.a_shared_watch(world)

        with world() as s:
            report = effectiveness(s, "all")
            assert sum(week["watched"] for week in report["trend"]) == report["overall"]["watched"]
            assert sum(week["watched"] for week in report["trend"]) == 1

    def test_top_titles_includes_it_with_its_name(self, world):
        from shortlist.server.services.report_service import effectiveness

        self.a_shared_watch(world)

        with world() as s:
            top = effectiveness(s, "all")["top_titles"]
            assert [(t["title"], t["watchers"]) for t in top] == [("Fight Club", 1)]


class TestTopTitlesTiesAreStable:
    def test_the_order_is_total_even_when_the_input_order_is_wrong(self):
        """The only version of this test with teeth. Driven from the database it has none: SQLite
        emits GROUP BY output in ascending key order and Python's sort is stable, so the input is
        already in tie-break order and the assertion holds with the tie-break deleted."""
        from shortlist.server.services.report_service import _rank_titles

        counts = {(300, "movie"): 1, (200, "movie"): 1, (100, "movie"): 1, (400, "movie"): 2}
        assert [k for k, _ in _rank_titles(counts, 8)] == [
            (400, "movie"),
            (100, "movie"),
            (200, "movie"),
            (300, "movie"),
        ]

    def test_a_movie_and_a_show_sharing_a_tmdb_id_have_a_defined_order(self):
        from shortlist.server.services.report_service import _rank_titles

        counts = {(1399, "show"): 1, (1399, "movie"): 1}
        assert [k for k, _ in _rank_titles(counts, 8)] == [(1399, "movie"), (1399, "show")]

    def test_equal_scores_keep_a_fixed_order(self, world):
        """Tie order was the SQL planner's, then the dict's insertion order — either can reshuffle
        equal-scoring titles between two renders of identical data, which reads as the list jumping
        around on refresh."""
        from shortlist.server.services.report_service import effectiveness

        with world() as s:
            # 300 gets TWO watchers so it must lead on score; 100 and 200 tie on one and must then be
            # ordered by tmdb id. Both keys have to act. A version where every title tied passed with
            # the tie-break DELETED: SQLite emits GROUP BY output in ascending key order and Python's
            # sort is stable, so the input was already in the asserted order.
            for tmdb in (300, 100, 200):
                s.add(
                    SharedRowWatch(
                        user_id=1,
                        collection_slug="staff",
                        tmdb_id=tmdb,
                        media_type="movie",
                        title=f"T{tmdb}",
                        watched_at=NOW - timedelta(hours=1),
                    )
                )
            s.add(
                SharedRowWatch(
                    user_id=2,
                    collection_slug="staff",
                    tmdb_id=300,
                    media_type="movie",
                    title="T300",
                    watched_at=NOW - timedelta(hours=1),
                )
            )
            s.commit()

        with world() as s:
            once = [t["tmdb_id"] for t in effectiveness(s, "all")["top_titles"]]
            twice = [t["tmdb_id"] for t in effectiveness(s, "all")["top_titles"]]
        assert once == twice == [300, 100, 200], "score first, then tmdb id for the tie"


class TestASharedFinishReachesTheReport:
    """Every `finished` figure has a shared arm, and none of them was covered: stubbing
    `_shared_finished_in` to false left 407 report tests green. The failure mode is invisible rather
    than loud — finished stays a valid subset of watched, it just reads as "nobody ever finishes
    anything off the shared row"."""

    def test_a_completed_shared_watch_counts_everywhere_finished_is_reported(self, world):
        from shortlist.server.services.report_service import effectiveness

        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=6), offset=1_200_000)
        reconcile_watched(world, [profile()])
        # They finish it later, by which time the row has moved on.
        finish = [WatchedItem(title="Fight Club", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=550)]
        reconcile_watched(world, [profile(finish)])

        with world() as s:
            report = effectiveness(s, "all")
            assert report["overall"]["finished"] == 1, "the headline tile"
            line = next(p for p in report["per_user"] if p["username"] == "alex")
            assert line["finished"] == 1, "their By-person line"
            row = next(r for r in report["per_row"] if r["slug"] == "staff")
            assert row["finished"] == 1, "the By-row line"
            assert sum(w["finished"] for w in report["trend"]) == 1, "the chart's dark segment"
            assert resolve_outcomes(s, None)[(1, 550, "movie")]["outcome"] == "finished"


class TestAServerWithNoSharedRowsIsUnaffected:
    """Most installs have no shared row at all. Every figure widened here must return exactly what it
    returned before, or this change is a silent regression for the majority of users."""

    def test_every_figure_matches_the_picks_only_computation(self, world):
        from sqlalchemy import func

        from shortlist.server.services.report_service import (
            _PERSON_TITLE,
            _finished_count,
            _finished_in,
            _watched_count,
            _watched_in,
            _watchers_count,
        )

        with world() as s:
            s.query(RunSharedRow).delete()
            s.query(Collection).filter_by(slug="staff").delete()
            s.commit()
        with world() as s:
            for rank, tmdb in enumerate((11, 22, 33), start=1):
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
                        rank=rank,
                        created_at=NOW - timedelta(days=3),
                        watched_at=NOW - timedelta(days=1) if tmdb != 33 else None,
                        finished_at=NOW - timedelta(hours=2) if tmdb == 11 else None,
                    )
                )
            s.commit()

        with world() as s:
            assert s.query(SharedRowWatch).count() == 0
            old_watched = s.query(func.count(func.distinct(_PERSON_TITLE))).filter(*_watched_in(None)).scalar()
            old_finished = s.query(func.count(func.distinct(_PERSON_TITLE))).filter(*_finished_in(None)).scalar()
            old_watchers = s.query(func.count(func.distinct(PickRow.user_id))).filter(*_watched_in(None)).scalar()

            assert _watched_count(s, None) == old_watched == 2
            assert _finished_count(s, None) == old_finished == 1
            assert _watchers_count(s, None) == old_watchers == 1

    def test_the_report_still_renders_with_no_shared_row(self, world):
        from shortlist.server.services.report_service import effectiveness

        with world() as s:
            s.query(RunSharedRow).delete()
            s.query(Collection).filter_by(slug="staff").delete()
            s.commit()
        with world() as s:
            report = effectiveness(s, "all")
            assert report["overall"]["watched"] == 0
            assert report["per_row"] == []
            assert report["top_titles"] == []
            assert report["recent"] == []


class TestTheWriterAndTheReaderAgree:
    """`_pick_dicts` writes the shared row's picks; `RowMembership._load_shared` reads them back.

    Nothing pinned that they agreed on a field set, and they did not: `_pick_dicts` omitted
    `media_type`, so the reader keyed every title `(tmdb_id, "")` and never matched the
    `(tmdb_id, "movie")` a play resolves to. The feature was a complete no-op on the real server while
    every test passed, because every test hand-wrote the JSON with the field present.
    """

    def test_a_written_pick_is_readable_as_a_credit_key(self, world):
        from shortlist.engine.models import MediaType as MT
        from shortlist.engine.models import Pick
        from shortlist.server.services.run_persistence import _pick_dicts

        written = _pick_dicts(
            SimpleNamespace(
                picks=[
                    Pick(tmdb_id=550, rating_key=9001, title="Fight Club", rank=1, reason="r", media_type=MT.MOVIE),
                    Pick(tmdb_id=1399, rating_key=9002, title="A Series", rank=2, reason="r", media_type=MT.SHOW),
                ]
            )
        )
        with world() as s:
            s.query(RunSharedRow).filter_by(run_id=1).one().picks = written
            s.commit()

        with world() as s:
            pool = RowMembership(s).shared_pool()

        assert (550, "movie") in pool, "the writer's output must be readable by the reader"
        assert (1399, "show") in pool
        # And the type is genuinely carried, not defaulted: a show and a film must not collide.
        assert (1399, "movie") not in pool

    def test_the_serializer_carries_the_media_type(self, world):
        from shortlist.engine.models import MediaType as MT
        from shortlist.engine.models import Pick
        from shortlist.server.services.run_persistence import _pick_dicts

        written = _pick_dicts(
            SimpleNamespace(picks=[Pick(tmdb_id=550, rating_key=1, title="T", rank=1, reason="r", media_type=MT.MOVIE)])
        )
        assert written[0]["media_type"] == "movie"


class TestStoppingPlaybackCreditsImmediately:
    """The whole chain the owner sees: play a pick, stop, and the number moves — without waiting for
    the nightly sync. Before this, `watch_sessions` held the fact and nothing acted on it for hours."""

    def test_the_event_only_pass_credits_a_partial_watch(self, world):
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
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.commit()
        watch_session(world, 99, started=NOW - timedelta(hours=2), offset=1_800_000)

        assert reconcile_from_events(world) == 1

        with world() as s:
            pick = s.query(PickRow).filter_by(user_id=1, tmdb_id=550).one()
            assert pick.watched_at is not None, "they started it from the row"
            assert pick.max_percent == 30, "and this is how far they got"

    def test_it_reads_nothing_from_plex_and_needs_no_profiles(self, world):
        """That is the entire point: it answers from records we already hold, so it can run the moment
        someone presses stop instead of waiting for a pass that re-reads every user's watched set."""
        from shortlist.server.services.run_persistence import reconcile_from_events

        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=2), offset=1_800_000)

        reconcile_from_events(world)  # no profiles argument exists to pass

        with world() as s:
            assert s.query(SharedRowWatch).count() == 1, "shared rows credit here too"

    def test_it_does_not_invent_a_history_depth(self, world):
        """`history_depth` means "how many titles this person has watched", answered by the Plex read
        this function skips. Writing it from an empty history is how every user came to read
        "0 titles watched"."""
        from shortlist.server.services.run_persistence import reconcile_from_events

        with world() as s:
            s.query(User).filter_by(id=1).one().prefs = {"history_depth": 412}
            s.commit()
        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=2), offset=1_800_000)

        reconcile_from_events(world)

        with world() as s:
            assert s.query(User).filter_by(id=1).one().prefs["history_depth"] == 412

    def test_it_never_uses_the_snapshot_path(self, world):
        """The snapshot path credits a title Plex FLAGGED watched with no play behind it. This function
        asked Plex nothing, so it has no business acting on that flag."""
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
                    tmdb_id=777,
                    media_type="movie",
                    rating_key=0,
                    rank=1,
                    title="Unplayed",
                    created_at=NOW - timedelta(days=1),
                )
            )
            s.commit()
        # No session, no event — only a title sitting in a live row.
        reconcile_from_events(world)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=777).one().watched_at is None

    def test_running_it_twice_changes_nothing_the_second_time(self, world):
        from shortlist.server.services.run_persistence import reconcile_from_events

        a_pick_so_the_rating_key_resolves(world)
        watch_session(world, 99, started=NOW - timedelta(hours=2), offset=1_800_000)
        reconcile_from_events(world)
        with world() as s:
            before = s.query(SharedRowWatch).one().watched_at

        reconcile_from_events(world)

        with world() as s:
            rows = s.query(SharedRowWatch).all()
            assert len(rows) == 1 and rows[0].watched_at == before


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
        reconcile_watched(world, [profile(history)], full_resync=full)

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

    def test_an_incremental_read_withdraws_nothing(self, world):
        """It sees an un-watch only inside the window it covered, so "absent from this read" would
        withdraw half a roster's credits on any night the cursor was narrow."""
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

        reconcile_watched(world, [profile(other)], full_resync=True)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=510).one().watched_at is not None

    def test_a_recent_credit_is_still_withdrawn(self, world):
        """The case this exists for: Plex flags something watched wrongly and the person corrects it,
        which happens within days."""
        from shortlist.server.services.run_persistence import UNWATCH_WITHDRAW_DAYS

        self.a_credit_aged(world, tmdb=511, days=UNWATCH_WITHDRAW_DAYS - 5)
        other = [WatchedItem(title="Other", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=999)]

        reconcile_watched(world, [profile(other)], full_resync=True)

        with world() as s:
            assert s.query(PickRow).filter_by(tmdb_id=511).one().watched_at is None


class TestAWithdrawnCreditLeavesNothingBehind:
    def test_the_percentage_goes_with_the_credit(self, world):
        """`resolve_outcomes` derives bounced/dropped from `max_percent` ALONE. A withdrawn pick that
        kept one still renders as an abandonment with no credit behind it — the exact "percentage on
        an uncredited title" state `_decide_outcomes` calls a bug."""
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

        reconcile_watched(world, [profile(other)], full_resync=True)

        with world() as s:
            row = s.query(PickRow).filter_by(tmdb_id=510).one()
            assert row.watched_at is None
            assert row.max_percent is None, "it would still render as a drop"
            assert resolve_outcomes(s, None) == {}
