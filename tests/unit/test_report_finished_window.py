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
from shortlist.server.services.report_service import effectiveness, row_effectiveness

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
            overall = effectiveness(session, "30")["overall"]

        assert overall["watched"] == 1
        assert overall["watched_prev"] is None, "there was no previous period to count"
        assert overall["watched_delta"] is None, "a comparison against an uninstalled app is not growth"
        assert overall["avg_days_to_watch_delta"] is None, "already None; the pair must agree"

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
