"""Share-filter parse/merge/serialize and restriction sync — the load-bearing wall.

Every function here obeys plex-safety rule 3: writes are read-modify-write MERGES that leave
every condition Rowarr didn't add byte-identical. Values are kept raw (never URL-decoded) so
``serialize_filter(parse_filter(s)) == s`` holds for any filter Plex hands us.

Live-validated against plex.tv on 2026-07-12 (Phase 0): `PUT /api/users/{id}` persists
`filterMovies`/`filterTelevision` verbatim with no server-side normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from rowarr.engine.models import FilterSnapshot, UserProfile, UserType

if TYPE_CHECKING:
    from rowarr.engine.clients.plex import PlexTvClient

FILTER_FIELDS = ("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos")
RESTRICTED_FILTER_FIELDS = ("filterMovies", "filterTelevision")


class FilterParseError(ValueError):
    """A share filter didn't parse; refuse to touch it rather than risk clobbering it."""


@dataclass(frozen=True)
class FilterCondition:
    field: str
    op: str  # "=" or "!="
    values: tuple[str, ...]


def parse_filter(raw: str) -> list[FilterCondition]:
    """Parse ``'label!=a,b|contentRating=PG'`` into ordered conditions.

    Args:
        raw: The pipe-separated filter string from plex.tv (may be empty).

    Returns:
        Ordered conditions; values are raw strings, never URL-decoded.

    Raises:
        FilterParseError: If any condition has no operator — we must not rewrite
            a filter we cannot fully represent.
    """
    if not raw:
        return []
    conditions = []
    for part in raw.split("|"):
        for op in ("!=", "="):
            head, sep, tail = part.partition(op)
            if sep:
                conditions.append(FilterCondition(head, op, tuple(tail.split(",")) if tail else ()))
                break
        else:
            raise FilterParseError(f"unparseable condition {part!r} in filter {raw!r}")
    return conditions


def serialize_filter(conditions: list[FilterCondition]) -> str:
    return "|".join(f"{c.field}{c.op}{','.join(c.values)}" for c in conditions)


def merge_label_excludes(raw: str, labels: set[str]) -> str:
    """Union `labels` into the first ``label!=`` condition, byte-preserving everything else.

    Membership is case-insensitive (Plex tag matching is), so a case-variant of an already
    excluded label is never appended as a duplicate.
    """
    conditions = parse_filter(raw)
    for i, cond in enumerate(conditions):
        if cond.field == "label" and cond.op == "!=":
            present = {v.lower() for v in cond.values}
            missing = [v for v in sorted(labels) if v.lower() not in present]
            if not missing:
                return raw
            conditions[i] = FilterCondition(cond.field, cond.op, cond.values + tuple(missing))
            return serialize_filter(conditions)
    if labels:
        conditions.append(FilterCondition("label", "!=", tuple(sorted(labels))))
    return serialize_filter(conditions)


def remove_label_excludes(raw: str, labels: set[str]) -> str:
    """Remove exactly `labels` from ``label!=`` conditions; drop the condition if it empties."""
    conditions = parse_filter(raw)
    out = []
    for cond in conditions:
        if cond.field == "label" and cond.op == "!=":
            targets = {label.lower() for label in labels}
            values = tuple(v for v in cond.values if v.lower() not in targets)
            if not values:
                continue
            cond = FilterCondition(cond.field, cond.op, values)
        out.append(cond)
    return serialize_filter(out)


def rowarr_labels_in(raw: str, label_prefix: str) -> set[str]:
    """Return the rowarr-owned labels currently excluded in a filter string."""
    prefix = f"{label_prefix}_".lower()
    found = set()
    for cond in parse_filter(raw):
        if cond.field == "label" and cond.op == "!=":
            found.update(v for v in cond.values if v.lower().startswith(prefix))
    return found


class SnapshotStore(Protocol):
    """Persistence for pre-mutation snapshots; the CLI and server provide implementations."""

    def get(self, plex_account_id: int) -> FilterSnapshot | None: ...

    def save(self, snapshot: FilterSnapshot) -> None: ...


def desired_excludes(user: UserProfile, enabled_users: list[UserProfile], stored_labels: dict[str, str]) -> set[str]:
    """Labels user U must NOT see: every other enabled user's EXISTING collection label.

    Only labels that exist on real collections are excluded (`stored_labels` is built from
    the PMS, so casing matches what Plex stored — Phase 0 finding). A user without a
    collection yet has nothing to leak, and guessing their label's casing would poison
    filters with case-variants.
    """
    return {
        stored_labels[other.slug]
        for other in enabled_users
        if other.plex_account_id != user.plex_account_id and other.slug in stored_labels
    }


def sync_user_restrictions(
    plextv: PlexTvClient,
    user: UserProfile,
    enabled_users: list[UserProfile],
    stored_labels: dict[str, str],
    snapshots: SnapshotStore,
    *,
    label_prefix: str = "rowarr",
    dry_run: bool = False,
) -> bool:
    """Merge the desired rowarr excludes into one user's share filters.

    Steady state (already correct) makes one plex.tv read and ZERO writes.
    Returns True if a write happened (or would have, in dry-run).

    The owner is never restricted (Plex limitation — skipped, not an error).
    """
    if user.user_type is UserType.OWNER:
        logger.debug("{}: owner is never restricted — skipping", user.username)
        return False

    remote = plextv.get_user(user.plex_account_id)
    wanted = desired_excludes(user, enabled_users, stored_labels)
    desired_fields = {}
    for fieldname in RESTRICTED_FILTER_FIELDS:
        current = remote.filters[fieldname]
        merged = merge_label_excludes(current, wanted)
        if merged != current:
            desired_fields[fieldname] = merged

    if not desired_fields:
        return False

    if snapshots.get(user.plex_account_id) is None:
        snapshot = FilterSnapshot(
            plex_account_id=user.plex_account_id,
            username=user.username,
            taken_at=datetime.now(UTC),
            filters=dict(remote.filters),
        )
        if dry_run:
            logger.info("[dry-run] {}: would snapshot filters {}", user.username, snapshot.filters)
        else:
            snapshots.save(snapshot)
            logger.info("{}: snapshot persisted before first restriction write", user.username)

    diff = {k: (remote.filters[k], v) for k, v in desired_fields.items()}
    if dry_run:
        logger.info("[dry-run] {}: would merge filters {}", user.username, diff)
        return True

    plextv.update_user_filters(user.plex_account_id, desired_fields)
    readback = plextv.get_user(user.plex_account_id)
    for fieldname, expected in desired_fields.items():
        got = readback.filters[fieldname]
        missing = rowarr_labels_in(expected, label_prefix) - rowarr_labels_in(got, label_prefix)
        if missing:
            raise RuntimeError(f"{user.username}: read-back missing excludes {missing} on {fieldname}")
        if got != expected:
            logger.warning("{}: {} persisted but normalized: {!r} -> {!r}", user.username, fieldname, expected, got)
    logger.info("{}: filters merged {}", user.username, diff)
    return True


def restore_user_restrictions(
    plextv: PlexTvClient,
    snapshot: FilterSnapshot,
    *,
    dry_run: bool = False,
) -> bool:
    """Restore a user's filters byte-identical from their pre-Rowarr snapshot (uninstall path)."""
    remote = plextv.get_user(snapshot.plex_account_id)
    changed = {
        k: snapshot.filters[k] for k in FILTER_FIELDS if remote.filters.get(k, "") != snapshot.filters.get(k, "")
    }
    if not changed:
        return False
    if dry_run:
        logger.info("[dry-run] {}: would restore filters {}", snapshot.username, changed)
        return True
    plextv.update_user_filters(snapshot.plex_account_id, changed)
    readback = plextv.get_user(snapshot.plex_account_id)
    for fieldname, expected in changed.items():
        if readback.filters.get(fieldname, "") != expected:
            raise RuntimeError(f"{snapshot.username}: restore mismatch on {fieldname}")
    logger.info("{}: filters restored from snapshot", snapshot.username)
    return True
