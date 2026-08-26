"""Make one Plex account's watch state match another's — the plan, not the writing.

Pure: state in, an ordered list of writes out. The caller applies them (and can dry-run them, count
them, or show them to someone) without this module knowing what a PMS is.

**Why this exists.** The transfer used to scrobble a SHOW's rating key, which marks every episode
watched. Someone 400 episodes into One Piece arrived on their new account with all 1,100 finished.
On the maintainer's own account 342 of 535 watched shows are partial, so that was the common case.
The cache it read from could not have done better: `watched_titles` is built from `?unwatched=0`,
which is show-level and completions-only — it knows how MANY episodes were watched, never which.

Three rules, each measured against a real server (see
`.claude/docs/watching-account-transfer-design.md`):

* **Only leaves are ever written.** Never a show key, never a season key. A show-key scrobble does
  mark all the episodes, but leaves the SHOW's own `viewCount` unset — and `?type=2&unwatched=0`
  filters on exactly that, so the show goes missing from the read Shortlist's own watch cache is
  built from. Scrobbling the episodes instead reproduces the source exactly and cost 0.46s for 47.
* **It mirrors, it does not merge.** State the source lacks is REMOVED. Add-only cannot repair an
  account the old transfer already spoiled: its 1,098 spurious episodes are already marked, so
  re-running changes nothing and the result is not a replica.
* **Additions are written oldest-first.** Plex stamps every write `now` and accepts no date, so
  absolute dates are lost whatever we do. Writing in the source's order makes the target's
  Continue Watching sort the way the source's does, which is the shelf people actually look at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

#: Plex echoes an offset back rounded, so an exact comparison would rewrite thousands of positions on
#: every run and never settle. A second is far below anything a viewer could notice and far above the
#: rounding.
OFFSET_TOLERANCE_MS = 1_000


class OpKind(StrEnum):
    """What one write does. Named for the effect, not the Plex endpoint, because two of them share
    an endpoint and one endpoint serves two effects."""

    MARK = "mark"
    UNMARK = "unmark"
    SET_OFFSET = "set_offset"
    #: Rewind to nothing. Sends `/:/unscrobble`, the only call that clears an offset — see `_plan_one`.
    CLEAR_OFFSET = "clear_offset"


@dataclass(frozen=True)
class ItemState:
    """One movie or one EPISODE, as an account sees it. Never a show and never a season.

    `view_count` is Plex's own play count for this account, so a rewatch is 2 or more. `last_viewed_at`
    is a unix timestamp, or 0 when Plex reported none — 0 is a real value here and sorts first, see
    `build_plan`.
    """

    rating_key: int
    media_type: str
    view_count: int = 0
    view_offset_ms: int = 0
    last_viewed_at: int = 0
    show_rating_key: int | None = None
    title: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.view_count and not self.view_offset_ms


@dataclass(frozen=True)
class WatchState:
    """Everything one account has watched or started, keyed by rating key.

    Holds only leaves. A show's totals are derived by whoever needs them — deliberately, because a
    show row is state Plex computes and can disagree with its own episodes (a show-key scrobble
    leaves it reading 47/47 while the section read cannot see it at all).
    """

    items: dict[int, ItemState] = field(default_factory=dict)
    #: Library keys this read could NOT see — a 403, i.e. not shared with that token.
    #:
    #: Load-bearing, because `build_plan` treats the source as authoritative and REMOVES whatever it
    #: does not contain. A source read that quietly dropped a library would therefore un-mark every
    #: title that library holds on the target — 10,995 episodes on a real account — and the verify
    #: pass would report a clean run, because the target really would match the truncated source.
    #:
    #: `watched_titles` has carried the same guard for the same reason since a partial cache was
    #: served as a complete one (`WatchedRead.covers_window`). This is that guard for the leaf read.
    unreadable: tuple[str, ...] = ()

    @property
    def is_complete(self) -> bool:
        return not self.unreadable


@dataclass(frozen=True)
class WriteOp:
    """One write, carrying the DESIRED end state rather than a delta.

    Desired-state rather than "scrobble twice" is what makes a resumed or retried run safe: applying
    the same op to an account that already took it is a no-op, where replaying a delta would push the
    count past the source's.
    """

    kind: OpKind
    rating_key: int
    media_type: str
    #: For MARK: the total `viewCount` the target should END UP with. Reporting and verification read
    #: this; it is NOT how many calls to make.
    view_count: int = 0
    #: For MARK: how many scrobbles that actually takes, given what the target already has. A scrobble
    #: only ever adds one, so topping 1 up to 3 is TWO calls — sending three would land on four. The
    #: plan is always rebuilt from a fresh read, so this delta is never applied to a state it was not
    #: computed against.
    scrobbles: int = 0
    offset_ms: int = 0
    #: The SOURCE's `lastViewedAt`, used only to order the run. Not written anywhere — Plex takes no
    #: date.
    sort_key: int = 0
    title: str = ""
    show_rating_key: int | None = None


def build_plan(source: WatchState, target: WatchState) -> list[WriteOp]:
    """The writes that turn `target`'s watch state into `source`'s.

    Args:
        source: The account being copied FROM — normally the owner's.
        target: The account being copied ONTO, as it is right now. Pass an empty state only when the
            target is genuinely empty; passing empty for a populated account turns a mirror into a
            merge and silently leaves its existing watches in place.

    Returns:
        Removals first, then additions oldest-first. Empty when the two already agree, which is what
        makes a second run cost nothing and a re-run after a crash safe.
    """
    removals: list[WriteOp] = []
    additions: list[WriteOp] = []

    for key in sorted(set(source.items) | set(target.items)):
        remove, add = _plan_one(key, source.items.get(key), target.items.get(key))
        removals.extend(remove)
        additions.extend(add)

    # Stable, so a MARK stays ahead of the SET_OFFSET for the same title when both share a timestamp.
    additions.sort(key=lambda o: o.sort_key)
    return removals + additions


def _plan_one(key: int, want: ItemState | None, have: ItemState | None) -> tuple[list[WriteOp], list[WriteOp]]:
    """The removals and additions for a single rating key. At least one side is always present."""
    # Neither side is authoritative about type on its own: a key present only on the TARGET still
    # needs one to be un-marked, and only the target can describe it.
    spec = want or have
    if spec is None:  # unreachable; keeps the type checker and the next reader honest
        return [], []

    def op(kind: OpKind, **kw) -> WriteOp:
        return WriteOp(
            kind=kind,
            rating_key=key,
            media_type=spec.media_type,
            sort_key=want.last_viewed_at if want else 0,
            # Whichever side actually has a name. `want` is authoritative for everything else, but on
            # the UNDO path it is rebuilt from a snapshot, which stores no titles — so every removal
            # for a key the snapshot also holds fell through to "ratingKey 12345" in the preview.
            # That is the listing a person is asked to approve before watches are deleted, on the more
            # destructive of the two mirrors: the one that removes what was watched AFTER the copy.
            # A removal always has a live-read `have`, so a name is always available.
            title=(have.title if have and have.title else spec.title),
            show_rating_key=spec.show_rating_key,
            **kw,
        )

    want_count = want.view_count if want else 0
    have_count = have.view_count if have else 0
    want_offset = want.view_offset_ms if want else 0
    have_offset = have.view_offset_ms if have else 0

    removals: list[WriteOp] = []
    additions: list[WriteOp] = []

    # Clearing an offset is a FULL RESET of the item, not a surgical edit. `/:/progress?time=0` was
    # the obvious way to do it and it silently does nothing — live-probed against a real server, an
    # offset of 1,139,347 was still 1,139,347 afterwards. The only call that clears one is
    # `/:/unscrobble`, which zeroes the view count with it. So any count we still want has to be
    # rebuilt from zero afterwards, exactly as it is for an over-count.
    #
    # This cost a real 293-item residue on a live undo before it was understood, and the fake had
    # modelled `time=0` as working — a fake easier than the server, which is the one thing a fake may
    # never be.
    clearing = bool(have_offset) and not want_offset
    resetting = have_count > want_count or clearing
    marking = want_count > have_count or (resetting and want_count)
    # What the target will have AFTER the writes above — not what it has now. BOTH of them destroy an
    # existing offset, and both were measured saying so:
    #
    #   * the reset is `/:/unscrobble`, which zeroes count and offset together;
    #   * a plain `/:/scrobble` ALSO clears an offset the item already carries — probed live, an
    #     offset of 480,000 read back as 0 after one scrobble.
    #
    # Comparing `want_offset` against the pre-write reading therefore skipped the reposition whenever
    # the two already agreed, and the position was silently lost. The undo path hits the reset case
    # routinely; the scrobble case bites any title that is both watched and part-way through.
    effective_have_offset = 0 if (resetting or marking) else have_offset
    if resetting:
        # Reported as an un-mark when a watch is genuinely being removed, and as a rewind when only
        # the position is. Both send the same call; they are different sentences to a person.
        removals.append(op(OpKind.UNMARK if have_count > want_count else OpKind.CLEAR_OFFSET))
        # The reset zeroes the count, so the rebuild starts from nothing and needs the full total.
        if want_count:
            additions.append(op(OpKind.MARK, view_count=want_count, scrobbles=want_count))
    elif want_count > have_count:
        additions.append(op(OpKind.MARK, view_count=want_count, scrobbles=want_count - have_count))

    if want_offset and abs(want_offset - effective_have_offset) > OFFSET_TOLERANCE_MS:
        # After the MARK, never before: probed on a film both watched and 8 minutes in,
        # scrobble-then-progress reproduced both. The other order loses the position.
        additions.append(op(OpKind.SET_OFFSET, offset_ms=want_offset))

    return removals, additions


def summarise(plan: list[WriteOp]) -> dict[str, int]:
    """Counts per kind, for the audit row and the dry-run summary (rule 10).

    Reported per kind rather than as one total because "wrote 7,000 things" and "removed 412 watches
    from someone's account" are not the same sentence, and only the second needs consent.
    """
    out = {kind.value: 0 for kind in OpKind}
    for op in plan:
        out[op.kind.value] += 1
    return out


def removals_by_title(plan: list[WriteOp], limit: int = 0) -> list[str]:
    """Titles this plan would un-mark or rewind, for the confirmation screen.

    By title, not by count: "this removes 412 watches" is not something anyone can check, and this is
    the only destructive path in the feature.
    """
    names = [op.title or f"ratingKey {op.rating_key}" for op in plan if op.kind in (OpKind.UNMARK, OpKind.CLEAR_OFFSET)]
    return names[:limit] if limit else names
