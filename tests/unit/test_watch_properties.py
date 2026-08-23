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

from hypothesis import given, settings
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
    User,
    WatchEvent,
    WatchSession,
)
from shortlist.server.services.report_service import BOUNCE_PERCENT, engagement, resolve_outcomes
from shortlist.server.services.run_persistence import reconcile_watched

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
    @SETTINGS
    def test_the_bounce_boundary_is_exact_and_total(self, pct):
        sessions = fresh()
        _seed(sessions, [2], [], [(1, pct)])
        reconcile_watched(sessions, [_profile()])

        with sessions() as s:
            outcomes = list(resolve_outcomes(s, None).values())

        assert len(outcomes) == 1
        expected = "bounced" if pct < BOUNCE_PERCENT else "dropped"
        assert outcomes[0]["outcome"] == expected
