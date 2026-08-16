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

    def test_the_trend_segment_never_exceeds_its_own_column(self, sessions):
        seed(sessions, tmdb_id=1, delivered_ago=50, watched_ago=40, finished_ago=2)
        seed(sessions, tmdb_id=2, delivered_ago=6, watched_ago=5, finished_ago=3)

        for point in self._report(sessions, "all")["trend"]:
            assert point["finished"] <= point["watched"], point
