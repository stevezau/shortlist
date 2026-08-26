"""The write plan that makes one account's watch state match another's.

The bug this module exists to prevent is concrete and shipped: the old transfer scrobbled a SHOW's
rating key, so a person 400 episodes into One Piece arrived on their new account with all 1,100
marked watched. On the maintainer's own account 342 of 535 watched shows are partial, so that was
the majority case, not an edge.

See .claude/docs/watching-account-transfer-design.md for the probed Plex behaviour these rules
encode.
"""

from __future__ import annotations

import pytest

from shortlist.engine.watch_replica import (
    ItemState,
    OpKind,
    WatchState,
    build_plan,
)


def movie(key: int, *, count: int = 0, offset: int = 0, at: int = 0) -> ItemState:
    return ItemState(rating_key=key, media_type="movie", view_count=count, view_offset_ms=offset, last_viewed_at=at)


def episode(key: int, show: int, *, count: int = 0, offset: int = 0, at: int = 0) -> ItemState:
    return ItemState(
        rating_key=key,
        media_type="episode",
        view_count=count,
        view_offset_ms=offset,
        last_viewed_at=at,
        show_rating_key=show,
    )


def state(*items: ItemState) -> WatchState:
    return WatchState(items={i.rating_key: i for i in items})


EMPTY = WatchState(items={})


class TestMarking:
    def test_a_watched_movie_is_marked_once(self):
        plan = build_plan(state(movie(1, count=1)), EMPTY)

        assert [(op.kind, op.rating_key, op.view_count) for op in plan] == [(OpKind.MARK, 1, 1)]

    def test_a_rewatched_movie_carries_its_full_count(self):
        # Probed live: scrobbling the same key three times leaves viewCount=3, so a rewatch is
        # replicable rather than flattened to "watched".
        plan = build_plan(state(movie(1, count=3)), EMPTY)

        assert [(op.kind, op.view_count) for op in plan] == [(OpKind.MARK, 3)]

    def test_a_partly_watched_show_marks_only_the_episodes_watched(self):
        """The One Piece case. Three of a hundred episodes watched marks three keys, never the show."""
        source = state(episode(11, show=9), episode(12, show=9, count=1), episode(13, show=9, count=1))

        plan = build_plan(source, EMPTY)

        assert {op.rating_key for op in plan} == {12, 13}
        assert 9 not in {op.rating_key for op in plan}

    def test_the_show_key_is_never_written_even_when_every_episode_is_watched(self):
        """A show-key scrobble marks all episodes but leaves the show's own viewCount unset, so the
        show goes missing from `?type=2&unwatched=0` — the read the watch cache is built from."""
        source = state(episode(11, show=9, count=1), episode(12, show=9, count=1))

        plan = build_plan(source, EMPTY)

        assert {op.rating_key for op in plan} == {11, 12}


class TestPartialProgress:
    def test_a_film_started_and_never_finished_gets_an_offset_and_no_mark(self):
        plan = build_plan(state(movie(1, count=0, offset=490_509)), EMPTY)

        assert [(op.kind, op.offset_ms) for op in plan] == [(OpKind.SET_OFFSET, 490_509)]

    def test_a_film_both_watched_and_in_progress_is_marked_then_positioned(self):
        """Probed live on `10 Cloverfield Lane`: scrobble then progress reproduces both, in that
        order. The order is the assertion — reversing it loses the offset."""
        plan = build_plan(state(movie(1, count=1, offset=490_509)), EMPTY)

        assert [(op.kind, op.rating_key) for op in plan] == [(OpKind.MARK, 1), (OpKind.SET_OFFSET, 1)]

    def test_an_in_progress_episode_gets_its_offset(self):
        plan = build_plan(state(episode(11, show=9, offset=1_234_000)), EMPTY)

        assert [(op.kind, op.rating_key, op.offset_ms) for op in plan] == [(OpKind.SET_OFFSET, 11, 1_234_000)]


class TestMirroring:
    def test_a_watch_the_source_does_not_have_is_removed(self):
        plan = build_plan(EMPTY, state(movie(1, count=1)))

        assert [(op.kind, op.rating_key) for op in plan] == [(OpKind.UNMARK, 1)]

    def test_the_stray_episodes_an_old_show_key_transfer_left_are_removed(self):
        """The repair case. The old transfer marked all 1,100 episodes; only 2 were really watched.
        Add-only leaves the other 1,098 marked for ever."""
        source = state(episode(11, show=9, count=1), episode(12, show=9, count=1))
        target = state(*[episode(k, show=9, count=1) for k in range(11, 111)])

        plan = build_plan(source, target)

        assert {op.rating_key for op in plan if op.kind is OpKind.UNMARK} == set(range(13, 111))
        assert not [op for op in plan if op.kind is OpKind.MARK]

    def test_an_offset_the_source_does_not_have_is_cleared(self):
        plan = build_plan(EMPTY, state(movie(1, offset=500_000)))

        assert [(op.kind, op.rating_key) for op in plan] == [(OpKind.CLEAR_OFFSET, 1)]

    def test_a_count_that_is_too_high_is_reset_rather_than_topped_up(self):
        """Scrobbling only ever increments, so an over-count can only be fixed by clearing first."""
        plan = build_plan(state(movie(1, count=1)), state(movie(1, count=4)))

        assert [(op.kind, op.view_count) for op in plan] == [(OpKind.UNMARK, 0), (OpKind.MARK, 1)]

    def test_a_count_that_is_too_low_is_topped_up_in_place(self):
        plan = build_plan(state(movie(1, count=3)), state(movie(1, count=1)))

        assert [(op.kind, op.view_count) for op in plan] == [(OpKind.MARK, 3)]

    def test_topping_up_asks_for_the_shortfall_not_the_total(self):
        """A scrobble only ever adds one. Sending the TOTAL against a film already watched once takes
        it to four, so the count drifts up on every re-run and never reaches a fixed point."""
        plan = build_plan(state(movie(1, count=3)), state(movie(1, count=1)))

        assert (plan[0].view_count, plan[0].scrobbles) == (3, 2)

    def test_a_rebuild_after_an_unmark_asks_for_the_whole_count(self):
        """The un-mark zeroes it first, so here the shortfall IS the total."""
        plan = build_plan(state(movie(1, count=2)), state(movie(1, count=5)))

        mark = next(op for op in plan if op.kind is OpKind.MARK)
        assert (mark.view_count, mark.scrobbles) == (2, 2)

    def test_an_identical_account_needs_no_writes(self):
        both = state(movie(1, count=2, offset=1000), episode(11, show=9, count=1))

        assert build_plan(both, both) == []

    def test_applying_a_plan_twice_asks_for_nothing_the_second_time(self):
        """The fixed-point property. `apply` is modelled here rather than mocked, so the plan and the
        state model cannot drift apart silently."""
        source = state(movie(1, count=3, offset=90_000), episode(11, show=9, count=1), movie(2, offset=5_000))
        target = state(movie(1, count=9), movie(7, count=1), episode(12, show=9, count=1))

        once = apply_plan(target, build_plan(source, target))

        assert build_plan(source, once) == []


class TestOrder:
    def test_removals_come_before_additions(self):
        """The account is cleared, then filled. A removal landing mid-fill could clear a key the fill
        had already written when the same title is both."""
        source = state(movie(1, count=1, at=500))
        target = state(movie(2, count=1))

        plan = build_plan(source, target)

        assert [op.kind for op in plan] == [OpKind.UNMARK, OpKind.MARK]

    def test_additions_are_written_oldest_first(self):
        """Plex stamps every write `now` and takes no date, so absolute dates are lost either way.
        Writing in the source's order makes the target's Continue Watching sort the same way."""
        source = state(movie(1, count=1, at=300), movie(2, count=1, at=100), movie(3, count=1, at=200))

        plan = build_plan(source, EMPTY)

        assert [op.rating_key for op in plan] == [2, 3, 1]

    def test_an_undated_watch_sorts_before_dated_ones_rather_than_after(self):
        """`last_viewed_at` is 0 when Plex reported none. Sorting it last would put an unknown-date
        watch at the top of Continue Watching, which is the most visible shelf on the account."""
        source = state(movie(1, count=1, at=0), movie(2, count=1, at=100))

        plan = build_plan(source, EMPTY)

        assert [op.rating_key for op in plan] == [1, 2]


class TestOffsetTolerance:
    @pytest.mark.parametrize("delta", [0, 999])
    def test_an_offset_within_a_second_is_left_alone(self, delta):
        """Plex rounds offsets it echoes back. Rewriting on a sub-second difference would make every
        re-run report thousands of changes and never reach a fixed point."""
        source = state(movie(1, offset=500_000))
        target = state(movie(1, offset=500_000 + delta))

        assert build_plan(source, target) == []

    def test_an_offset_off_by_more_than_a_second_is_rewritten(self):
        source = state(movie(1, offset=500_000))
        target = state(movie(1, offset=520_000))

        assert [op.kind for op in build_plan(source, target)] == [OpKind.SET_OFFSET]


def apply_plan(target: WatchState, plan: list) -> WatchState:
    """A model of what the PMS does, used to prove the plan reaches a fixed point.

    Mirrors the probed behaviour: MARK sets viewCount to the requested total, UNMARK zeroes it,
    SET_OFFSET sets the position, CLEAR_OFFSET zeroes it.
    """
    items = {k: v for k, v in target.items.items()}
    for op in plan:
        cur = items.get(
            op.rating_key,
            ItemState(rating_key=op.rating_key, media_type=op.media_type, show_rating_key=op.show_rating_key),
        )
        if op.kind is OpKind.MARK:
            # A scrobble clears an existing offset (measured), so the model must too — otherwise the
            # fixed-point checks below agree with a server that does something else.
            cur = replace_state(cur, view_count=op.view_count, view_offset_ms=0)
        elif op.kind is OpKind.SET_OFFSET:
            cur = replace_state(cur, view_offset_ms=op.offset_ms)
        else:
            # UNMARK and CLEAR_OFFSET are the same call — `/:/unscrobble` — and it zeroes BOTH.
            # Modelling CLEAR_OFFSET as touching only the offset is what made the live undo look
            # correct here while leaving 293 items part-watched on a real account.
            cur = replace_state(cur, view_count=0, view_offset_ms=0)
        items[op.rating_key] = cur
    return WatchState(items={k: v for k, v in items.items() if v.view_count or v.view_offset_ms})


def replace_state(item: ItemState, **kw) -> ItemState:
    import dataclasses

    return dataclasses.replace(item, **kw)


class TestClearingAnOffsetIsAFullReset:
    """Found on a LIVE undo: 293 items were left part-watched while the run reported success.

    The plan sent `/:/progress?time=0`, which a real server silently ignores — an offset of 1,139,347
    was still 1,139,347 afterwards. Only `/:/unscrobble` clears one, and it zeroes the view count too,
    so clearing an offset can never be a surgical edit.
    """

    def test_clearing_an_offset_alone_is_reported_as_a_rewind(self):
        plan = build_plan(EMPTY, state(movie(1, offset=500_000)))

        assert [(op.kind, op.rating_key) for op in plan] == [(OpKind.CLEAR_OFFSET, 1)]

    def test_a_watched_title_being_rewound_keeps_its_count(self):
        """The case the naive fix breaks. Un-scrobbling to clear the offset also zeroes the count, so
        the count has to be rebuilt from zero — not left, and not topped up from a stale reading."""
        source = state(movie(1, count=2))
        target = state(movie(1, count=2, offset=500_000))

        plan = build_plan(source, target)

        assert [(op.kind, op.view_count, op.scrobbles) for op in plan] == [
            (OpKind.CLEAR_OFFSET, 0, 0),
            (OpKind.MARK, 2, 2),
        ]

    def test_the_rebuild_asks_for_the_whole_count_not_the_shortfall(self):
        """`scrobbles` must be the TOTAL here: the reset already took the count to zero, so treating
        this like a top-up would leave it short."""
        plan = build_plan(state(movie(1, count=3)), state(movie(1, count=3, offset=9_000)))

        mark = next(op for op in plan if op.kind is OpKind.MARK)
        assert mark.scrobbles == 3

    def test_an_over_count_that_also_needs_rewinding_is_one_reset(self):
        """Both reasons to reset at once must not emit two clears."""
        plan = build_plan(state(movie(1, count=1)), state(movie(1, count=5, offset=9_000)))

        kinds = [op.kind for op in plan]
        assert kinds == [OpKind.UNMARK, OpKind.MARK]

    def test_a_rewind_reaches_a_fixed_point(self):
        """The live failure in miniature: applying the plan must leave nothing for a second pass."""
        source = state(movie(1, count=2), movie(2, count=0, offset=0))
        target = state(movie(1, count=2, offset=500_000), movie(3, offset=90_000))

        once = apply_plan(target, build_plan(source, target))

        assert build_plan(source, once) == []

    def test_a_reset_item_is_repositioned_even_when_the_offsets_already_agree(self):
        """The offset survives the read but not the plan.

        `SET_OFFSET` was decided against the offset as READ, before the plan ran. But the reset above
        it is `/:/unscrobble`, which zeroes the offset too — so an item being reset whose offsets
        already match got no `SET_OFFSET`, and its position was silently lost.

        Every other case in this class has a source offset of 0, which is exactly why none of them
        caught it. The undo path hits it routinely: a transfer raises a count without touching an
        already-matching offset, so the restore has to put both back.
        """
        source = state(movie(1, count=1, offset=490_509))
        target = state(movie(1, count=3, offset=490_509))

        plan = build_plan(source, target)

        assert [op.kind for op in plan] == [OpKind.UNMARK, OpKind.MARK, OpKind.SET_OFFSET]
        assert plan[-1].offset_ms == 490_509

    def test_that_case_reaches_a_fixed_point(self):
        """It did not: a second pass emitted the SET_OFFSET the first had skipped."""
        source = state(movie(1, count=1, offset=490_509))
        target = state(movie(1, count=3, offset=490_509))

        once = apply_plan(target, build_plan(source, target))

        assert build_plan(source, once) == []

    def test_a_rewind_and_a_reposition_are_not_confused(self):
        """Reset for an over-count, and the source wants a DIFFERENT position afterwards."""
        source = state(movie(1, count=1, offset=90_000))
        target = state(movie(1, count=3, offset=490_509))

        plan = build_plan(source, target)

        assert [(op.kind, op.offset_ms) for op in plan if op.kind is OpKind.SET_OFFSET] == [(OpKind.SET_OFFSET, 90_000)]

    def test_a_top_up_repositions_because_the_scrobble_clears_the_offset(self):
        """Probed live: `/:/scrobble` clears an offset the item already carries — 480,000 read back
        as 0 after one scrobble. So ANY mark, not just a reset, has to be followed by the reposition.

        The original probe only ever measured scrobble-then-progress on a FRESH item, which cannot
        answer this. Without it, a title that is both watched and part-way through loses its position
        every time its count is topped up.
        """
        source = state(movie(1, count=3, offset=490_509))
        target = state(movie(1, count=1, offset=490_509))

        plan = build_plan(source, target)

        assert [op.kind for op in plan] == [OpKind.MARK, OpKind.SET_OFFSET]
        assert plan[-1].offset_ms == 490_509

    def test_an_untouched_matching_offset_is_still_left_alone(self):
        """The counterweight: nothing is written when neither the count nor the position changes, or
        every re-run would rewrite every position on the account."""
        both = state(movie(1, count=2, offset=490_509))

        assert build_plan(both, both) == []


def test_every_combination_reaches_a_fixed_point():
    """Exhaustive: 625 cells of (want_count, want_offset, have_count, have_offset).

    Individual cases keep missing each other — the offset-after-reset bug and the offset-after-mark
    bug both sat inside classes whose every case happened to use a source offset of 0. A plan that
    does not converge means a re-run keeps writing, and on this feature "keeps writing" means counts
    that climb and positions that move.
    """
    counts = (0, 1, 2, 3, 5)
    offsets = (0, 90_000, 490_509, 490_900, 900_000)
    bad = []
    for want_count in counts:
        for want_offset in offsets:
            for have_count in counts:
                for have_offset in offsets:
                    src = state(movie(1, count=want_count, offset=want_offset))
                    tgt = state(movie(1, count=have_count, offset=have_offset))
                    once = apply_plan(tgt, build_plan(src, tgt))
                    if build_plan(src, once):
                        bad.append((want_count, want_offset, have_count, have_offset))
    assert bad == []


class TestRemovalsAreNamed:
    """`removals_by_title` says "By title, not by count… this is the only destructive path".

    On the UNDO path `want` is rebuilt from a snapshot, which stores no titles — so every removal for
    a key the snapshot also holds came out as "ratingKey 12345". That is the listing someone is asked
    to approve before watches are deleted, and it defeated the contract on the MORE destructive of
    the two mirrors: the one that removes what was watched after the copy.
    """

    def test_a_removal_is_named_even_when_the_wanted_side_has_no_title(self):
        from shortlist.engine.watch_replica import removals_by_title

        # `want` as the snapshot rebuilds it: no title. `have` is the live read, which always has one.
        want = WatchState(items={10: ItemState(rating_key=10, media_type="movie", view_count=1)})
        have = WatchState(
            items={
                10: ItemState(rating_key=10, media_type="movie", view_count=3, title="Jaws"),
                20: ItemState(rating_key=20, media_type="movie", view_count=1, title="Alien"),
            }
        )

        names = removals_by_title(build_plan(want, have))

        assert names == ["Jaws", "Alien"]
        assert not any(n.startswith("ratingKey ") for n in names)

    def test_it_still_falls_back_when_neither_side_has_a_name(self):
        """Better than crashing, and it only happens when Plex itself returned no title."""
        from shortlist.engine.watch_replica import removals_by_title

        have = WatchState(items={20: ItemState(rating_key=20, media_type="movie", view_count=1)})

        assert removals_by_title(build_plan(EMPTY, have)) == ["ratingKey 20"]
