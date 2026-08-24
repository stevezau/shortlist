"""`finished` must never outrun `watched` inside a report window.

The dashboard presents the pair as "N watched · M finished" and draws M as a segment INSIDE the bar
of N. That only holds if finished is a genuine subset of watched for the same window — and the
obvious implementation (filter `watched_at` in the window for one, `finished_at` in the window for
the other) is NOT: a series credited in June and finished in August produces `0 watched · 1 finished`
in an August window, which is nonsense text and an overflowing bar.

So the window question for both columns is asked of `watched_at`: "of the picks credited in this
window, how many are finished". The trend chart already had to work this way to be stackable; these
tests pin the same rule across the headline, the per-user and the per-row breakdowns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.db.models import Base, Collection, PickRow, User
from shortlist.server.services.report_service import SETTLING_HOURS, effectiveness, row_effectiveness

NOW = datetime.now(UTC)


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        session.add(User(id=1, plex_account_id=7, username="alex", slug="alex", enabled=True))
        session.add(Collection(slug="picked", name="Picked for You", enabled=True))
        session.commit()
    return factory


def seed(sessions, *, tmdb_id: int, delivered_ago: int, watched_ago: int | None, finished_ago: int | None) -> None:
    with sessions() as session:
        session.add(
            PickRow(
                user_id=1,
                tmdb_id=tmdb_id,
                media_type="show",
                rating_key=tmdb_id,
                rank=1,
                collection_slug="picked",
                section_key="1",
                library="TV Shows",
                title=f"S{tmdb_id}",
                created_at=NOW - timedelta(days=delivered_ago),
                watched_at=None if watched_ago is None else NOW - timedelta(days=watched_ago),
                finished_at=None if finished_ago is None else NOW - timedelta(days=finished_ago),
            )
        )
        session.commit()


def seed_in(sessions, *, tmdb_id: int, media_type: str, library: str, watched: bool, finished: bool) -> None:
    """A pick in a named library, old enough to be inside the matured cohort the row panel judges."""
    with sessions() as session:
        session.add(
            PickRow(
                user_id=1,
                tmdb_id=tmdb_id,
                media_type=media_type,
                rating_key=tmdb_id,
                rank=1,
                collection_slug="picked",
                section_key="1" if media_type == "movie" else "2",
                library=library,
                title=f"T{tmdb_id}",
                created_at=NOW - timedelta(days=60),
                watched_at=NOW - timedelta(days=55) if watched else None,
                finished_at=NOW - timedelta(days=50) if finished else None,
            )
        )
        session.commit()


class TestFinishedNeverExceedsWatchedInAWindow:
    """The failing shape: credited outside the window, finished inside it."""

    def _report(self, sessions, window: str = "7") -> dict:
        with sessions() as session:
            return effectiveness(session, window)

    def test_the_headline_pair_stays_a_subset(self, sessions):
        # Credited 40 days ago, finished 2 days ago — the exact case a long series produces.
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=40, finished_ago=2)

        overall = self._report(sessions)["overall"]

        assert overall["finished"] <= overall["watched"], (
            f"finished ({overall['finished']}) outran watched ({overall['watched']}) — "
            "the tile pair reads as nonsense and the trend segment overflows its bar"
        )

    def test_the_per_user_line_stays_a_subset(self, sessions):
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=40, finished_ago=2)

        per_user = self._report(sessions)["per_user"]

        for line in per_user:
            assert line["finished"] <= line["watched"], f"'{line['watched']} watched · {line['finished']} finished'"

    def test_the_per_row_line_stays_a_subset(self, sessions):
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=40, finished_ago=2)

        per_row = self._report(sessions)["per_row"]

        for line in per_row:
            assert line["finished"] <= line["watched"], f"'{line['watched']} watched · {line['finished']} finished'"

    def test_a_pick_credited_and_finished_inside_the_window_counts_in_both(self, sessions):
        """The rule must not fix the overflow by simply never counting anything."""
        seed(sessions, tmdb_id=2, delivered_ago=6, watched_ago=5, finished_ago=3)

        overall = self._report(sessions)["overall"]

        assert overall["watched"] == 1
        assert overall["finished"] == 1

    def test_a_series_credited_in_the_window_but_unfinished_counts_only_as_watched(self, sessions):
        seed(sessions, tmdb_id=3, delivered_ago=6, watched_ago=5, finished_ago=None)

        overall = self._report(sessions)["overall"]

        assert overall["watched"] == 1
        assert overall["finished"] == 0

    def test_the_landing_cohort_stays_a_subset(self, sessions):
        """`landing` counts against a matured cohort with its own filters, so it does not route
        through `_finished_in` and needs its own guard — and its own test."""
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=40, finished_ago=2)

        landing = self._report(sessions, "30")["overall"]["landing"]

        assert landing["finished"] <= landing["watched"], landing

    def test_the_row_panel_and_its_per_library_split_stay_a_subset(self, sessions):
        """`row_effectiveness` is a separate query path with the same invariant to keep — the row
        editor draws finished inside watched exactly as the dashboard does."""
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=40, finished_ago=2)

        with sessions() as session:
            panel = row_effectiveness(session, "picked")

        assert panel["finished"] <= panel["watched"], panel
        if panel["matured"]:
            assert panel["matured"]["finished"] <= panel["matured"]["watched"], panel["matured"]
        for lib in panel["per_library"]:
            assert lib["finished"] <= lib["watched"], lib

    def test_a_finished_stamp_without_a_watched_stamp_is_never_counted(self, sessions):
        """The invariant the guards defend, asserted directly rather than assumed from the writer.

        Nothing clears `watched_at` today, so this row cannot occur — which is exactly why it is
        worth pinning: the day something does, every one of these counters must refuse it rather
        than render `0 watched · 1 finished` and a segment wider than its own bar.

        Delivered 50 days ago, NOT 10: `landing` and the row panel's `matured` cohort only contain
        picks older than the hit window, so a recent pick leaves both counting 0 for a reason that
        has nothing to do with the guard — the first version of this test passed with the guard
        deleted.
        """
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=None, finished_ago=2)

        with sessions() as session:
            report = effectiveness(session, "all")
            panel = row_effectiveness(session, "picked")

        assert report["overall"]["finished"] == 0
        assert report["overall"]["landing"]["finished"] == 0
        assert panel["finished"] == 0
        for line in report["per_row"] + report["per_user"]:
            assert line["finished"] == 0, line
        for lib in panel["per_library"]:
            assert lib["finished"] == 0, lib

    def test_a_weeks_finished_count_is_the_real_number_not_a_zero(self, sessions):
        """The subset assertions above all hold for a counter that returns 0 for everything.

        Proven, not theorised: replacing `finished_by_week.get(week, 0)` with a literal `0` left the
        whole suite green. `finished <= watched` is only half a test — this is the other half.
        """
        seed(sessions, tmdb_id=1, delivered_ago=20, watched_ago=18, finished_ago=17)
        seed(sessions, tmdb_id=2, delivered_ago=20, watched_ago=18, finished_ago=None)

        trend = self._report(sessions, "all")["trend"]

        assert trend, "no trend weeks at all — the assertion below would be vacuous"
        assert sum(p["finished"] for p in trend) == 1, trend
        assert sum(p["watched"] for p in trend) == 2, trend

    def test_the_per_library_split_counts_each_library_correctly(self, sessions):
        """Positive direction, per library. The guard added to the `case()` is exactly where an
        over-tight condition would land — and hard-coding that case to count 0 kept 142 existing
        tests green, so nothing else in the suite would notice a row panel reading "0 finished"
        for every library on a real server.
        """
        # Movies: everything watched is finished. TV: one of three, the shape this split exists for.
        for tmdb_id, watched, finished in [(10, True, True), (11, True, True), (12, False, False)]:
            seed_in(
                sessions,
                tmdb_id=tmdb_id,
                media_type="movie",
                library="Movies",
                watched=watched,
                finished=finished,
            )
        for tmdb_id, watched, finished in [(20, True, True), (21, True, False), (22, True, False)]:
            seed_in(
                sessions,
                tmdb_id=tmdb_id,
                media_type="show",
                library="TV Shows",
                watched=watched,
                finished=finished,
            )

        with sessions() as session:
            panel = row_effectiveness(session, "picked")

        by_library = {lib["library"]: lib for lib in panel["per_library"]}
        assert by_library["Movies"]["watched"] == 2, by_library
        assert by_library["Movies"]["finished"] == 2, by_library
        assert by_library["TV Shows"]["watched"] == 3, by_library
        assert by_library["TV Shows"]["finished"] == 1, by_library
        assert panel["finished"] == 3, panel

    def test_the_trend_segment_never_exceeds_its_own_column(self, sessions):
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=40, finished_ago=2)
        seed(sessions, tmdb_id=2, delivered_ago=6, watched_ago=5, finished_ago=3)

        for point in self._report(sessions, "all")["trend"]:
            assert point["finished"] <= point["watched"], point


class TestTheRecentFeedSaysWhichKindOfWatchItWas:
    """ "Watched" is Plex's binary flag, and for a SERIES it trips on the first finished episode.

    Measured on a real server: 21 of 158 credited show picks had actually been finished. A feed that
    prints "watched" for all of them overstates the product's success by a factor of seven, on the
    one page whose whole job is reporting that success — and it contradicts the By-row card directly
    above it, which has said "N watched · M finished" all along.
    """

    def test_a_finished_series_is_reported_as_finished(self, sessions):
        seed(sessions, tmdb_id=1, delivered_ago=40, watched_ago=10, finished_ago=8)

        with sessions() as session:
            recent = effectiveness(session, "30")["recent"]

        assert len(recent) == 1
        assert recent[0]["finished_at"] is not None

    def test_a_series_they_only_started_carries_no_finish(self, sessions):
        """Credited by Plex on episode one, never seen out — the case the split exists for."""
        seed(sessions, tmdb_id=2, delivered_ago=40, watched_ago=10, finished_ago=None)

        with sessions() as session:
            recent = effectiveness(session, "30")["recent"]

        assert len(recent) == 1
        assert recent[0]["watched_at"] is not None
        assert recent[0]["finished_at"] is None

    def test_the_finish_is_taken_over_the_whole_title_not_one_delivery(self, sessions):
        """A title re-recommended over several runs has one pick row per run, and the two stamps
        land on different passes — `watched_at` when Plex credits it, `finished_at` once it passes
        our completion threshold, possibly a fortnight later onto whichever row was newest then.

        The feed already collapses those rows to one line per (person, title). Reading `finished_at`
        off the single credited row rather than aggregating it over the group would report a series
        the person demonstrably finished as merely started, depending on which delivery won.
        """
        # Older delivery carries the finish; the newest delivery carries only the credit.
        seed(sessions, tmdb_id=3, delivered_ago=60, watched_ago=20, finished_ago=15)
        seed(sessions, tmdb_id=3, delivered_ago=10, watched_ago=5, finished_ago=None)

        with sessions() as session:
            recent = effectiveness(session, "30")["recent"]

        assert len(recent) == 1, "one line per person+title, not one per delivery"
        assert recent[0]["finished_at"] is not None, "the finish belongs to the title, not the row"


class TestADeltaNeedsAPreviousPeriodToCompareAgainst:
    """A "+53 vs previous" that compares against a month when Shortlist was not installed.

    Found on a real server 2026-08-24: the 30-day window read `watched: 53, watched_prev: 0,
    watched_delta: 53` because the previous 30 days ended the day after the first pick ever existed.
    The dashboard rendered it as growth. The same response carried `avg_days_to_watch_delta: null`
    for the identical reason — an average over nothing is already None — so one fact was being shown
    two different ways depending on which aggregate happened to be null-safe.
    """

    def test_no_delta_when_the_previous_period_predates_the_first_pick(self, sessions):
        # Everything delivered and watched 3 days ago. A 30-day window's previous period runs from
        # day 60 to day 30, entirely before this app had ever delivered anything.
        seed(sessions, tmdb_id=1, delivered_ago=3, watched_ago=3, finished_ago=3)

        with sessions() as session:
            report = effectiveness(session, "30")
        overall = report["overall"]

        assert overall["watched"] == 1
        # EVERY previous-period figure, not just the headline one. Asserting `watched_prev` alone is
        # how `runs.in_window_delta` was missed the first time, and a mutation audit of this very fix
        # then found `watchers_prev` and `avg_prev` unpinned as well. The guard is one decision; the
        # test covers all of what it decides.
        assert overall["watched_prev"] is None, "there was no previous period to count"
        assert overall["watched_delta"] is None, "a comparison against an uninstalled app is not growth"
        assert overall["avg_days_to_watch_delta"] is None, "already None; the pair must agree"
        assert report["coverage"]["users_watched_delta"] is None, "watchers_prev needs the same guard"

    def test_the_delta_still_works_once_there_is_real_history(self, sessions):
        # First pick 40 days ago, so a 7-day window's previous period (days 14-7) sits well inside
        # the app's lifetime and IS a fair comparison.
        seed(sessions, tmdb_id=1, delivered_ago=40, watched_ago=10, finished_ago=10)  # previous period
        seed(sessions, tmdb_id=2, delivered_ago=40, watched_ago=3, finished_ago=3)  # current period
        seed(sessions, tmdb_id=3, delivered_ago=40, watched_ago=2, finished_ago=2)  # current period

        with sessions() as session:
            overall = effectiveness(session, "7")["overall"]

        assert overall["watched"] == 2
        assert overall["watched_prev"] == 1
        assert overall["watched_delta"] == 1, "two this week against one last week is a real +1"

    def test_a_partly_covered_previous_period_is_still_no_comparison(self, sessions):
        """The case that separates `if comparable` from `if since` on the AVERAGES.

        A count over a period the app half-existed in is a truthful 0 only when nothing happened.
        Put a real watch inside the previous window while the app is younger than that window, and
        `avg_prev`/`watchers_prev` become genuine numbers over a period that is two-thirds
        pre-installation — an undercount that biases every delta toward good news.
        """
        # First pick 10 days ago. A 7-day window's previous period runs from day 14 to day 7, so the
        # app existed for only the last three days of it — and there is a real watch in that sliver.
        seed(sessions, tmdb_id=1, delivered_ago=10, watched_ago=9, finished_ago=9)  # inside prev period
        seed(sessions, tmdb_id=2, delivered_ago=10, watched_ago=2, finished_ago=2)  # current period

        with sessions() as session:
            report = effectiveness(session, "7")
        overall = report["overall"]

        assert overall["watched"] == 1, "one watch inside the current window"
        assert overall["watched_prev"] is None, "counted a period the app existed for 3 of 7 days"
        assert report["coverage"]["users_watched_delta"] is None
        assert overall["avg_days_to_watch_delta"] is None, "averaged over a partly pre-install window"

    def test_an_app_that_started_exactly_when_the_window_opens_is_comparable(self):
        """`<=`, not `<`, tested at the exact instant.

        Through `effectiveness` this boundary is unreachable: it reads its own clock, so a fixture
        can never land a timestamp exactly on `prev_since` and the flip to `<` survived a full
        mutation audit. `_period_is_comparable` exists as a named function so the rule can be asked
        directly.
        """
        from shortlist.server.services.report_service import _period_is_comparable

        opens = datetime(2026, 8, 10, 17, 30, tzinfo=UTC)

        assert _period_is_comparable(opens, opens) is True, "started exactly at the window's open"
        assert _period_is_comparable(opens - timedelta(microseconds=1), opens) is True
        assert _period_is_comparable(opens + timedelta(microseconds=1), opens) is False, (
            "one microsecond into the window is not the whole window"
        )

    def test_no_previous_period_and_no_evidence_are_both_not_comparable(self):
        """The `all` window has no previous period; an empty database has no start."""
        from shortlist.server.services.report_service import _period_is_comparable

        when = datetime(2026, 8, 10, tzinfo=UTC)
        assert _period_is_comparable(when, None) is False
        assert _period_is_comparable(None, when) is False
        assert _period_is_comparable(None, None) is False

    def test_the_runs_delta_is_guarded_too(self, sessions):
        """The site the first version of this guard missed. `runs.in_window_delta` is the same
        misleading arrow — "15 runs, +15 vs previous" against a fortnight with no app in it — and it
        was still reading `if since`."""
        seed(sessions, tmdb_id=1, delivered_ago=3, watched_ago=3, finished_ago=3)

        with sessions() as session:
            runs = effectiveness(session, "30")["runs"]

        assert runs["in_window_delta"] is None, "compared runs against a month with no app in it"

    def test_a_run_that_picked_nothing_still_anchors_the_comparison(self, sessions):
        """The anchor is when Shortlist was RUNNING, not when it first delivered. A server whose
        early runs produced no picks is still a server that was installed, and suppressing its runs
        comparison would be the opposite error."""
        from shortlist.server.db.models import Run

        with sessions() as session:
            session.add(Run(trigger="schedule", status="ok", started_at=NOW - timedelta(days=70)))
            session.commit()
        # First pick only 3 days ago, so picks alone would say "not comparable".
        seed(sessions, tmdb_id=1, delivered_ago=3, watched_ago=3, finished_ago=3)

        with sessions() as session:
            overall = effectiveness(session, "30")["overall"]

        assert overall["watched_prev"] == 0, "the app was running; zero watches is a real zero"
        assert overall["watched_delta"] == 1

    def test_a_server_with_no_picks_at_all_reports_no_delta(self, sessions):
        """`first_pick` is None on an empty database, and None is not comparable to a date."""
        with sessions() as session:
            overall = effectiveness(session, "30")["overall"]

        assert overall["watched"] == 0
        assert overall["watched_prev"] is None
        assert overall["watched_delta"] is None


class TestTheRowLabelIsStable:
    """A title delivered on TWO rows is credited on both with the identical `watched_at` — that is
    what `_apply_outcomes` does — so a tie is the normal case, not an edge. Nothing decided which row
    won it, and the query has no ORDER BY, so the label on "Worth a look" and the recent-watches feed
    was whichever row the database happened to return first.

    Both `<` → `<=` flips survived the mutation audit of 2026-08-24 for exactly that reason: the
    answer was arbitrary in both directions, so neither direction could be wrong.
    """

    def _two_rows_showing_one_title(self, sessions, order=("because", "picked")):
        """Both rows delivered it; `picked` FIRST, ten days before `because`. One watch, stamped on
        both to the microsecond, which is what `_apply_outcomes` really does.

        `order` is the row INSERTION order, and it is the whole point. With no tie-break the winner
        is whichever row the database returns first, which for SQLite is insertion order — so a test
        that inserts the expected winner first passes with the tie-break deleted. Both orders are
        exercised below.
        """
        from shortlist.server.db.models import Collection, PickRow

        watched = NOW - timedelta(days=1)
        delivered = {"picked": 20, "because": 10}
        with sessions() as session:
            session.add(Collection(slug="because", name="Because you watched", enabled=True))
            for slug in order:
                delivered_ago = delivered[slug]
                session.add(
                    PickRow(
                        user_id=1,
                        tmdb_id=550,
                        media_type="movie",
                        rating_key=550,
                        rank=1,
                        collection_slug=slug,
                        section_key="1",
                        library="Movies",
                        title="Fight Club",
                        created_at=NOW - timedelta(days=delivered_ago),
                        watched_at=watched,
                    )
                )
            session.commit()

    @pytest.mark.parametrize("order", [("picked", "because"), ("because", "picked")])
    def test_the_row_that_delivered_it_first_gets_the_credit(self, sessions, order):
        """Whichever way round the rows go in, the row that delivered it EARLIEST owns the watch.

        The `("because", "picked")` case is the one with teeth: `because` is returned first and would
        win by default, so it fails the moment the tie-break stops deciding.
        """
        from shortlist.server.services.report_service import resolve_outcomes

        self._two_rows_showing_one_title(sessions, order)
        with sessions() as session:
            entry = resolve_outcomes(session, None)[(1, 550, "movie")]

        assert entry["row"] == "picked", (
            f"inserted {order} and got {entry['row']!r} — the label follows row order, not the rule"
        )


class TestTwoSharedRowsCarryingOneTitle:
    """The same tie, on the shared side. Two shared rows can both carry a title and both credit the
    same person at the same instant, and a shared row has no per-person delivery time to order by —
    so slug alone decides. Arbitrary, but STABLE, which is the property that was missing.
    """

    def _both_rows_credit(self, sessions, order):
        from shortlist.server.db.models import SharedRowWatch

        watched = NOW - timedelta(days=1)
        with sessions() as session:
            for slug in order:
                session.add(
                    SharedRowWatch(
                        user_id=1,
                        collection_slug=slug,
                        tmdb_id=550,
                        media_type="movie",
                        title="Fight Club",
                        watched_at=watched,
                    )
                )
            session.commit()

    @pytest.mark.parametrize("order", [("a_row", "z_row"), ("z_row", "a_row")])
    def test_the_same_pair_always_resolves_to_the_same_row(self, sessions, order):
        from shortlist.server.services.report_service import resolve_outcomes

        self._both_rows_credit(sessions, order)
        with sessions() as session:
            entry = resolve_outcomes(session, None)[(1, 550, "movie")]

        assert entry["row"] == "a_row", (
            f"inserted {order} and got {entry['row']!r} — the label follows insertion order, not a rule"
        )


class TestASharedRowIsNotLabelledWithAPersonalRowsLibrary:
    def test_the_library_is_cleared_when_a_shared_row_wins_the_outcome(self, sessions):
        """`engagement` renders `namer.label(row, library)`. A shared row is ONE collection and is not
        inside the personal row's library, so carrying the old value over prints a shared TV row under
        a Movies heading. The clearing was commented but never tested — a mutation that kept the
        personal library left the suite green.
        """
        from shortlist.server.db.models import PickRow, SharedRowWatch
        from shortlist.server.services.report_service import resolve_outcomes

        with sessions() as session:
            # A personal row in Movies, watched LATER...
            session.add(
                PickRow(
                    user_id=1,
                    tmdb_id=550,
                    media_type="movie",
                    rating_key=550,
                    rank=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    title="Fight Club",
                    created_at=NOW - timedelta(days=20),
                    watched_at=NOW - timedelta(days=1),
                )
            )
            # ...and a shared row watched EARLIER, so the shared row wins the outcome.
            session.add(
                SharedRowWatch(
                    user_id=1,
                    collection_slug="staff",
                    tmdb_id=550,
                    media_type="movie",
                    title="Fight Club",
                    watched_at=NOW - timedelta(days=5),
                )
            )
            session.commit()

        with sessions() as session:
            entry = resolve_outcomes(session, None)[(1, 550, "movie")]

        assert entry["row"] == "staff", "the earlier watch should own the outcome"
        assert entry["library"] == "", "kept the personal row's library on a shared row's label"


class TestTheEngagementDetailSurvivesARosterChange:
    def test_a_pick_belonging_to_a_deleted_user_does_not_break_the_page(self, sessions):
        """`users[user_id]` with no guard raises KeyError and 500s the endpoint. Picks outlive the
        person: `remove_departed_user` clears the account while their rows are still on record."""
        from shortlist.server.db.models import PickRow
        from shortlist.server.services.report_service import engagement

        with sessions() as session:
            session.add(
                PickRow(
                    user_id=999,  # nobody
                    tmdb_id=550,
                    media_type="movie",
                    rating_key=550,
                    rank=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    title="Fight Club",
                    created_at=NOW - timedelta(days=5),
                    watched_at=NOW - timedelta(days=1),
                )
            )
            session.commit()

        with sessions() as session:
            report = engagement(session, "30")

        assert report["people"] == [], "a departed user's pick should be skipped, not rendered or raised on"


class TestTheRowPanelReportsFirstDeliveryNotLatest:
    def test_first_delivered_at_is_the_earliest_delivery(self, sessions):
        """It is what tells "never run" apart from "ran last night" on the row editor. `func.max`
        instead of `func.min` reports the most recent delivery and the panel says a long-running row
        started yesterday."""
        from shortlist.server.services.report_service import row_effectiveness

        seed(sessions, tmdb_id=1, delivered_ago=60, watched_ago=None, finished_ago=None)
        seed(sessions, tmdb_id=2, delivered_ago=1, watched_ago=None, finished_ago=None)

        with sessions() as session:
            panel = row_effectiveness(session, "picked")

        assert panel["first_delivered_at"].startswith((NOW - timedelta(days=60)).strftime("%Y-%m-%d")), (
            f"reported {panel['first_delivered_at']} — the latest delivery, not the first"
        )

    def test_finished_can_never_exceed_watched_on_the_panel(self, sessions):
        """`finished` is drawn INSIDE `watched`, so its query carries both conditions. Dropping the
        watched one lets a pick finished-but-not-credited inflate the segment past its own bar."""
        from shortlist.server.db.models import PickRow
        from shortlist.server.services.report_service import row_effectiveness

        with sessions() as session:
            session.add(
                PickRow(
                    user_id=1,
                    tmdb_id=7,
                    media_type="movie",
                    rating_key=7,
                    rank=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    title="Orphan",
                    created_at=NOW - timedelta(days=40),
                    watched_at=None,  # never credited...
                    finished_at=NOW - timedelta(days=2),  # ...but carries a completion
                )
            )
            session.commit()

        with sessions() as session:
            panel = row_effectiveness(session, "picked")

        assert panel["finished"] <= panel["watched"], (
            f"finished ({panel['finished']}) outran watched ({panel['watched']})"
        )


class TestTheTitlesThatLosePeople:
    """`engagement()["losing"]` — "what does everyone do with THIS pick", as opposed to "what did
    this person do with their row". No longer rendered (the card was removed), but still returned and
    still documented, and the boundary that decides membership was pinned by nothing.
    """

    def _title_watched_by(self, sessions, *, finishers: int, abandoners: int, tmdb_id: int = 550):
        """One title, watched by N people who finished it and M who gave up half way."""
        from shortlist.server.db.models import PickRow, User

        uid = 100
        with sessions() as session:
            for finished in [True] * finishers + [False] * abandoners:
                uid += 1
                session.add(User(id=uid, plex_account_id=uid, username=f"u{uid}", slug=f"u{uid}", enabled=True))
                session.add(
                    PickRow(
                        user_id=uid,
                        tmdb_id=tmdb_id,
                        media_type="movie",
                        rating_key=tmdb_id,
                        rank=1,
                        collection_slug="picked",
                        section_key="1",
                        library="Movies",
                        title="Divisive Film",
                        created_at=NOW - timedelta(days=10),
                        watched_at=NOW - timedelta(days=2),
                        finished_at=NOW - timedelta(days=1) if finished else None,
                        max_percent=None if finished else 50,
                    )
                )
            session.commit()

    def test_a_title_exactly_half_of_whom_finished_it_still_counts_as_losing(self, sessions):
        """`finished * 2 <= started`, not `<`. Two finished out of four is a title that loses half
        the people who try it — the flat boundary, and integers, so it is an ordinary case rather
        than an edge."""
        from shortlist.server.services.report_service import engagement

        self._title_watched_by(sessions, finishers=2, abandoners=2)

        with sessions() as session:
            losing = engagement(session, "30")["losing"]

        assert [t["title"] for t in losing] == ["Divisive Film"], "half the audience giving up is not 'landing'"
        assert (losing[0]["started"], losing[0]["finished"]) == (4, 2)

    def test_a_title_most_people_finish_is_not_losing_anyone(self, sessions):
        """The other side: three of four finishing is a title that works."""
        from shortlist.server.services.report_service import engagement

        self._title_watched_by(sessions, finishers=3, abandoners=2)

        with sessions() as session:
            assert engagement(session, "30")["losing"] == []

    def test_one_abandonment_alone_is_never_a_pattern(self, sessions):
        """`len(percents) >= 2`. A single person giving up on something is a bad night, not a bad
        recommendation, and the heading says so."""
        from shortlist.server.services.report_service import engagement

        self._title_watched_by(sessions, finishers=0, abandoners=1)

        with sessions() as session:
            assert engagement(session, "30")["losing"] == []


class TestAWatchIsNotJudgedTheMomentItStarts:
    """An outcome used to be decided on percentage alone, with no notion of time.

    Reported live 2026-08-24: MooHouse pressed play on Moxie, and the dashboard said "gave up on
    Moxie after 1%" while the session was still open — they were watching it as it said so. Anyone
    who starts something and is a minute in was written off immediately, and "gave up" is the
    loudest thing this report says about a pick.
    """

    def _started(self, sessions, *, watched_ago_hours: float, percent: int, tmdb_id: int = 550):
        from shortlist.server.db.models import PickRow

        with sessions() as session:
            session.add(
                PickRow(
                    user_id=1,
                    tmdb_id=tmdb_id,
                    media_type="movie",
                    rating_key=tmdb_id,
                    rank=1,
                    collection_slug="picked",
                    section_key="1",
                    library="Movies",
                    title="Moxie",
                    created_at=NOW - timedelta(days=5),
                    watched_at=NOW - timedelta(hours=watched_ago_hours),
                    max_percent=percent,
                )
            )
            session.commit()

    def _outcome(self, sessions) -> str:
        from shortlist.server.services.report_service import resolve_outcomes

        with sessions() as session:
            return resolve_outcomes(session, None)[(1, 550, "movie")]["outcome"]

    def test_a_watch_still_in_progress_is_not_an_abandonment(self, sessions):
        """The reported case: playback OPEN, and the report calling it a failure.

        Deliberately dated OUTSIDE the settling window. With a fresh timestamp the time branch
        answers this on its own, and the still-open check could be deleted with every test here
        green — which is exactly what happened to the first version of this test. Someone who paused
        a film yesterday and left the client open is past the window and still has not given up.
        """
        from shortlist.server.db.models import WatchSession

        self._started(sessions, watched_ago_hours=SETTLING_HOURS + 6, percent=1)
        with sessions() as session:
            session.add(
                WatchSession(
                    plex_account_id=7,  # the `sessions` fixture's user 1
                    session_key="live-1",
                    rating_key=550,
                    media_type="movie",
                    started_at=NOW - timedelta(hours=SETTLING_HOURS + 6),
                    last_seen_at=NOW,
                    ended_at=None,  # still playing
                    max_offset_ms=58_000,
                    duration_ms=6_700_000,
                )
            )
            session.commit()

        assert self._outcome(sessions) == "watching", "called it an abandonment while it was playing"

    def test_a_watch_stopped_an_hour_ago_is_still_too_early_to_judge(self, sessions):
        """Pausing at 40% and resuming after dinner is ordinary. Overnight is the window."""
        self._started(sessions, watched_ago_hours=1, percent=40)

        assert self._outcome(sessions) == "watching"

    def test_a_watch_left_for_a_day_is_an_abandonment(self, sessions):
        """The other side — the rule must not make every abandonment invisible."""
        self._started(sessions, watched_ago_hours=SETTLING_HOURS + 1, percent=40)

        assert self._outcome(sessions) == "dropped"

    def test_a_settled_bounce_is_still_a_bounce(self, sessions):
        self._started(sessions, watched_ago_hours=SETTLING_HOURS + 1, percent=1)

        assert self._outcome(sessions) == "bounced"

    def test_finishing_beats_the_settling_rule(self, sessions):
        """A completion is known the moment it happens; there is nothing to wait for."""
        from shortlist.server.db.models import PickRow

        self._started(sessions, watched_ago_hours=0.02, percent=100)
        with sessions() as session:
            session.query(PickRow).update({"finished_at": NOW - timedelta(minutes=1)})
            session.commit()

        assert self._outcome(sessions) == "finished"

    def test_the_histogram_only_counts_what_it_calls_abandoned(self, sessions):
        """The chart and the tile must count one set. Reading the raw percentage here put
        in-progress watches into the histogram while the outcome called them `watching`."""
        from shortlist.server.services.report_service import engagement

        self._started(sessions, watched_ago_hours=1, percent=40, tmdb_id=550)  # too early to judge
        self._started(sessions, watched_ago_hours=SETTLING_HOURS + 2, percent=30, tmdb_id=680)  # settled

        with sessions() as session:
            data = engagement(session, "all")

        assert sum(b["count"] for b in data["stop_points"]) == 1, "an in-progress watch entered the histogram"
