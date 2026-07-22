"""Share-filter parse/merge/serialize and restriction sync — the load-bearing wall.

Every function here obeys plex-safety rule 3: writes are read-modify-write MERGES that leave
every condition Shortlist didn't add byte-identical. Values are kept raw (never URL-decoded) so
``serialize_filter(parse_filter(s)) == s`` holds for any filter Plex hands us.

Live-validated against plex.tv on 2026-07-12 (Phase 0): `PUT /api/users/{id}` persists
`filterMovies`/`filterTelevision` verbatim with no server-side normalization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from shortlist.engine.models import FilterSnapshot, UserProfile, UserType

if TYPE_CHECKING:
    from shortlist.engine.clients.plextv import PlexTvClient, PlexTvUser

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


def shortlist_labels_in(raw: str, label_prefix: str) -> set[str]:
    """Return the shortlist-owned labels currently excluded in a filter string."""
    prefix = f"{label_prefix}_".lower()
    found = set()
    for cond in parse_filter(raw):
        if cond.field == "label" and cond.op == "!=":
            found.update(v for v in cond.values if v.lower().startswith(prefix))
    return found


class SnapshotStore(Protocol):
    """Persistence for pre-mutation snapshots; the server (and tests) provide implementations."""

    def get(self, plex_account_id: int) -> FilterSnapshot | None: ...

    def save(self, snapshot: FilterSnapshot) -> None: ...


_UNSHARED = object()  # sentinel: a label the CONFIG does not declare as a shared row


def shared_label_audiences(config) -> dict[str, set[int] | None]:
    """Lowercased label -> audience account ids (None = public) for every CONFIGURED shared row.

    The one definition of "what is a shared row, and who is allowed to see it" — used by the writer
    to decide which `label!=` excludes each account's share needs (a shared row is public, so its own
    label is NOT excluded on anyone).
    """
    return {spec.label.lower(): spec.audience for spec in config.shared_rows() if spec.label}


def desired_excludes(
    own_label: str | None,
    stored_labels: dict[str, str],
    *,
    account_id: int | None = None,
    shared_labels: dict[str, set[int] | None] | None = None,
    hide_all_shared: bool = False,
) -> set[str]:
    """Labels an account must NOT see: every EXISTING Shortlist row's label except their own.

    Derived from the rows that exist on the server — NOT from the list of users Shortlist manages.
    A row is visible to everyone whose share filter doesn't exclude it, and Plex does not care
    whether we consider its owner "enabled", "paused", or in tonight's run. Keying this off the
    user list is how 45 of a live server's 48 accounts ended up able to see three other people's
    private rows: only the three managed users ever had excludes written (SFLIX, 2026-07-12).

    `own_label` is resolved by the caller from the account's ID — never from its NAME. Two Plex
    accounts can have display names that slugify identically, and anyone can rename themselves at
    any time; deciding "this row is mine" from a name would hand one of them somebody else's row
    and hide the other's own row from them. `None` means the account owns no row — the right
    answer for every account Shortlist has never built one for, and they are excluded from all of it.

    Only labels that exist on real collections are excluded (`stored_labels` is built from the
    PMS, so casing matches what Plex stored — Phase 0 finding). A user without a collection yet
    has nothing to leak, and guessing their label's casing would poison filters with case-variants.

    Shared "popular on this server" rows are classified by CONFIG, never by the label string: only a
    label the caller declares in `shared_labels` (lowercased label -> the audience account ids, or
    None for public) is treated as shared. A public shared row is excluded from nobody; a subset one
    is excluded from every account not in its audience. Anything NOT in `shared_labels` — a private
    row (even one whose owner's slug happens to look shared), or a stale/disabled shared collection
    still on the server — is excluded, fail-safe: a leak we never write beats a leak we can't unwrite.

    `hide_all_shared` is set for a DISABLED (opted-out) Shortlist account: it hides EVERY shared row
    from them, including public ones — a disabled user should see nothing Shortlist produces, not even
    the "Popular on this server" rows everyone else gets.
    """
    shared_labels = shared_labels or {}
    excludes: set[str] = set()
    for label in stored_labels.values():
        if label == own_label:
            continue
        audience = shared_labels.get(label.lower(), _UNSHARED)
        if audience is not _UNSHARED and not hide_all_shared:  # a CONFIGURED shared row, account opted in
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
    label_prefix: str = "shortlist",
    shared_labels: dict[str, set[int] | None] | None = None,
    hide_all_shared: bool = False,
    dry_run: bool = False,
) -> dict[str, tuple[str, str]] | None:
    """Merge the desired shortlist excludes into one user's share filters.

    `remote` is this user's CURRENT plex.tv record, passed in rather than fetched: the caller
    already holds the whole roster, and re-fetching it per user would mean a full `GET /api/users`
    for every account on the server (~96 of them on a 48-user server) every night.

    Steady state (already correct) makes ZERO writes. Returns the {field: (before, after)} diff of
    what was written — or would be, in dry-run — and None when nothing needed changing. The diff
    is the audit record: changing someone's Plex share permissions is the most sensitive write
    Shortlist makes (rule 10).

    The owner is never restricted (Plex limitation — skipped, not an error).
    """
    if user.user_type is UserType.OWNER:
        logger.debug("{}: owner is never restricted — skipping", user.username)
        return None
    if remote is None:
        # Shortlist knows this user but Plex no longer shares the server with them: there is no
        # share, so there is no filter to write. Skipping is right — erroring here would let one
        # stale user row stop every other user's rows from being promoted, every night.
        logger.info("{}: no longer shares this server — nothing to restrict", user.username)
        return None

    wanted = desired_excludes(
        own_label,
        stored_labels,
        account_id=user.plex_account_id,
        shared_labels=shared_labels,
        hide_all_shared=hide_all_shared,
    )
    # Converge SHARED-row excludes: drop any shortlist SHARED-row exclude the account should no longer
    # have (re-enabled after a disable, or added to a subset row's audience). This is the ONE safe
    # place to remove an exclude — un-hiding a *shared* row only ever reveals a public or in-audience
    # row, never a private one, so it can't leak even if `wanted` is computed from a partial read.
    # Private-row excludes are NEVER pruned (removing one is the leak direction), so they stay
    # union-only and fail-safe. Foreign filters are untouched (both primitives byte-preserve them).
    shared_lower = set(shared_labels or {})
    wanted_lower = {w.lower() for w in wanted}
    prunable_shared: set[str] = set()
    for fieldname in RESTRICTED_FILTER_FIELDS:
        for lbl in shortlist_labels_in(remote.filters[fieldname], label_prefix):
            if lbl.lower() in shared_lower and lbl.lower() not in wanted_lower:
                prunable_shared.add(lbl)

    desired_fields = {}
    for fieldname in RESTRICTED_FILTER_FIELDS:
        current = remote.filters[fieldname]
        merged = merge_label_excludes(current, wanted)
        if prunable_shared:
            merged = remove_label_excludes(merged, prunable_shared)
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
            logger.info("[dry-run] {}: would snapshot filters before the first write", user.username)
        else:
            snapshots.save(snapshot)
            logger.info("{}: snapshot persisted before first restriction write", user.username)

    diff = {k: (remote.filters[k], v) for k, v in desired_fields.items()}
    if dry_run:
        logger.info("[dry-run] {}: would merge filters — {}", user.username, summarise_filter_diff(diff, label_prefix))
        return diff

    plextv.update_user_filters(user.plex_account_id, desired_fields)
    # Verification is NOT done per-user here: each read-back was a full GET /api/users, so on a night
    # that writes A accounts it cost A full-roster fetches (~O(A²)). The caller instead reads the roster
    # ONCE after all writes and verifies every written account's shortlist excludes persisted, still
    # strictly before any promotion — see the batched read-back at the end of _privacy_sync_phase in
    # pipeline.py (plex-safety rule 1).
    logger.info("{}: filters merged — {}", user.username, summarise_filter_diff(diff, label_prefix))
    return diff


def summarise_filter_diff(diff: dict[str, tuple[str, str]], label_prefix: str) -> str:
    """A one-line description of what a filter write actually CHANGED.

    The full before/after belongs in the audit event (rule 10), not in the log: on a 48-user server
    each account's filter string carries every other account's exclude, so logging both sides put
    ~8 KB per user per field into the file. Forty-eight of those buried everything else in the run,
    which is the opposite of what the log is for — and it is the same 47 labels every time, so the
    only information in it is the one that changed.
    """
    parts = []
    for fieldname, (before, after) in sorted(diff.items()):
        was = shortlist_labels_in(before, label_prefix)
        now = shortlist_labels_in(after, label_prefix)
        added, removed = sorted(now - was), sorted(was - now)
        if not added and not removed:
            # A change outside our own excludes (pruning a shared row's label leaves the set equal).
            parts.append(f"{fieldname} rewritten")
            continue
        bits = []
        for sign, labels in (("+", added), ("-", removed)):
            if not labels:
                continue
            shown = ", ".join(labels[:3])
            more = f" +{len(labels) - 3} more" if len(labels) > 3 else ""
            bits.append(f"{sign}{len(labels)} ({shown}{more})")
        parts.append(f"{fieldname} {' '.join(bits)}")
    return "; ".join(parts) or "no change"


def restore_user_restrictions(
    plextv: PlexTvClient,
    snapshot: FilterSnapshot,
    *,
    dry_run: bool = False,
) -> bool:
    """Restore a user's filters byte-identical from their pre-Shortlist snapshot (uninstall path)."""
    remote = plextv.get_user(snapshot.plex_account_id)
    changed = {
        k: snapshot.filters[k] for k in FILTER_FIELDS if remote.filters.get(k, "") != snapshot.filters.get(k, "")
    }
    if not changed:
        return False
    if dry_run:
        # Field names only: a restore payload is the user's ENTIRE original filter string per field.
        logger.info("[dry-run] {}: would restore {} from the snapshot", snapshot.username, ", ".join(sorted(changed)))
        return True
    plextv.update_user_filters(snapshot.plex_account_id, changed)
    readback = plextv.get_user(snapshot.plex_account_id)
    for fieldname, expected in changed.items():
        if readback.filters.get(fieldname, "") != expected:
            raise RuntimeError(f"{snapshot.username}: restore mismatch on {fieldname}")
    logger.info("{}: filters restored from snapshot", snapshot.username)
    return True
