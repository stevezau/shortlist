"""Dividing a run's request slots between its rows.

The rule: `max_per_run` is the run's ceiling and rows split it evenly; a row that cannot fill its
share hands the surplus back; a row's own `max_per_row` may only restrict it further. A title wanted
by several rows is claimed once, by the first row in run order whose gate it passed — one title, one
slot, so N slots always yield N distinct titles.
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from shortlist.engine.models import MediaType, MissingTitle
from shortlist.engine.request_alloc import allocate


def _title(tmdb_id: int) -> MissingTitle:
    return MissingTitle(tmdb_id, f"t{tmdb_id}", MediaType.MOVIE, 2021, 8.0, 500, demand=1)


def _titles(n: int, start: int = 1) -> list[MissingTitle]:
    return [_title(i) for i in range(start, start + n)]


def _by_row(claims) -> Counter:
    return Counter(slug for slug, _ in claims)


class TestSplittingTheRunCeiling:
    def test_a_single_row_is_bound_by_its_own_max(self):
        """The owner's case: global 10, one row capped at 3 -> 3, not 10."""
        claims = allocate([("picked", _titles(20))], cap=10, row_caps={"picked": 3})
        assert len(claims) == 3

    def test_a_single_uncapped_row_may_use_the_whole_ceiling(self):
        claims = allocate([("picked", _titles(20))], cap=10, row_caps={})
        assert len(claims) == 10

    def test_two_rows_split_the_ceiling_evenly(self):
        claims = allocate([("a", _titles(20)), ("b", _titles(20, 100))], cap=10, row_caps={})
        assert _by_row(claims) == {"a": 5, "b": 5}

    def test_three_rows_split_evenly_with_the_remainder_to_the_earlier_rows(self):
        """10 across 3 is 4/3/3 — deterministic by run order, never arbitrary."""
        claims = allocate([("a", _titles(9)), ("b", _titles(9, 100)), ("c", _titles(9, 200))], cap=10, row_caps={})
        assert [_by_row(claims)[k] for k in ("a", "b", "c")] == [4, 3, 3]


class TestSurplusRedistribution:
    """A ceiling is a ceiling, not a target — but leaving slots idle while a row has good titles
    waiting is just lost work."""

    def test_a_row_capped_below_its_share_hands_the_surplus_back(self):
        """global 10, A capped at 3, B uncapped -> 3 + 7, not 3 + 5 with two slots idle."""
        claims = allocate([("a", _titles(20)), ("b", _titles(20, 100))], cap=10, row_caps={"a": 3})
        assert _by_row(claims) == {"a": 3, "b": 7}

    def test_a_row_short_of_titles_also_hands_its_surplus_back(self):
        claims = allocate([("a", _titles(2)), ("b", _titles(20, 100))], cap=10, row_caps={})
        assert _by_row(claims) == {"a": 2, "b": 8}

    def test_surplus_redistributes_across_more_than_one_round(self):
        """A hands back to B and C; then B is also short and hands back again. A single pass would
        stop at 8 and leave two slots unused."""
        claims = allocate([("a", _titles(1)), ("b", _titles(2, 100)), ("c", _titles(50, 200))], cap=12, row_caps={})
        assert _by_row(claims) == {"a": 1, "b": 2, "c": 9}
        assert len(claims) == 12

    def test_every_row_short_means_the_ceiling_is_simply_not_reached(self):
        claims = allocate([("a", _titles(1)), ("b", _titles(1, 100))], cap=10, row_caps={})
        assert len(claims) == 2

    def test_a_row_cap_of_zero_excludes_it_entirely(self):
        claims = allocate([("a", _titles(20)), ("b", _titles(20, 100))], cap=10, row_caps={"a": 0})
        assert _by_row(claims) == {"b": 10}


class TestCollisions:
    """One title, one slot — whichever rows wanted it."""

    def test_a_title_in_two_rows_is_claimed_by_the_earlier_row(self):
        shared = _title(550)
        claims = allocate([("a", [shared]), ("b", [shared, *_titles(5, 100)])], cap=10, row_caps={})
        assert ("a", 550) in [(s, t.tmdb_id) for s, t in claims]
        assert ("b", 550) not in [(s, t.tmdb_id) for s, t in claims]

    def test_a_claimed_title_frees_the_other_rows_slot(self):
        """It consumes ONE slot in total, so 10 slots still yield 10 distinct titles."""
        shared = _title(550)
        claims = allocate([("a", [shared]), ("b", [shared, *_titles(20, 100)])], cap=10, row_caps={})
        assert len(claims) == 10
        assert len({t.tmdb_id for _, t in claims}) == 10

    def test_the_same_id_in_different_namespaces_is_two_titles(self):
        """Movie 550 and show 550 are different titles — the same rule filter_candidates follows."""
        movie = MissingTitle(550, "movie", MediaType.MOVIE, 2021, 8.0, 500, demand=1)
        show = MissingTitle(550, "show", MediaType.SHOW, 2021, 8.0, 500, demand=1)
        claims = allocate([("a", [movie]), ("b", [show])], cap=10, row_caps={})
        assert len(claims) == 2

    def test_a_title_in_three_rows_is_still_claimed_once(self):
        shared = _title(550)
        claims = allocate([("a", [shared]), ("b", [shared]), ("c", [shared])], cap=10, row_caps={})
        assert len(claims) == 1

    def test_a_row_whose_only_title_was_claimed_elsewhere_contributes_nothing(self):
        shared = _title(550)
        claims = allocate([("a", [shared, *_titles(3, 100)]), ("b", [shared])], cap=10, row_caps={})
        assert _by_row(claims) == {"a": 4}


class TestEdges:
    def test_no_rows_yields_nothing(self):
        assert allocate([], cap=10, row_caps={}) == []

    def test_rows_with_no_titles_yield_nothing(self):
        assert allocate([("a", []), ("b", [])], cap=10, row_caps={}) == []

    def test_a_zero_ceiling_sends_nothing(self):
        assert allocate([("a", _titles(5))], cap=0, row_caps={}) == []

    def test_a_negative_ceiling_is_treated_as_zero(self):
        assert allocate([("a", _titles(5))], cap=-1, row_caps={}) == []

    def test_row_order_is_preserved_within_a_row(self):
        """Each row's list arrives already ranked best-first; allocation must not reorder it."""
        claims = allocate([("a", _titles(3))], cap=3, row_caps={})
        assert [t.tmdb_id for _, t in claims] == [1, 2, 3]

    def test_a_cap_larger_than_everything_available_takes_everything(self):
        claims = allocate([("a", _titles(3)), ("b", _titles(2, 100))], cap=100, row_caps={})
        assert len(claims) == 5

    def test_an_unknown_row_cap_key_is_ignored(self):
        claims = allocate([("a", _titles(5))], cap=10, row_caps={"ghost": 1})
        assert len(claims) == 5


class TestAllocatorInvariants:
    """Property audit: five invariants that must hold for ANY row/cap shape, not just the cells
    above. Verified to bite by mutation — single-pass redistribution, dropping the cross-row dedup,
    and ignoring `max_per_row` each fail it."""

    @given(
        rows=st.lists(
            st.tuples(
                st.text(alphabet="abcdefg", min_size=1, max_size=3),
                st.lists(st.integers(min_value=1, max_value=30).map(_title), max_size=12),
            ),
            max_size=6,
        ),
        cap=st.integers(min_value=-3, max_value=25),
        caps=st.dictionaries(
            st.text(alphabet="abcdefg", min_size=1, max_size=3),
            st.integers(min_value=0, max_value=8),
            max_size=6,
        ),
    )
    @settings(max_examples=400, deadline=None)
    def test_every_shape_holds_the_invariants(self, rows, cap, caps):
        seen, deduped = set(), []
        for slug, titles in rows:  # one entry per row, as the caller builds it
            if slug not in seen:
                seen.add(slug)
                deduped.append((slug, titles))

        claims = allocate(deduped, cap=cap, row_caps=caps)

        assert len(claims) <= max(0, cap), "the run ceiling is never exceeded"
        keys = [(t.tmdb_id, t.media_type) for _, t in claims]
        assert len(keys) == len(set(keys)), "one title, one slot"

        per_row: Counter = Counter(slug for slug, _ in claims)
        for slug, taken in per_row.items():
            if slug in caps:
                assert taken <= caps[slug], f"{slug} exceeded its own cap"

        offered = {slug: {(t.tmdb_id, t.media_type) for t in titles} for slug, titles in deduped}
        for slug, title in claims:
            assert (title.tmdb_id, title.media_type) in offered[slug], "a row claimed what it never offered"

        # Work-conserving: an unused slot must mean no row could have filled it. This is the one that
        # catches a redistribution that gives up early.
        if len(claims) < max(0, cap):
            claimed = set(keys)
            for slug, titles in deduped:
                room = caps.get(slug, 10**9) - per_row.get(slug, 0)
                leftover = [t for t in titles if (t.tmdb_id, t.media_type) not in claimed]
                assert room <= 0 or not leftover, f"{slug} could still have filled a slot"
