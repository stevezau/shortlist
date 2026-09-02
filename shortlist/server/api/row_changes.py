"""What one row edit owes Plex, decided from the row's before/after state.

``PATCH /collections/{id}`` used to carry this table itself: eleven mutable flags accumulated down a
200-line handler, then eight conditional dispatches at the bottom, none of them reachable in a test
without a Plex context. The rules are pure — they compare two snapshots of a row and return an ordered
list of follow-up work — so they live here, and the handler becomes validate → apply → plan → enqueue
→ drain.

**Order is load-bearing.** ``privacy.sync`` drains the job queue as a side effect, so the removals
planned BEFORE it are the ones that have actually happened by the time it computes each account's
``label!=`` excludes — excludes are merged from what is really left on the server, and only then is
anything promoted (plex-safety rule 1). Do not reorder without reading that rule.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

#: Queue this row's collections for removal (a `row.reconcile` job). `scope` is the audit scope.
RECONCILE = "reconcile"
#: Recompute every account's share filter, because who may SEE this row changed.
PRIVACY_SYNC = "privacy_sync"
#: Retitle this row's existing collections in place, rather than waiting for a run to rebuild them.
RENAME = "rename"
#: Put the artwork on Plex back to the default, because the row dropped its custom poster.
POSTER_RESET = "poster_reset"
#: Apply this row's day schedule to Plex now, because which days it appears on changed (issue #102).
VISIBILITY = "visibility"


@dataclass(frozen=True)
class RowChange:
    """A row as it was before a PATCH, beside what it is now.

    Every pair is normalised by the caller so that **equal means "the request did not touch this"** —
    a field the PATCH omitted is reported with the same value on both sides. That is what removes the
    handler's flag soup: no rule here needs to know which fields were in the request body.

    Attributes:
        slug: The row's slug (unchanged by a PATCH).
        build_before: ``per_person`` | ``shared``, before.
        build_after: ...and after.
        enabled_before: Whether the row was switched on, before.
        enabled_after: ...and after.
        media_before: ``movie`` | ``show`` | ``both``, before.
        media_after: ...and after.
        libraries_before: Explicit library keys, before ([] = every library of the media type).
        libraries_after: ...and after.
        audience_before: The user ids the row was for, before ("everyone" resolved to the full set).
        audience_after: ...and after.
        template_before: The row's effective title template as the collections on Plex are currently
            titled, or "" when no rename can be owed (a shared row, or a name the PATCH didn't send).
        template_after: ...and after, "" under the same conditions.
        poster_mode_before: The custom-poster mode, before ("" = Plex's own artwork).
        poster_mode_after: ...and after.
        days_before: The ISO weekdays the row appeared on, before (() = every day).
        days_after: ...and after.
        defer_rename: The caller is going to stream the rename itself, so plan no rename here.
    """

    slug: str
    build_before: str
    build_after: str
    enabled_before: bool
    enabled_after: bool
    media_before: str
    media_after: str
    libraries_before: tuple[str, ...]
    libraries_after: tuple[str, ...]
    audience_before: frozenset[int]
    audience_after: frozenset[int]
    template_before: str
    template_after: str
    poster_mode_before: str
    poster_mode_after: str
    days_before: tuple[int, ...] = ()
    days_after: tuple[int, ...] = ()
    defer_rename: bool = False


@dataclass(frozen=True)
class PlannedWork:
    """One unit of follow-up work an edit owes Plex.

    Attributes:
        kind: One of :data:`RECONCILE`, :data:`PRIVACY_SYNC`, :data:`RENAME`, :data:`POSTER_RESET`.
        scope: The audit scope for a reconcile/rename/poster reset, or the plain-English reason a
            privacy sync is owed (it lands in the job's detail line and the Jobs page).
        only_user_ids: RECONCILE — remove only these users' copies, not the whole row.
        in_sections: RECONCILE — remove only what is in these section keys.
        new_template: RENAME — the title to render.
        old_template: RENAME — the title the collections on Plex currently carry, which is the only
            way to tell this row's collection apart from the person's other rows.
    """

    kind: str
    scope: str
    only_user_ids: list[int] | None = None
    in_sections: list[str] | None = None
    new_template: str = ""
    old_template: str = ""


def plan_row_changes(change: RowChange, stranded_sections: Callable[[], set[str]]) -> list[PlannedWork]:
    """The follow-up work this edit owes Plex, in the order it must happen.

    Args:
        change: The row before and after the patch.
        stranded_sections: Returns the section keys the row USED to deliver into and no longer does.
            A callable rather than a value because answering it costs a Plex read, and only a row
            whose media type or library list actually moved needs one.

    Returns:
        Ordered :class:`PlannedWork`, empty when the edit owes Plex nothing.
    """
    plan: list[PlannedWork] = []

    # A build flip (per-person ↔ shared) makes the OLD build's collections stale — a shared collection,
    # or every user's per-person one, under a label the new build won't touch. Remove them so the next
    # run rebuilds the row cleanly under its new build; otherwise both live on Home at once. This is a
    # removal (gate-exempt), and it SUPERSEDES the audience/rename/poster work below, which all act on
    # the old build that is being removed wholesale.
    if change.build_before != change.build_after:
        return [
            PlannedWork(kind=RECONCILE, scope="collection.build"),
            # Flipping per_person→shared makes one PUBLIC row out of what were private per-person
            # ones, and shared→per_person does the reverse. Either way every account's excludes are
            # now wrong.
            PlannedWork(kind=PRIVACY_SYNC, scope=f"row '{change.slug}' changed how it is built"),
        ]

    # Removing a dropped user's row is a removal (gate-exempt); a newly-ADDED user's row is a create,
    # so it's left for the next run's gated delivery. Per-person only: a shared row is ONE collection,
    # so deleting it could not hide it from one person — that is the privacy pass further down.
    dropped = change.audience_before - change.audience_after
    if change.build_before == "per_person" and dropped:
        plan.append(PlannedWork(kind=RECONCILE, scope="collection.audience", only_user_ids=sorted(dropped)))

    # Switching a row OFF must take its collections down, not merely stop refreshing them. Nothing
    # used to fire here: the next run removes the row only for the users it processes, so anyone
    # paused, disabled or restricted kept it indefinitely — and a row with no schedule has no next
    # run at all.
    if change.enabled_before and not change.enabled_after:
        plan.append(PlannedWork(kind=RECONCILE, scope="collection.disable"))

    # A row narrowed to fewer libraries — by media type or by an explicit list — strands whatever it
    # already built in the ones it left. Remove those and only those: the collections in the libraries
    # it still targets are live and must survive.
    if (change.media_before, sorted(change.libraries_before)) != (change.media_after, sorted(change.libraries_after)):
        stranded = stranded_sections()
        if stranded:
            plan.append(PlannedWork(kind=RECONCILE, scope="collection.libraries", in_sections=sorted(stranded)))

    # A shared row is ONE collection, so there is nothing per-person to delete — who SEES it is decided
    # entirely by each account's `label!=` excludes. Without this pass, narrowing the audience changed
    # only the config: the dropped accounts kept the row on their Home until the next nightly run.
    # Widening owes the mirror image, the REMOVAL of an exclude, so both directions qualify.
    if change.build_before == "shared" and change.audience_before != change.audience_after:
        plan.append(PlannedWork(kind=PRIVACY_SYNC, scope=f"the audience for row '{change.slug}' changed"))

    # A rename updates each user's collection title IN PLACE (multi-row users would otherwise keep the
    # old-named copy until the next run rebuilt it). Privacy-neutral, so gate-exempt.
    if change.template_after and change.template_after != change.template_before and not change.defer_rename:
        plan.append(
            PlannedWork(
                kind=RENAME,
                scope="collection.rename",
                new_template=change.template_after,
                old_template=change.template_before,
            )
        )

    # Dropping a custom poster back to Plex default reverts the artwork on Plex now, not just in
    # config. Cosmetic + privacy-neutral, so gate-exempt.
    if change.poster_mode_before and not change.poster_mode_after:
        plan.append(PlannedWork(kind=POSTER_RESET, scope="collection.poster"))

    # Which DAYS the row appears on changed, so Plex is now wrong until midnight — set "weekdays only"
    # on a Saturday and the row has to come down NOW. Skipped for a row being switched OFF: that
    # already deletes its collections above, so there would be nothing left to show or hide.
    #
    # LAST in the plan deliberately. The handler for this merges every account's excludes itself
    # before promoting anything (rule 1), so it must run after the removals above have actually
    # happened — excludes are computed from what is really left on the server.
    if change.enabled_after and tuple(change.days_before) != tuple(change.days_after):
        plan.append(PlannedWork(kind=VISIBILITY, scope=f"the days row '{change.slug}' appears on changed"))

    return plan
