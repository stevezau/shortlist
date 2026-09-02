"""Which days a row is SHOWN (issue #102).

The whole feature turns on one pure function, so it is tested as one: no clock inside it, every
weekday covered, and the ISO convention pinned — Sunday is 7, and a 0 from JavaScript's
`Date.getDay()` must never quietly mean Sunday.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from shortlist.engine.pipeline import identity_map
from shortlist.engine.rows import row_is_shown

# A full week, Monday first. 2026-08-31 is a Monday, and the week crosses a month boundary on
# purpose — the first draft of this test built the week with `31 + offset` and blew up on 32.
MONDAY = datetime(2026, 8, 31, 12, 0)
WEEK = [MONDAY + timedelta(days=offset) for offset in range(7)]


class TestRowIsShown:
    def test_an_empty_schedule_shows_the_row_every_day(self):
        """[] is what every row carries after the upgrade migration — it must change nothing."""
        assert [row_is_shown([], day) for day in WEEK] == [True] * 7

    def test_only_the_chosen_days_show_the_row(self):
        # Mon, Wed, Fri — the reporter's own example.
        shown = [row_is_shown([1, 3, 5], day) for day in WEEK]
        assert shown == [True, False, True, False, True, False, False]

    def test_every_day_chosen_is_the_same_as_an_empty_schedule(self):
        assert [row_is_shown([1, 2, 3, 4, 5, 6, 7], day) for day in WEEK] == [True] * 7

    def test_sunday_is_seven(self):
        """ISO weekdays. The browser's Date.getDay() calls Sunday 0, and a 0 leaking into the stored
        list would silently match nothing — so Sunday must be 7 here and 0 must never match."""
        sunday = WEEK[6]
        assert row_is_shown([7], sunday) is True
        assert row_is_shown([0], sunday) is False

    def test_the_time_of_day_does_not_matter(self):
        """Days only — one minute past midnight and one minute to it are the same day."""
        assert row_is_shown([1], datetime(2026, 8, 31, 0, 1)) is True
        assert row_is_shown([1], datetime(2026, 8, 31, 23, 59)) is True
        assert row_is_shown([1], datetime(2026, 9, 1, 0, 1)) is False

    @pytest.mark.parametrize("junk", [None, ""])
    def test_a_missing_schedule_is_treated_as_every_day(self, junk):
        """The column is JSON and nullable in practice (an older row, a hand-edited database), and
        the safe reading of "no schedule" is the one that shows the row rather than hiding it."""
        assert row_is_shown(junk, MONDAY) is True


class TestIdentityMap:
    """Which row a delivered collection belongs to, from the ledger — the branch that decides whether
    a `{top_seed}` row scheduled off is recognised or silently promoted (issue #102).

    The ambiguous cell is the one that matters: if it ever started ARBITRATING instead of dropping,
    the suite would stay green while a row got another row's placement.
    """

    def test_an_unambiguous_key_identifies_its_row(self):
        keys = {("sarah", "picked", "1"): 9001, ("sarah", "gems", "2"): 9002}

        assert identity_map(keys) == {"sarah": {9001: "picked", 9002: "gems"}}

    def test_a_key_two_rows_claim_is_dropped_rather_than_arbitrated(self):
        """Reachable: only the delivery path writes `removed_deliveries`, so a collection deleted by
        converge or orphan handling leaves a stale entry, and Plex reuses rowids. Dropping sends it to
        the title map; arbitrating would hand it the WRONG row's placement."""
        keys = {("sarah", "picked", "1"): 9001, ("sarah", "gems", "2"): 9001}

        assert identity_map(keys) == {}

    def test_one_ambiguous_key_does_not_discard_the_others(self):
        keys = {("sarah", "picked", "1"): 9001, ("sarah", "gems", "2"): 9001, ("sarah", "solo", "3"): 9003}

        assert identity_map(keys) == {"sarah": {9003: "solo"}}

    def test_two_people_may_hold_the_same_rating_key_without_colliding(self):
        """Keyed per user on purpose. A ratingKey is unique per server, so this is defensive rather
        than reachable — but the map is only ever consulted for collections found under one person's
        own label, so collapsing the users would be the bug, not the guard."""
        keys = {("sarah", "picked", "1"): 9001, ("mike", "picked", "1"): 9001}

        assert identity_map(keys) == {"sarah": {9001: "picked"}, "mike": {9001: "picked"}}

    def test_a_dry_runs_placeholder_key_contributes_nothing(self):
        """Delivery records `rating_key: 0` for a preview — it created no collection."""
        assert identity_map({("sarah", "picked", "1"): 0}) == {}
