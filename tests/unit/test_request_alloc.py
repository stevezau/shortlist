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

        # Ownership: a title is claimed by the earliest row that offered it and had room, so any
        # EARLIER row that also offered it must have ended at its own cap. Suggested by the second
        # architecture review — one line here is cheaper than more hand-written cells if the
        # allocator is touched again.
        for slug, title in claims:
            key = (title.tmdb_id, title.media_type)
            for earlier, titles in deduped:
                if earlier == slug:
                    break
                if key in {(t.tmdb_id, t.media_type) for t in titles}:
                    cap = caps.get(earlier)
                    assert cap is not None and per_row.get(earlier, 0) >= cap, (
                        f"{earlier} offered {key} first and had room, but {slug} claimed it"
                    )

        # Work-conserving: an unused slot must mean no row could have filled it. This is the one that
        # catches a redistribution that gives up early.
        if len(claims) < max(0, cap):
            claimed = set(keys)
            for slug, titles in deduped:
                room = caps.get(slug, 10**9) - per_row.get(slug, 0)
                leftover = [t for t in titles if (t.tmdb_id, t.media_type) not in claimed]
                assert room <= 0 or not leftover, f"{slug} could still have filled a slot"


class TestTheWalkClearsItsOwnCachedHead:
    """Audit round 24, 2026-08-18: the walk limit has to be longer than the head of cached titles the
    gate's own caching creates, or a row is starved by its own cache — the original bug, in miniature.

    Two things pile up in front of the frontier at the spend rate: deferred rejects (60 days) and
    near misses on the normal weekly re-check (7 days). Counting only the rejects left a 60-day head
    against a 61-budget walk, which the near misses then overflowed.
    """

    def test_the_walk_outlasts_both_kinds_of_cached_head(self):
        from shortlist.engine.clients.mdblist import RATING_CACHE_TTL_S
        from shortlist.engine.requests import _REJECT_RECHECK_TTL_S, _walk_limit

        day = 24 * 3600
        for budget in (20, 50, 100, 400):
            head = budget * (_REJECT_RECHECK_TTL_S // day) + budget * (RATING_CACHE_TTL_S // day)
            assert _walk_limit(budget) > head, f"a {budget} budget cannot see past its own cache"

    def test_it_leaves_room_for_a_whole_run_of_new_ground(self):
        """Clearing the head by one title would let a run reach exactly one new title a night."""
        from shortlist.engine.clients.mdblist import RATING_CACHE_TTL_S
        from shortlist.engine.requests import _REJECT_RECHECK_TTL_S, _walk_limit

        day = 24 * 3600
        budget = 100
        head = budget * ((_REJECT_RECHECK_TTL_S + RATING_CACHE_TTL_S) // day)
        assert _walk_limit(budget) - head >= budget


class TestTheEarlierRowOwnsAContestedTitle:
    """Architecture review, 2026-08-18 (HIGH). `allocate`'s docstring, `_dedupe_queued` and the
    shipped guide all promise the FIRST row in run order gets a title several rows want — and the
    code gave it to whichever row's per-round allowance happened to reach it first. That decides
    which root folder and quality profile it lands in, so the owner sees the mis-file in Radarr."""

    def test_the_earlier_row_wins_at_every_cap(self):
        """main's own slot went to a higher-demand title, so kids used to take the contested one."""
        shared = _title(9)
        for cap in (2, 3, 4):
            claims = allocate([("main", [_title(100), shared]), ("kids", [shared])], cap=cap, row_caps={})
            owner = next(slug for slug, t in claims if t.tmdb_id == 9)
            assert owner == "main", f"at cap {cap} the later row took it"

    def test_ownership_falls_through_when_the_earlier_row_has_no_room(self):
        """ "The first row whose settings it passes" — a row at its own cap does not pass. Holding the
        title there would leave a slot idle while a later row had it ready."""
        shared = _title(9)
        claims = allocate([("capped", [_title(100), shared]), ("open", [shared])], cap=5, row_caps={"capped": 1})
        assert ("open", 9) in [(s, t.tmdb_id) for s, t in claims]
        assert len(claims) == 2, "the surplus slot must still be usable"

    def test_a_row_that_can_never_take_does_not_hold_a_title_hostage(self):
        shared = _title(9)
        claims = allocate([("zero", [shared]), ("open", [shared])], cap=3, row_caps={"zero": 0})
        assert [(s, t.tmdb_id) for s, t in claims] == [("open", 9)]

    def test_a_skipped_title_is_not_discarded_from_the_later_rows_queue(self):
        """It is only borrowed by the earlier row's claim on it. Popping it while passing lost it for
        good, so the run came up short — which is what the property test caught."""
        shared = _title(4)
        claims = allocate(
            [("a", [_title(1), _title(2), _title(3), _title(6), shared]), ("b", [shared, _title(5)])],
            cap=6,
            row_caps={"a": 4},
        )
        assert len(claims) == 6
        assert ("b", 4) in [(s, t.tmdb_id) for s, t in claims]

    def test_a_later_row_cannot_take_a_title_the_earlier_row_still_owns(self):
        """The case `_can_take` alone does not cover, found by mutation testing (round 35).

        Row A's allowance runs out mid-round while it still owns an unclaimed title. A is live, B is
        live, and B's queue reaches A's title before B's own — so without the ownership check inside
        `_drain`, B claims a title A owns and files it into B's folder. `_can_take` cannot catch it:
        B legitimately owns something else, so it IS live.

        Verified to bite: with `_drain`'s ownership check stubbed out this returns
        [('a', 1), ('b', 2)] — b taking a's title. (An earlier attempt at this mutation silently
        matched nothing after the check was split across two `if`s, and appeared to prove the line
        redundant. A mutation that changes no behaviour is a mutation that did not apply.)
        """
        x, z, w = _title(1), _title(2), _title(3)
        claims = allocate([("a", [x, z]), ("b", [z, w])], cap=2, row_caps={})

        assert [(s, t.tmdb_id) for s, t in claims] == [("a", 1), ("b", 3)]
        assert 2 not in [t.tmdb_id for _, t in claims], "z is owned by a, which had no slot left"
