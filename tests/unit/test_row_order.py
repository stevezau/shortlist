"""Every value of `RowSpec.pick_order`, against one fixture, asserted to its exact output.

`test_pipeline.py` proves each order is WIRED UP — that a row configured with it reaches Plex in
that order through the real pipeline. This file proves each order is CORRECT, by calling
`_apply_order` directly on a list whose five picks disagree about every axis at once: the ranking,
the rating, the year, which titles are new, and where a rotation lands.

The fixture is built so no two orders can produce the same list (`test_no_two_orders_agree` holds
that property). That is the point: with a pool whose ratings happen to descend with its ranking —
which is what the pipeline fixture has — `best`, `rating`, `new_first` and `rotate` all return the
same five ids on day 0, and four of the six tests would pass without their feature existing at all.
"""

from __future__ import annotations

import pytest

from shortlist.engine.models import MediaType, Pick
from shortlist.engine.rows import ROW_ORDERS, _apply_order

#: tmdb id -> (rating, year). Deliberately decorrelated: the ranking is 1..5, the ratings peak at 2
#: and the years peak at 1, so "best", "highest rated" and "newest" are three different answers.
_FIXTURE = {
    1: (5.0, 2005),
    2: (9.0, 2001),
    3: (7.0, 2004),
    4: (6.0, 2002),
    5: (8.0, 2003),
}

#: The two titles that "arrived this run" — mid-list, so `new_first` cannot be mistaken for the
#: ranking (which would lead with 1) or for a reversal (which would lead with 5).
_NEW = {(4, MediaType.MOVIE), (5, MediaType.MOVIE)}


def _picks() -> list[Pick]:
    """Five picks in rank order 1..5, each carrying the fixture's rating and year."""
    return [
        Pick(
            tmdb_id=tmdb_id,
            rating_key=1000 + tmdb_id,
            title=f"T{tmdb_id}",
            rank=tmdb_id,
            reason="test",
            media_type=MediaType.MOVIE,
            rating=rating,
            year=year,
        )
        for tmdb_id, (rating, year) in _FIXTURE.items()
    ]


def _order(order: str, *, run_day: int = 2) -> list[int]:
    ordered = _apply_order(
        _picks(),
        order,
        row_slug="picked",
        user_slug="sarah",
        run_day=run_day,
        new_keys=_NEW,
    )
    return [p.tmdb_id for p in ordered]


class TestEveryPickOrder:
    """One test per value of `ROW_ORDERS`, asserting the exact delivered list."""

    def test_best_leaves_the_ranking_untouched(self):
        """The default, and what every pre-existing row is on: it must be a genuine no-op."""
        assert _order("best") == [1, 2, 3, 4, 5]

    def test_rating_leads_with_the_highest_score(self):
        assert _order("rating") == [2, 5, 3, 4, 1]

    def test_newest_leads_with_the_most_recent_release(self):
        assert _order("newest") == [1, 3, 5, 4, 2]

    def test_new_first_leads_with_the_titles_that_arrived_this_run(self):
        """Issue #63's first ask: 4 and 5 arrived, so they lead — each group keeping its rank order."""
        assert _order("new_first") == [4, 5, 1, 2, 3]

    def test_rotate_advances_the_front_by_the_day(self):
        """Issue #63's second ask: a cyclic shift, so the list keeps its relative order and the head
        moves along. Day 2 of a five-title row starts at the third pick."""
        assert _order("rotate", run_day=2) == [3, 4, 5, 1, 2]

    def test_shuffle_is_a_permutation_that_is_not_the_ranking(self):
        """Shuffle's day-to-day and per-user behaviour is covered in `test_pipeline.py`; here it only
        has to be a real reordering of the same five titles rather than a truncation."""
        shuffled = _order("shuffle")

        assert sorted(shuffled) == [1, 2, 3, 4, 5], f"same titles, reordered — got {shuffled}"
        assert shuffled != [1, 2, 3, 4, 5], "a shuffle that returns the ranking is not shuffling"

    @pytest.mark.parametrize("order", ROW_ORDERS)
    def test_every_order_delivers_the_whole_row(self, order):
        """No order may drop, duplicate or invent a pick. Ordering is presentation: the row that goes
        in is the row that comes out. `rotate` is the one most able to break this — an off-by-one in
        the slice would silently shorten every row on the server."""
        assert sorted(_order(order)) == [1, 2, 3, 4, 5]

    def test_no_two_orders_agree(self):
        """The property that makes the assertions above meaningful. If two orders collapse on this
        fixture, one of them is untested here and the fixture needs decorrelating — not the test
        deleting."""
        results = {order: tuple(_order(order)) for order in ROW_ORDERS}

        assert len(set(results.values())) == len(ROW_ORDERS), f"two orders produced the same row: {results}"


class TestPickOrderEdgeCases:
    def test_an_unknown_order_falls_back_to_the_ranking(self):
        """A row carrying an order this build does not know — a downgrade, or a hand-edited DB — must
        deliver in rank order rather than raise mid-run and cost the user their row."""
        assert _order("no_such_order") == [1, 2, 3, 4, 5]

    def test_rotate_survives_an_empty_row(self):
        """`run_day % len(picks)` divides by zero on an empty list. A row can legitimately be empty
        (nothing in the library matched), and an ordering must never be what fails the run."""
        assert _apply_order([], "rotate", row_slug="picked", user_slug="sarah", run_day=3) == []

    def test_rotate_on_a_single_title_row_is_a_no_op(self):
        picks = _picks()[:1]

        ordered = _apply_order(picks, "rotate", row_slug="picked", user_slug="sarah", run_day=7)

        assert [p.tmdb_id for p in ordered] == [1]

    def test_new_first_without_new_keys_keeps_the_ranking(self):
        """The carried-forward path passes nothing, and a row that did not change must not move."""
        ordered = _apply_order(_picks(), "new_first", row_slug="picked", user_slug="sarah", run_day=2)

        assert [p.tmdb_id for p in ordered] == [1, 2, 3, 4, 5]

    def test_new_first_when_everything_is_new_keeps_the_ranking(self):
        """A bootstrap build is all newcomers. Leading with 'the new ones' is then the whole row, so
        the ranking must survive intact rather than being reversed or re-sorted."""
        every = {(p.tmdb_id, p.media_type) for p in _picks()}

        ordered = _apply_order(_picks(), "new_first", row_slug="picked", user_slug="sarah", run_day=2, new_keys=every)

        assert [p.tmdb_id for p in ordered] == [1, 2, 3, 4, 5]
