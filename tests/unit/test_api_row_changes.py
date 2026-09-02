"""The row-edit decision table: what a `PATCH /collections/{id}` owes Plex.

This used to be eleven mutable flags and eight conditional dispatches inside a 200-line handler, so
every one of these cases needed an app, a session and a Plex context to reach. As a pure function
over two snapshots of a row, each branch is one assertion.
"""

from __future__ import annotations

import pytest

from shortlist.server.api.row_changes import (
    POSTER_RESET,
    PRIVACY_SYNC,
    RECONCILE,
    RENAME,
    VISIBILITY,
    RowChange,
    plan_row_changes,
)


def make_change(**overrides) -> RowChange:
    """A row that a PATCH left completely alone — every pair equal. Override one side to move it."""
    base = dict(
        slug="gems",
        build_before="per_person",
        build_after="per_person",
        enabled_before=True,
        enabled_after=True,
        media_before="both",
        media_after="both",
        libraries_before=(),
        libraries_after=(),
        audience_before=frozenset({1, 2}),
        audience_after=frozenset({1, 2}),
        template_before="",
        template_after="",
        poster_mode_before="",
        poster_mode_after="",
        days_before=(),
        days_after=(),
    )
    return RowChange(**{**base, **overrides})


def never_called() -> set[str]:
    raise AssertionError("the Plex read for stranded libraries must not happen when nothing moved")


class TestNothingChanged:
    def test_an_edit_that_moves_nothing_owes_plex_nothing(self):
        assert plan_row_changes(make_change(), never_called) == []

    def test_a_row_that_kept_its_libraries_never_pays_for_the_plex_read(self):
        """`stranded_sections` is a callable precisely so a routine save (an enable toggle, a size
        change) does not open a PMS connection to answer a question with a known answer."""
        plan_row_changes(make_change(enabled_before=True, enabled_after=True), never_called)


class TestBuildFlip:
    def test_a_build_flip_removes_the_old_builds_collections_and_rewrites_every_filter(self):
        plan = plan_row_changes(make_change(build_after="shared"), never_called)

        assert [(w.kind, w.scope) for w in plan] == [
            (RECONCILE, "collection.build"),
            (PRIVACY_SYNC, "row 'gems' changed how it is built"),
        ]

    def test_a_build_flip_supersedes_every_other_change_in_the_same_patch(self):
        """The old build's collections are being removed wholesale, so renaming or un-postering them
        is work against something that is about to be gone. Anything else in the same PATCH lands on
        the next run's rebuild instead."""
        plan = plan_row_changes(
            make_change(
                build_after="shared",
                enabled_before=True,
                enabled_after=False,
                audience_after=frozenset({1}),
                template_before="Old",
                template_after="New",
                poster_mode_before="text",
                poster_mode_after="",
            ),
            never_called,
        )

        assert [w.kind for w in plan] == [RECONCILE, PRIVACY_SYNC]


class TestAudience:
    def test_dropping_someone_from_a_per_person_row_removes_only_their_copy(self):
        plan = plan_row_changes(make_change(audience_after=frozenset({1})), never_called)

        assert len(plan) == 1
        assert (plan[0].kind, plan[0].scope, plan[0].only_user_ids) == (RECONCILE, "collection.audience", [2])

    def test_adding_someone_to_a_per_person_row_removes_nothing(self):
        """A newly-added user's row is a CREATE, and creates are the next run's gated delivery —
        never a mutation handler's, which is what keeps the leak-safe ordering intact."""
        assert plan_row_changes(make_change(audience_after=frozenset({1, 2, 3})), never_called) == []

    @pytest.mark.parametrize(
        "after, why",
        [
            (frozenset({1}), "narrowed — the dropped account is owed an exclude"),
            (frozenset({1, 2, 3}), "widened — the added account is owed that exclude's removal"),
        ],
    )
    def test_a_shared_rows_audience_moving_either_way_rewrites_the_share_filters(self, after, why):
        """A shared row is ONE collection, so deleting nothing can hide it from one person — the only
        mechanism is a `label!=` exclude, and both directions of the change owe one."""
        plan = plan_row_changes(
            make_change(build_before="shared", build_after="shared", audience_after=after), never_called
        )

        assert [(w.kind, w.scope) for w in plan] == [(PRIVACY_SYNC, "the audience for row 'gems' changed")], why

    def test_a_shared_row_never_deletes_a_dropped_persons_copy(self):
        """There isn't one. Queuing a per-user removal here would delete the row for EVERYONE."""
        plan = plan_row_changes(
            make_change(build_before="shared", build_after="shared", audience_after=frozenset({1})), never_called
        )

        assert not any(w.kind == RECONCILE for w in plan)


class TestEnabled:
    def test_switching_a_row_off_takes_its_collections_down(self):
        plan = plan_row_changes(make_change(enabled_after=False), never_called)

        assert [(w.kind, w.scope) for w in plan] == [(RECONCILE, "collection.disable")]

    def test_switching_a_row_on_removes_nothing(self):
        assert plan_row_changes(make_change(enabled_before=False, enabled_after=True), never_called) == []


class TestLibraryScope:
    def test_narrowing_the_media_type_removes_only_the_libraries_it_left(self):
        plan = plan_row_changes(make_change(media_after="movie"), lambda: {"2", "3"})

        assert len(plan) == 1
        assert (plan[0].kind, plan[0].scope, plan[0].in_sections) == (RECONCILE, "collection.libraries", ["2", "3"])

    def test_a_widened_row_strands_nothing(self):
        """`stranded_sections` returning empty is the answer for a widening, and for a Plex we could
        not reach — not knowing which libraries exist must mean "delete nothing"."""
        assert plan_row_changes(make_change(media_after="movie"), lambda: set()) == []

    def test_reordering_the_library_list_is_not_a_change(self):
        assert (
            plan_row_changes(make_change(libraries_before=("1", "2"), libraries_after=("2", "1")), never_called) == []
        )


class TestRename:
    def test_a_changed_title_is_reconciled_onto_plex_with_the_old_one_to_find_it_by(self):
        """The old title is the only thing telling this row's collection apart from the person's
        other rows — they all share one label."""
        plan = plan_row_changes(make_change(template_before="Gems", template_after="Hidden Gems"), never_called)

        assert len(plan) == 1
        assert (plan[0].kind, plan[0].new_template, plan[0].old_template) == (RENAME, "Hidden Gems", "Gems")

    def test_saving_the_same_title_does_no_plex_work(self):
        assert plan_row_changes(make_change(template_before="Gems", template_after="Gems"), never_called) == []

    def test_defer_rename_leaves_the_rename_to_the_caller_streaming_it(self):
        """The rename page streams the walk itself; renaming inline as well left that stream
        reporting "renamed 0 collections" for a rename that did happen."""
        change = make_change(template_before="Gems", template_after="Hidden Gems", defer_rename=True)

        assert plan_row_changes(change, never_called) == []


class TestPoster:
    def test_dropping_a_custom_poster_reverts_the_artwork_on_plex(self):
        plan = plan_row_changes(make_change(poster_mode_before="text", poster_mode_after=""), never_called)

        assert [(w.kind, w.scope) for w in plan] == [(POSTER_RESET, "collection.poster")]

    def test_switching_between_custom_posters_reverts_nothing(self):
        """The next delivery pushes the new artwork; reverting to Plex default in between would make
        the row flicker back to its stock poster for a day."""
        assert plan_row_changes(make_change(poster_mode_before="text", poster_mode_after="ai"), never_called) == []

    def test_adding_a_poster_to_a_row_that_had_none_reverts_nothing(self):
        assert plan_row_changes(make_change(poster_mode_before="", poster_mode_after="upload"), never_called) == []


class TestOrdering:
    def test_every_removal_is_planned_before_the_share_filter_pass(self):
        """Load-bearing, not cosmetic. `privacy.sync` drains the queue as it goes, so each account's
        excludes are computed from what is ACTUALLY still on the server — plex-safety rule 1. Planning
        a removal after it would compute the filters against a collection that is about to vanish.
        """
        plan = plan_row_changes(
            make_change(
                build_before="shared",
                build_after="shared",
                enabled_after=False,
                audience_after=frozenset({1}),
                media_after="movie",
                template_before="Gems",
                template_after="Hidden Gems",
                poster_mode_before="text",
                poster_mode_after="",
            ),
            lambda: {"2"},
        )
        kinds = [w.kind for w in plan]

        assert kinds.index(PRIVACY_SYNC) > max(i for i, k in enumerate(kinds) if k == RECONCILE)
        # …and the cosmetic work trails it, so nothing is retitled on a collection being removed.
        assert kinds == [RECONCILE, RECONCILE, PRIVACY_SYNC, RENAME, POSTER_RESET]


class TestShowDaysChange:
    """Changing which days a row appears is a Plex write, not a config change (issue #102).

    Set "weekdays only" on a Saturday and the row has to go NOW. Leaving it to the midnight tick is
    the same bug the `collection.disable` rule was added to fix: you save, nothing visibly happens,
    and it reads as broken.
    """

    def test_changing_the_days_owes_plex_a_visibility_pass(self):
        plan = plan_row_changes(make_change(days_before=(), days_after=(1, 2, 3, 4, 5)), never_called)

        assert [w.kind for w in plan] == [VISIBILITY]

    def test_clearing_the_days_owes_one_too(self):
        """Back to "every day" has to put the row back, not just stop hiding it in future."""
        plan = plan_row_changes(make_change(days_before=(1,), days_after=()), never_called)

        assert [w.kind for w in plan] == [VISIBILITY]

    def test_leaving_the_days_alone_owes_nothing(self):
        """A rename must not fire a server-wide converge."""
        plan = plan_row_changes(make_change(days_before=(1, 3), days_after=(1, 3)), never_called)

        assert [w.kind for w in plan] == []

    def test_a_disabled_row_owes_a_removal_rather_than_a_visibility_pass(self):
        """Switching a row off DELETES its collections, so there is nothing left to show or hide —
        queuing a converge as well would be a pass over rows that no longer exist."""
        plan = plan_row_changes(
            make_change(enabled_before=True, enabled_after=False, days_before=(), days_after=(1,)), never_called
        )

        assert VISIBILITY not in [w.kind for w in plan]
