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
    from rowarr.engine.clients.plextv import PlexTvClient, PlexTvUser

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


_UNSHARED = object()  # sentinel: a label the CONFIG does not declare as a shared row


def desired_excludes(
    own_label: str | None,
    stored_labels: dict[str, str],
    *,
    account_id: int | None = None,
    shared_labels: dict[str, set[int] | None] | None = None,
) -> set[str]:
    """Labels an account must NOT see: every EXISTING Rowarr row's label except their own.

    Derived from the rows that exist on the server — NOT from the list of users Rowarr manages.
    A row is visible to everyone whose share filter doesn't exclude it, and Plex does not care
    whether we consider its owner "enabled", "paused", or in tonight's run. Keying this off the
    user list is how 45 of a live server's 48 accounts ended up able to see three other people's
    private rows: only the three managed users ever had excludes written (SFLIX, 2026-07-12).

    `own_label` is resolved by the caller from the account's ID — never from its NAME. Two Plex
    accounts can have display names that slugify identically, and anyone can rename themselves at
    any time; deciding "this row is mine" from a name would hand one of them somebody else's row
    and hide the other's own row from them. `None` means the account owns no row — the right
    answer for every account Rowarr has never built one for, and they are excluded from all of it.

    Only labels that exist on real collections are excluded (`stored_labels` is built from the
    PMS, so casing matches what Plex stored — Phase 0 finding). A user without a collection yet
    has nothing to leak, and guessing their label's casing would poison filters with case-variants.

    Shared "popular on this server" rows are classified by CONFIG, never by the label string: only a
    label the caller declares in `shared_labels` (lowercased label -> the audience account ids, or
    None for public) is treated as shared. A public shared row is excluded from nobody; a subset one
    is excluded from every account not in its audience. Anything NOT in `shared_labels` — a private
    row (even one whose owner's slug happens to look shared), or a stale/disabled shared collection
    still on the server — is excluded, fail-safe: a leak we never write beats a leak we can't unwrite.
    """
    shared_labels = shared_labels or {}
    excludes: set[str] = set()
    for label in stored_labels.values():
        if label == own_label:
            continue
        audience = shared_labels.get(label.lower(), _UNSHARED)
        if audience is not _UNSHARED:  # a CONFIGURED shared row
            if audience is None:  # public -> everyone may see it -> never excluded
                continue
            if account_id is not None and account_id in audience:  # in the audience -> may see it
                continue
            # restricted, and this account isn't in the audience -> hide it, like a private row
        excludes.add(label)
    return excludes


def sync_user_restrictions(
    plextv: PlexTvClient,
    user: UserProfile,
    remote: PlexTvUser | None,
    stored_labels: dict[str, str],
    snapshots: SnapshotStore,
    *,
    own_label: str | None = None,
    label_prefix: str = "rowarr",
    shared_labels: dict[str, set[int] | None] | None = None,
    dry_run: bool = False,
) -> dict[str, tuple[str, str]] | None:
    """Merge the desired rowarr excludes into one user's share filters.

    `remote` is this user's CURRENT plex.tv record, passed in rather than fetched: the caller
    already holds the whole roster, and re-fetching it per user would mean a full `GET /api/users`
    for every account on the server (~96 of them on a 48-user server) every night.

    Steady state (already correct) makes ZERO writes. Returns the {field: (before, after)} diff of
    what was written — or would be, in dry-run — and None when nothing needed changing. The diff
    is the audit record: changing someone's Plex share permissions is the most sensitive write
    Rowarr makes (rule 10).

    The owner is never restricted (Plex limitation — skipped, not an error).
    """
    if user.user_type is UserType.OWNER:
        logger.debug("{}: owner is never restricted — skipping", user.username)
        return None
    if remote is None:
        # Rowarr knows this user but Plex no longer shares the server with them: there is no
        # share, so there is no filter to write. Skipping is right — erroring here would let one
        # stale user row stop every other user's rows from being promoted, every night.
        logger.info("{}: no longer shares this server — nothing to restrict", user.username)
        return None

    wanted = desired_excludes(own_label, stored_labels, account_id=user.plex_account_id, shared_labels=shared_labels)
    desired_fields = {}
    for fieldname in RESTRICTED_FILTER_FIELDS:
        current = remote.filters[fieldname]
        merged = merge_label_excludes(current, wanted)
        if merged != current:
            desired_fields[fieldname] = merged

    if not desired_fields:
        return None

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
        return diff

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
    return diff


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
