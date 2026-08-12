"""Mirror the live Plex Recommended shelf into agregarr's stored ordering.

Pure logic — no network, no engine context. ``plan_home_order`` takes the shelf as Plex currently
reports it plus the configs agregarr holds for that library, and returns the list agregarr's
``/api/v1/reorder`` endpoint expects, or nothing at all when the two already agree.

Why mirror the whole shelf rather than only Shortlist's rows: agregarr keeps ONE sort key per
library shared by every item in it, and sorts everything by that key. Placing our 46 rows at the top
therefore needs 46 slots below every other item's key — on a real server agregarr's own collections
start well inside that range, so writing only our rows leaves foreign rows interleaved through the
block. Negative keys are not an escape either: agregarr treats anything <= 0 as "unplaced" and
appends it to the END.

So the whole visible shelf is renumbered. That does touch rows Shortlist does not own, which is why
the ordering below is taken FROM Plex rather than invented: the live shelf already reflects
agregarr's own last-applied ordering of its own rows, so their relative order survives the write
untouched. We only make room.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Agregarr places a collection using the identifier Plex reports for its hub, which encodes the
# library: "custom.collection.<libraryId>.<ratingKey>". Built-in hubs ("movie.recentlyadded") carry
# their identifier directly on the config as `hubIdentifier`.
_COLLECTION_IDENTIFIER = "custom.collection.{library_id}.{rating_key}"

# Sorts after every real key, for items agregarr stores as unplaced (`sortOrderHome` 0 or missing).
_UNPLACED = float("inf")


@dataclass(frozen=True)
class MirrorPlan:
    """What (if anything) to send agregarr for one library."""

    library_id: str
    ordered: list[dict[str, Any]] = field(default_factory=list)
    moved: int = 0
    owned_placed: int = 0
    owned_contiguous: bool = True
    unknown_to_agregarr: list[str] = field(default_factory=list)
    #: Configs agregarr holds for this library that carry neither join key, so it cannot be told
    #: where to put them. They keep their stored key and may sort inside our block; non-zero means
    #: the shelf can stay contested however often we write.
    unjoinable: int = 0

    @property
    def changed(self) -> bool:
        """True when agregarr's stored order disagrees with the live shelf and a write is needed."""
        return bool(self.ordered) and self.moved > 0

    def summary(self) -> str:
        """One audit-friendly line."""
        caveat = f"; {self.unjoinable} config(s) agregarr cannot place" if self.unjoinable else ""
        if not self.ordered:
            return f"library {self.library_id}: nothing agregarr manages on this shelf{caveat}"
        if not self.changed:
            return f"library {self.library_id}: already in step ({len(self.ordered)} items, no write needed){caveat}"
        return (
            f"library {self.library_id}: {self.moved} of {len(self.ordered)} items reordered "
            f"({self.owned_placed} Shortlist row(s) placed at the top){caveat}"
        )


def plan_home_order(
    library_id: str,
    live_identifiers: list[str],
    items: list[dict[str, Any]],
    owned_rating_keys: set[str] | None = None,
) -> MirrorPlan:
    """Work out the ordering to store in agregarr so its next sync reproduces the live shelf.

    Args:
        library_id: Plex library section id, e.g. "1".
        live_identifiers: Hub identifiers in the order Plex currently serves them, top first
            (the ``identifier`` attribute of ``/hubs/sections/{id}/manage``).
        items: Configs from ``AgregarrClient.home_items`` for the same library.
        owned_rating_keys: Rating keys of the Shortlist rows, as strings — used only for the
            reporting fields, never to decide placement.

    Returns:
        A ``MirrorPlan``. ``plan.changed`` is False when agregarr already agrees, in which case
        the caller writes nothing — the steady state on a reconciled server.
    """
    owned = {str(k) for k in (owned_rating_keys or set())}
    by_identifier: dict[str, dict[str, Any]] = {}
    for item in items:
        identifier = _identifier_of(item, library_id)
        if identifier:
            by_identifier.setdefault(identifier, item)

    ordered: list[dict[str, Any]] = []
    unknown: list[str] = []
    for identifier in live_identifiers:
        item = by_identifier.pop(identifier, None)
        if item is None:
            unknown.append(identifier)
        else:
            ordered.append(item)

    # Everything agregarr holds for this library that we did NOT just place. Taken as "items minus
    # what we consumed", not from `by_identifier`, because that map only ever held items we could
    # build an identifier for: a config carrying neither join key — or a second config shadowing
    # another's identifier — would otherwise vanish here. Vanishing is not harmless. Such an item
    # keeps its stored key, so it can sort straight into the middle of our block, and because it
    # was invisible to the diff the plan would report "already in step" every run while the shelf
    # stayed contested forever.
    placed = {id(item) for item in ordered}
    remaining = [item for item in items if id(item) not in placed]
    # Unplaced items (key 0) are the one safe omission: agregarr already sorts them last, so
    # rewriting them changes nothing — and giving one a real key would opt a `randomizeHomeOrder`
    # row into shuffling it is currently excluded from.
    ranked = [item for item in remaining if _sort_key(item) != _UNPLACED]
    # A config carrying NEITHER join key cannot be written: agregarr's type guards are bare
    # `'collectionRatingKey' in config` / `'hubIdentifier' in config` checks, and it silently skips
    # anything satisfying neither while still answering 200. Sending it anyway would mean a write
    # every single run that never converges, so it is counted and surfaced instead. It still holds
    # its stored key and can therefore sort inside our block — which is precisely why the count has
    # to reach the audit rather than being swallowed here.
    leftovers = [item for item in ranked if _identifier_of(item, library_id)]
    unjoinable = len(ranked) - len(leftovers)
    ordered.extend(sorted(leftovers, key=_sort_key))

    owned_positions = [i for i, item in enumerate(ordered) if str(item.get("collectionRatingKey") or "") in owned]
    return MirrorPlan(
        library_id=str(library_id),
        ordered=ordered,
        moved=_rank_changes(ordered, items),
        owned_placed=len(owned_positions),
        owned_contiguous=_is_contiguous(owned_positions),
        unknown_to_agregarr=unknown,
        unjoinable=unjoinable,
    )


def _identifier_of(item: dict[str, Any], library_id: str) -> str | None:
    """The Plex hub identifier this config controls, or None if it carries neither join key."""
    rating_key = item.get("collectionRatingKey")
    if rating_key:
        return _COLLECTION_IDENTIFIER.format(library_id=library_id, rating_key=rating_key)
    hub_identifier = item.get("hubIdentifier")
    return str(hub_identifier) if hub_identifier else None


def _sort_key(item: dict[str, Any]) -> float:
    """An item's current rank key. 0/missing means unplaced, which agregarr sorts last.

    JSON numbers are not guaranteed to arrive as Python ints — a float or a numeric string reads as
    "unplaced" under a bare ``isinstance(value, int)``, which would quietly drop the item from the
    leftovers and let it sort into the middle of our block.
    """
    value = item.get("sortOrderHome")
    if isinstance(value, bool) or value is None:
        return _UNPLACED
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _UNPLACED
    return number if number > 0 else _UNPLACED


def _rank_changes(ordered: list[dict[str, Any]], items: list[dict[str, Any]]) -> int:
    """How many items the write would actually move.

    Compares the desired sequence against agregarr's CURRENT effective order — what its own sync
    would produce today (sort by key, unplaced last, ties by stored order). Zero means the write can
    be skipped entirely, which is the point: on a reconciled server every later run costs one read.

    An item we are placing that agregarr currently holds as UNPLACED always counts as a move, even
    when it happens to fall where we want it. Its position then rests on a tie-break between equal
    keys — the order agregarr's own GET happened to return — rather than on anything stored, and
    "it is already right by luck" is not a reason to leave a shelf's order unpinned.
    """
    if not ordered:
        return 0
    unpinned = sum(1 for item in ordered if _sort_key(item) == _UNPLACED)
    desired = [id(item) for item in ordered]
    placed = {id(item) for item in ordered}
    current = [id(item) for item in sorted((i for i in items if id(i) in placed), key=_sort_key)]
    mismatches = sum(1 for a, b in zip(desired, current, strict=False) if a != b)
    return mismatches + abs(len(desired) - len(current)) + unpinned


def _is_contiguous(positions: list[int]) -> bool:
    """True when the given ranks form one unbroken block (our rows are not interleaved)."""
    return not positions or positions == list(range(positions[0], positions[-1] + 1))
