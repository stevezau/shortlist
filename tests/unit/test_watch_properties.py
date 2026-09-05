"""Property tests for the invariants the watch numbers must never break.

Six review passes found ~45 defects here, and almost every one was an *invariant* violation rather
than a crash: a percentage walking backwards, a credit dated before its own delivery, one title
counted as both bounced and dropped, a completion predating its own credit. Example-based tests catch
those only where someone thought of the example.

These assert the invariants over generated inputs instead. `.claude/rules/testing.md` already requires
property tests for the privacy merge for the same reason — this is the other place where the rules are
simple, numerous, and easy to break one at a time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import example, given, settings
from hypothesis import strategies as st
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
    User,
    WatchEvent,
    WatchSession,
)
from shortlist.server.services.report_service import BOUNCE_PERCENT, engagement, resolve_outcomes
from shortlist.server.services.run_persistence import FINISHED_PERCENT, reconcile_watched

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
SETTINGS = settings(max_examples=50, deadline=None)


def fresh():
    """A brand-new database per EXAMPLE.

    Not a fixture: hypothesis runs many examples inside one test function, and a function-scoped
    fixture is created once for all of them — so the second example collided on `runs.id` and the
    failure looked like a defect in the code rather than in the harness.
    """
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as s:
        s.add(User(id=1, plex_account_id=99, username="alex", slug="alex"))
        s.add(Collection(id=1, slug="picked", name="Picked", enabled=True))
        s.add(Delivery(collection_slug="picked", user_slug="alex", library_key="1", rating_key=1))
        s.commit()
    return factory


#: Deliveries, plays and sessions at arbitrary offsets from NOW, in any order.
days_ago = st.integers(min_value=0, max_value=60)
percents = st.integers(min_value=0, max_value=100)


def _seed(sessions, deliveries, plays, sess, *, media_type="movie"):
    with sessions() as s:
        for i, d in enumerate(sorted(set(deliveries), reverse=True), start=1):
            s.add(Run(id=i, trigger="schedule", status="ok", started_at=NOW - timedelta(days=d)))
            s.add(
                PickRow(
                    run_id=i,
                    user_id=1,
                    collection_slug="picked",
                    section_key="1",
                    library="L",
                    tmdb_id=500,
                    media_type=media_type,
                    rating_key=10,
                    rank=1,
                    title="T",
                    created_at=NOW - timedelta(days=d),
                )
            )
        for j, d in enumerate(plays):
            s.add(
                WatchEvent(
                    plex_account_id=99,
                    rating_key=10,
                    media_type=media_type,
                    viewed_at=NOW - timedelta(days=d),
                    source="history",
                    history_key=f"h{j}",
                )
            )
        for k, (d, pct) in enumerate(sess):
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key=str(k),
                    rating_key=10,
                    media_type=media_type,
                    started_at=NOW - timedelta(days=d),
                    last_seen_at=NOW - timedelta(days=d),
                    ended_at=NOW - timedelta(days=d),
                    max_offset_ms=pct * 1000,
                    duration_ms=100 * 1000,
                    end_reason="stopped",
                )
            )
        s.commit()


def _profile(history=()):
    return UserProfile(
        username="alex", plex_account_id=99, user_type=UserType.SHARED, slug="alex", history=list(history)
    )


class TestPickInvariants:
    @given(
        deliveries=st.lists(days_ago, min_size=1, max_size=4),
        plays=st.lists(days_ago, max_size=4),
        sess=st.lists(st.tuples(days_ago, percents), max_size=4),
    )
    @SETTINGS
    def test_a_credit_is_never_dated_before_the_row_that_carries_it(self, deliveries, plays, sess):
        """`watched_at < created_at` on the same row says a title was watched before it was delivered."""
        sessions = fresh()
        _seed(sessions, deliveries, plays, sess)

        reconcile_watched(sessions, [_profile()])

        with sessions() as s:
            for pick in s.query(PickRow).filter(PickRow.watched_at.isnot(None)):
                assert pick.watched_at >= pick.created_at

    @given(
        deliveries=st.lists(days_ago, min_size=1, max_size=4),
        plays=st.lists(days_ago, max_size=4),
        sess=st.lists(st.tuples(days_ago, percents), max_size=4),
    )
    @SETTINGS
    def test_a_completion_never_predates_its_own_credit(self, deliveries, plays, sess):
        sessions = fresh()
        _seed(sessions, deliveries, plays, sess)
        history = [WatchedItem(title="T", media_type=MediaType.MOVIE, watched_at=NOW - timedelta(days=1), tmdb_id=500)]

        reconcile_watched(sessions, [_profile(history)])

        with sessions() as s:
            for pick in s.query(PickRow).filter(PickRow.finished_at.isnot(None)):
                assert pick.watched_at is not None, "finished but never credited"
                assert pick.finished_at >= pick.watched_at

    @given(
        deliveries=st.lists(days_ago, min_size=1, max_size=3),
        sess=st.lists(st.tuples(days_ago, percents), min_size=1, max_size=5),
    )
    @SETTINGS
    def test_progress_never_walks_backwards_across_repeated_reconciles(self, deliveries, sess):
        """The reconcile runs seven times a day forever; it must converge, not oscillate."""
        sessions = fresh()
        _seed(sessions, deliveries, [], sess)

        reconcile_watched(sessions, [_profile()])
        with sessions() as s:
            first = {p.id: p.max_percent for p in s.query(PickRow)}
        reconcile_watched(sessions, [_profile()])
        with sessions() as s:
            second = {p.id: p.max_percent for p in s.query(PickRow)}

        for pick_id, before in first.items():
            after = second[pick_id]
            if before is not None:
                assert after is not None and after >= before

    @given(
        deliveries=st.lists(days_ago, min_size=1, max_size=3),
        plays=st.lists(days_ago, max_size=3),
        sess=st.lists(st.tuples(days_ago, percents), max_size=3),
    )
    @SETTINGS
    def test_the_reconcile_is_idempotent(self, deliveries, plays, sess):
        sessions = fresh()
        _seed(sessions, deliveries, plays, sess)

        reconcile_watched(sessions, [_profile()])
        with sessions() as s:
            first = [(p.id, p.watched_at, p.finished_at, p.max_percent) for p in s.query(PickRow).order_by(PickRow.id)]
        reconcile_watched(sessions, [_profile()])
        with sessions() as s:
            second = [(p.id, p.watched_at, p.finished_at, p.max_percent) for p in s.query(PickRow).order_by(PickRow.id)]

        assert first == second

    @given(
        delivered=days_ago,
        watched=days_ago,
        session_day=days_ago,
        pct=percents,
    )
    @SETTINGS
    def test_a_percentage_never_appears_on_a_title_that_was_never_credited(self, delivered, watched, session_day, pct):
        """`max_percent` is a fact ABOUT a credited watch, never a reason to invent one.

        This is the property that was missing, and its absence let a real defect through: reading a
        `defaultdict` on the reject path minted an outcome for a title the snapshot had explicitly
        refused to credit, which then collected a percentage and surfaced as a "dropped" pick dated to
        today's delivery. Every other property here only inspects rows that already have `watched_at`,
        so none of them could see it.
        """
        sessions = fresh()
        _seed(sessions, [delivered], [], [(session_day, pct)])
        history = [
            WatchedItem(title="T", media_type=MediaType.MOVIE, watched_at=NOW - timedelta(days=watched), tmdb_id=500)
        ]

        reconcile_watched(sessions, [_profile(history)])

        with sessions() as s:
            for pick in s.query(PickRow):
                if pick.max_percent is not None:
                    assert pick.watched_at is not None, "a percentage on a title nothing ever credited"

    @given(
        deliveries=st.lists(days_ago, min_size=1, max_size=3),
        sess=st.lists(st.tuples(days_ago, percents), max_size=3),
    )
    @SETTINGS
    def test_a_series_never_carries_a_percentage(self, deliveries, sess):
        """An episode's progress is not the show's, and reporting it as such told the dashboard people
        abandon series just before the end."""
        sessions = fresh()
        _seed(sessions, deliveries, [], sess, media_type="show")

        reconcile_watched(sessions, [_profile()])

        with sessions() as s:
            assert all(p.max_percent is None for p in s.query(PickRow).filter_by(media_type="show"))


class TestReportInvariants:
    @given(
        deliveries=st.lists(days_ago, min_size=1, max_size=4),
        plays=st.lists(days_ago, max_size=3),
        sess=st.lists(st.tuples(days_ago, percents), max_size=4),
    )
    @SETTINGS
    def test_every_title_has_exactly_one_outcome(self, deliveries, plays, sess):
        """One person-title used to be counted as bounced AND dropped when two of its rows disagreed."""
        sessions = fresh()
        _seed(sessions, deliveries, plays, sess)
        reconcile_watched(sessions, [_profile()])

        with sessions() as s:
            outcomes = resolve_outcomes(s, None)
            data = engagement(s, "all")

        assert all(o["outcome"] in {"finished", "dropped", "bounced", "watching"} for o in outcomes.values())
        listed = [p for person in data["people"] for p in person["picks"]]
        assert len(listed) == len(outcomes), "the detail page and the split must see the same set"

    @given(
        deliveries=st.lists(days_ago, min_size=1, max_size=3),
        sess=st.lists(st.tuples(days_ago, percents), max_size=4),
    )
    # Every histogram bucket edge, always drawn — same reason as the boundary test below. The bucket
    # table (`0-10 / 10-25 / 25-50 / 50-75 / 75+`) is half-open, so the edge value belongs to the
    # bucket ABOVE it, and that was landing in a run only when hypothesis happened to pick it.
    @example(deliveries=[2], sess=[(1, 0), (1, 10), (1, 25), (1, 50), (1, 75)])
    @example(deliveries=[2], sess=[(1, 9), (1, 24), (1, 49), (1, 74)])
    @SETTINGS
    def test_the_histogram_always_sums_to_the_abandonments(self, deliveries, sess):
        """The tile and the chart beside it are the same quantity; they disagreed once already."""
        sessions = fresh()
        _seed(sessions, deliveries, [], sess)
        reconcile_watched(sessions, [_profile()])

        with sessions() as s:
            data = engagement(s, "all")
            outcomes = resolve_outcomes(s, None).values()

        abandoned = sum(1 for o in outcomes if o["outcome"] in {"bounced", "dropped"})
        assert sum(b["count"] for b in data["stop_points"]) == abandoned

    @given(pct=percents)
    # The boundaries, ALWAYS drawn. `st.integers(0, 100)` with 50 examples reaches an exact edge only
    # by luck: a mutation audit (2026-08-24) found this test killed `FINISHED_PERCENT >= ` -> ` > `
    # on only one of three cold runs, and `.hypothesis` is gitignored so CI starts cold every time.
    # A property test says "for all"; these say "and definitely for the values the rule turns on".
    @example(pct=0)
    @example(pct=BOUNCE_PERCENT - 1)
    @example(pct=BOUNCE_PERCENT)
    @example(pct=FINISHED_PERCENT - 1)
    @example(pct=FINISHED_PERCENT)
    @example(pct=100)
    @SETTINGS
    def test_the_bounce_boundary_is_exact_and_total(self, pct):
        sessions = fresh()
        _seed(sessions, [2], [], [(1, pct)])
        reconcile_watched(sessions, [_profile()])

        with sessions() as s:
            outcomes = list(resolve_outcomes(s, None).values())

        assert len(outcomes) == 1
        # THREE bands, not two. A film played past `FINISHED_PERCENT` is finished, not abandoned:
        # Plex flags it watched at its own ~90% bar, so the nightly sync would say so anyway, and
        # calling it "gave up" in the meantime states the opposite of what happened.
        if pct >= FINISHED_PERCENT:
            expected = "finished"
        elif pct < BOUNCE_PERCENT:
            expected = "bounced"
        else:
            expected = "dropped"
        assert outcomes[0]["outcome"] == expected, f"{pct}% should read as {expected}"


# --------------------------------------------------------------------------------------------
# Whole-pipeline invariants.
#
# Every pass over this feature so far has either read the code or built ONE state by hand. This
# generates states instead — many users, many rows, shared and personal, deliveries and plays and
# sessions at arbitrary times — runs the real reconcile and the real report over each, and asserts
# the properties the dashboard's numbers claim. A defect here is a number that means something other
# than its label, which is the shape almost every real defect in this feature has had.
# --------------------------------------------------------------------------------------------


def _world(users: int, rows: int):
    """A server with `users` people and `rows` rows, half of them shared."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as s:
        for u in range(1, users + 1):
            s.add(User(id=u, plex_account_id=100 + u, username=f"u{u}", slug=f"u{u}", enabled=True))
        for r in range(1, rows + 1):
            shared = r % 2 == 0
            s.add(
                Collection(
                    id=r,
                    slug=f"row{r}",
                    name=f"Row {r}",
                    enabled=True,
                    build="shared" if shared else "per_person",
                )
            )
            if shared:
                s.add(Delivery(collection_slug=f"row{r}", user_slug=f"shared_row{r}", library_key="1", rating_key=r))
            else:
                for u in range(1, users + 1):
                    s.add(Delivery(collection_slug=f"row{r}", user_slug=f"u{u}", library_key="1", rating_key=r))
        s.commit()
    return factory


@st.composite
def worlds(draw):
    users = draw(st.integers(min_value=1, max_value=3))
    rows = draw(st.integers(min_value=1, max_value=4))
    titles = draw(st.integers(min_value=1, max_value=4))
    deliveries = draw(st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=3, unique=True))
    plays = draw(st.lists(st.tuples(st.integers(1, 3), st.integers(0, 49)), max_size=6))
    sess = draw(st.lists(st.tuples(st.integers(1, 3), st.integers(0, 49), st.integers(0, 100)), max_size=6))
    return users, rows, titles, deliveries, plays, sess


def _populate(factory, users, rows, titles, deliveries, plays, sess):
    with factory() as s:
        for run_id, d in enumerate(sorted(set(deliveries), reverse=True), start=1):
            s.add(Run(id=run_id, trigger="schedule", status="ok", started_at=NOW - timedelta(days=d)))
            for r in range(1, rows + 1):
                shared = r % 2 == 0
                picks = [
                    {"tmdb_id": 500 + t, "media_type": "movie", "rating_key": 900 + t, "title": f"T{t}"}
                    for t in range(1, titles + 1)
                ]
                if shared:
                    s.add(
                        RunSharedRow(
                            run_id=run_id,
                            collection_slug=f"row{r}",
                            status="ok",
                            picks=picks,
                            audience=None,
                            delivered_at=NOW - timedelta(days=d),
                        )
                    )
                else:
                    for u in range(1, users + 1):
                        for t in range(1, titles + 1):
                            s.add(
                                PickRow(
                                    run_id=run_id,
                                    user_id=u,
                                    collection_slug=f"row{r}",
                                    section_key="1",
                                    library="L",
                                    tmdb_id=500 + t,
                                    media_type="movie",
                                    rating_key=900 + t,
                                    rank=t,
                                    title=f"T{t}",
                                    created_at=NOW - timedelta(days=d),
                                )
                            )
        for i, (u, d) in enumerate(plays):
            if u > users:
                continue
            s.add(
                WatchEvent(
                    plex_account_id=100 + u,
                    rating_key=901,
                    media_type="movie",
                    viewed_at=NOW - timedelta(days=d),
                    source="history",
                    history_key=f"h{i}",
                )
            )
        for i, (u, d, pct) in enumerate(sess):
            if u > users:
                continue
            s.add(
                WatchSession(
                    plex_account_id=100 + u,
                    session_key=f"s{i}",
                    rating_key=901,
                    media_type="movie",
                    started_at=NOW - timedelta(days=d),
                    last_seen_at=NOW - timedelta(days=d),
                    ended_at=NOW - timedelta(days=d),
                    max_offset_ms=pct * 1000,
                    duration_ms=100 * 1000,
                    end_reason="stopped",
                )
            )
        s.commit()


class TestTheReportMeansWhatItSays:
    """Generated servers, the real reconcile, the real report. Each assertion is a claim the
    dashboard makes in words."""

    @settings(max_examples=60, deadline=None)
    @given(worlds())
    def test_every_headline_invariant_holds_on_any_server(self, world):
        from shortlist.server.services.report_service import effectiveness
        from shortlist.server.services.run_persistence import reconcile_from_events

        users, rows, titles, deliveries, plays, sess = world
        factory = _world(users, rows)
        _populate(factory, users, rows, titles, deliveries, plays, sess)
        reconcile_from_events(factory)

        with factory() as s:
            for window in ("7", "30", "all"):
                r = effectiveness(s, window)
                o = r["overall"]
                w = f"[{window}]"
                assert o["finished"] <= o["watched"], f"{w} finished exceeds watched"
                assert o["bounced"] + o["dropped"] <= o["watched"], f"{w} gave-up exceeds watched"
                assert o["watched"] >= 0, w
                assert r["coverage"]["users_watched"] <= r["coverage"]["users_enabled"], w
                assert r["coverage"]["users_idle"] >= 0, w
                assert r["coverage"]["users_idle"] <= r["coverage"]["users_with_picks"], w
                for line in r["per_row"]:
                    assert line["finished"] <= line["watched"], f"{w} {line['slug']}"
                    assert line["name"], f"{w} nameless row"
                for line in r["per_user"]:
                    assert line["finished"] <= line["watched"], f"{w} {line['username']}"
                for week in r["trend"]:
                    assert week["finished"] <= week["watched"], f"{w} {week['week']}"
                land = o["landing"]
                assert land["watched"] <= land["delivered"], w
                assert len(r["recent"]) <= 20, w
                assert all(not k.startswith("_") for line in r["recent"] for k in line), w

    @settings(max_examples=40, deadline=None)
    @given(worlds())
    def test_running_the_credit_pass_twice_changes_nothing(self, world):
        """It runs every time anyone stops a video. Anything it does twice, it must do once."""
        from shortlist.server.services.report_service import effectiveness
        from shortlist.server.services.run_persistence import reconcile_from_events

        users, rows, titles, deliveries, plays, sess = world
        factory = _world(users, rows)
        _populate(factory, users, rows, titles, deliveries, plays, sess)

        reconcile_from_events(factory)
        with factory() as s:
            first = effectiveness(s, "all")["overall"]
        reconcile_from_events(factory)
        with factory() as s:
            second = effectiveness(s, "all")["overall"]

        # The COUNTS, not the whole payload: `landing.cohort_from`/`cohort_to` are derived from the
        # clock at call time, so they legitimately differ by microseconds between two calls and
        # comparing them compares the stopwatch rather than the answer.
        keys = ("watched", "finished", "bounced", "dropped", "delivered")
        assert {k: first[k] for k in keys} == {k: second[k] for k in keys}
        assert first["landing"]["watched"] == second["landing"]["watched"]

    @settings(max_examples=40, deadline=None)
    @given(worlds())
    def test_a_credit_is_never_silently_lost(self, world):
        """The full pass may WITHDRAW a credit — deliberately, and only under its own rules. What it
        must never do is quietly drop one that the event pass justified: every credit the event pass
        made is backed by playback, and playback-backed credits are exactly what withdrawal spares."""
        from shortlist.server.db.models import PickRow as PR
        from shortlist.server.services.run_persistence import reconcile_from_events, reconcile_watched

        users, rows, titles, deliveries, plays, sess = world
        factory = _world(users, rows)
        _populate(factory, users, rows, titles, deliveries, plays, sess)

        reconcile_from_events(factory)
        with factory() as s:
            before = {p.id for p in s.query(PR).filter(PR.watched_at.isnot(None))}

        # A full resync where Plex reports nothing watched — the harshest input withdrawal can get.
        profiles = [
            UserProfile(
                username=f"u{u}",
                plex_account_id=100 + u,
                user_type=UserType.SHARED,
                slug=f"u{u}",
                history=[WatchedItem(title="X", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=99999)],
            )
            for u in range(1, users + 1)
        ]
        reconcile_watched(factory, profiles, complete_read=True)

        with factory() as s:
            after = {p.id for p in s.query(PR).filter(PR.watched_at.isnot(None))}
        assert before <= after, "a playback-backed credit was withdrawn"


def _stamps(factory):
    """EVERYTHING either pass writes: both tables, and all three columns of each.

    An earlier version compared `picks.watched_at`/`max_percent` only, which made the comparison
    blind to shared-row credits and to completion — so deleting the whole `_apply_shared` call from
    one path still passed. If it is written by a credit pass, it belongs here.
    """
    from shortlist.server.db.models import PickRow as PR
    from shortlist.server.db.models import SharedRowWatch as SRW

    with factory() as s:
        picks = {
            ("pick", p.user_id, p.tmdb_id, p.media_type, p.collection_slug): (
                p.watched_at,
                p.finished_at,
                p.max_percent,
            )
            for p in s.query(PR).filter(PR.watched_at.isnot(None) | PR.max_percent.isnot(None))
        }
        shared = {
            ("shared", r.user_id, r.tmdb_id, r.media_type, r.collection_slug): (
                r.watched_at,
                r.finished_at,
                r.max_percent,
            )
            for r in s.query(SRW)
        }
        return {**picks, **shared}


class TestTheTwoCreditPathsAgree:
    """`reconcile_from_events` and `reconcile_watched` are two implementations of the same rule.

    One runs the moment playback stops and reads nothing from Plex; the other runs nightly with a
    profile's watched set in hand. On the parts BOTH can see — credits justified by playback — they
    must reach the same answer, or the dashboard changes its mind depending on which pass ran last.
    Nothing compared them until now; each was only ever tested against its own expectations.

    What this CANNOT catch, and so must not be trusted for: anything the two paths share. They both
    go through `_decide_outcomes`, so a change to the credit rule itself moves both sides equally and
    the comparison stays green — verified by breaking the finished-film rule and watching this pass.
    Those live in their own tests; this one is only ever evidence about DIVERGENCE.
    """

    @settings(max_examples=80, deadline=None)
    @given(worlds())
    def test_the_live_pass_and_the_nightly_pass_credit_the_same_things(self, world):
        from shortlist.server.services.run_persistence import reconcile_from_events, reconcile_watched

        users, rows, titles, deliveries, plays, sess = world

        live = _world(users, rows)
        _populate(live, users, rows, titles, deliveries, plays, sess)
        reconcile_from_events(live)

        nightly = _world(users, rows)
        _populate(nightly, users, rows, titles, deliveries, plays, sess)
        # An EMPTY history: whatever this pass credits can only have come from playback, which is
        # exactly the subset the live pass can see. Anything more would be the snapshot path, and
        # that is the one thing the two are not supposed to agree on.
        empty = [
            UserProfile(username=f"u{u}", plex_account_id=100 + u, user_type=UserType.SHARED, slug=f"u{u}", history=[])
            for u in range(1, users + 1)
        ]
        reconcile_watched(nightly, empty)

        assert _stamps(live) == _stamps(nightly), "the two passes disagree about the same playback"

    @settings(max_examples=80, deadline=None)
    @given(worlds())
    def test_running_one_after_the_other_changes_nothing_either_way(self, world):
        """They run in both orders in production — the nightly sync can land before or after a
        session ends. Neither order may produce an answer the other does not."""
        from shortlist.server.services.run_persistence import reconcile_from_events, reconcile_watched

        users, rows, titles, deliveries, plays, sess = world
        empty = [
            UserProfile(username=f"u{u}", plex_account_id=100 + u, user_type=UserType.SHARED, slug=f"u{u}", history=[])
            for u in range(1, users + 1)
        ]

        a = _world(users, rows)
        _populate(a, users, rows, titles, deliveries, plays, sess)
        reconcile_from_events(a)
        reconcile_watched(a, empty)

        b = _world(users, rows)
        _populate(b, users, rows, titles, deliveries, plays, sess)
        reconcile_watched(b, empty)
        reconcile_from_events(b)

        assert _stamps(a) == _stamps(b), "the order of the two passes changed the outcome"
