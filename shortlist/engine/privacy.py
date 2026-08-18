"""Share-filter parse/merge/serialize and restriction sync — the load-bearing wall.

Every function here obeys plex-safety rule 3: writes are read-modify-write MERGES that leave
every condition Shortlist didn't add byte-identical. Values are kept raw (never URL-decoded) and the
separators are kept as they were read, so ``serialize_filter(parse_filter(s)) == s`` holds for any
filter Plex hands us. Values are COMPARED url-decoded and case-folded, because the same label reaches
us written several ways.

Live-validated against plex.tv on 2026-07-12 (Phase 0): `PUT /api/users/{id}` persists
`filterMovies`/`filterTelevision` verbatim with no server-side normalization.

Re-validated 2026-08-10 (`tests/fixtures/plextv_combined_filters.json`), and this is the part that
matters: "verbatim" extends to the SEPARATORS. `|` and `&` between conditions, and `,` and `%2C`
between values, all round-trip byte-identical. plex.tv normalizes nothing, so the shape we read is
whichever writer last touched that account — Plex Web writes `&` with encoded values, plexapi writes
`|` with `%2C`, Shortlist writes plain — and a parser that understands only its own dialect corrupts
the other two (issue #77).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from urllib.parse import unquote

from loguru import logger

from shortlist.engine.models import LABEL_PREFIX, SHARED_LABEL_PREFIX, FilterSnapshot, UserProfile, UserType

if TYPE_CHECKING:
    from shortlist.engine.clients.plextv import PlexTvClient, PlexTvUser

FILTER_FIELDS = ("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos")
RESTRICTED_FILTER_FIELDS = ("filterMovies", "filterTelevision")


class FilterParseError(ValueError):
    """A share filter didn't parse; refuse to touch it rather than risk clobbering it."""


# Plex accepts BOTH separators between conditions, and plex.tv stores whatever it is given
# byte-for-byte — live-verified on a real account 2026-08-10: `|`+plain, `|`+`%2C`, `&`+plain and
# `&`+`%2C` all round-tripped identically, with no server-side normalization. So the form we read is
# whichever writer last touched that account: Plex Web writes `&` with URL-encoded values, plexapi
# writes `|` with `%2C` (myplex.py `_filterDictToStr`), and Shortlist writes plain. All three reach us.
_CONDITION_SEP = re.compile(r"([|&])")
# The fallback when `&` turns out to be part of a label rather than a separator — see _split_conditions.
_PIPE_SEP = re.compile(r"([|])")
# `%2C` is a comma that Plex Web encoded. Splitting on the bare comma alone left `A%2CB` as ONE value,
# so a merge appended to it and produced a value no reader could split back apart.
_VALUE_SEP = re.compile(r"(%2C|%2c|,)")


@dataclass(frozen=True)
class FilterCondition:
    field: str
    op: str  # "=" or "!="
    values: tuple[str, ...]
    # The separators as they were READ, so `serialize_filter(parse_filter(s)) == s` holds for any
    # filter Plex hands us — including one a third-party tool wrote in a form we would not choose.
    # `sep` joins this condition to the PREVIOUS one and is ignored on the first. `value_seps[i]`
    # joins `values[i]` to `values[i+1]`; empty means "use the default", which is what every
    # hand-built condition gets.
    sep: str = "|"
    value_seps: tuple[str, ...] = ()

    def joined_values(self) -> str:
        """The value list re-joined with exactly the separators it was parsed with."""
        if not self.values:
            return ""
        seps = self._padded_seps()
        out = self.values[0]
        for value, sep in zip(self.values[1:], seps, strict=True):
            out += sep + value
        return out

    def _padded_seps(self) -> tuple[str, ...]:
        """One separator per gap. A condition built by hand (or grown by a merge) carries fewer than
        it needs, so the last known separator is repeated — which keeps an appended label in the same
        encoding as the values already there rather than mixing the two."""
        needed = max(0, len(self.values) - 1)
        if len(self.value_seps) >= needed:
            return self.value_seps[:needed]
        fill = self.value_seps[-1] if self.value_seps else ","
        return self.value_seps + (fill,) * (needed - len(self.value_seps))


def _same_value(a: str, b: str) -> bool:
    """Do two filter values name the same Plex label?

    Compared URL-DECODED and case-folded, because the same label reaches us written both ways —
    Plex Web percent-encodes (`Age%200`), Shortlist and plexapi do not (`Age 0`) — and Plex's own tag
    matching is case-insensitive. Comparing raw bytes made an already-excluded label look absent, so
    a merge appended a second copy of it in the other encoding.
    """
    return unquote(a).casefold() == unquote(b).casefold()


def _split_conditions(raw: str) -> list[tuple[str, str]]:
    """Split a filter into ``(condition, separator-before-it)``, choosing which separators are real.

    ``&`` is a condition separator in the form Plex Web writes — but it is also a perfectly ordinary
    character inside a LABEL: ``label!=Kids & Family`` is a filter a person can create in Plex, and
    splitting it unconditionally leaves ``' Family'`` with no operator. Raising on that would be a
    regression with a server-wide blast radius: `FilterParseError` reaches the blanket handler in
    `_privacy_sync_phase`, which blocks promotion for EVERY user until somebody hand-edits that one
    account's filter. That is the shape this codebase already has a scar from (#14).

    So try the greedy split first, and if any piece comes out without an operator, treat ``&`` as
    ordinary text and split on ``|`` alone — which is exactly how such a filter parsed before ``&``
    was understood at all. Only genuinely unparseable input reaches the caller's error.
    """
    for pattern in (_CONDITION_SEP, _PIPE_SEP):
        # `re.split` with a capturing group yields [part, sep, part, sep, part, ...].
        chunks = pattern.split(raw)
        parts = [(chunks[i], chunks[i - 1] if i else "|") for i in range(0, len(chunks), 2)]
        if all("=" in part for part, _ in parts):
            return parts
    return parts


def parse_filter(raw: str) -> list[FilterCondition]:
    """Parse ``'label!=a,b|contentRating=PG'`` into ordered conditions.

    Conditions are separated by ``|`` OR ``&`` and values by ``,`` or ``%2C``; which one was used is
    recorded on each condition so serializing gives the original bytes back. Splitting on ``|`` and
    ``,`` alone silently mis-parsed the form Plex Web writes — ``label=Age%200%2CAge%203&label!=X``
    became a single condition whose FIELD was ``'label=Age%200%2CAge%203&label'``, so the existing
    exclude clause was invisible, a merge appended a second one, and the result was a three-fragment
    string that Plex Web itself could not read: the affected user's Restrictions tab failed with
    "Something went wrong" until the value was rewritten by hand (issue #77).

    Args:
        raw: The filter string from plex.tv (may be empty).

    Returns:
        Ordered conditions; values are raw strings, never URL-decoded.

    Raises:
        FilterParseError: If any condition has no operator, or if a parsed field still contains a
            separator — either means a form we cannot faithfully represent, and rewriting one of
            those is how a filter gets corrupted rather than merged.
    """
    if not raw:
        return []
    conditions = []
    for part, sep in _split_conditions(raw):
        for op in ("!=", "="):
            head, found, tail = part.partition(op)
            if not found:
                continue
            if any(c in head for c in "|&="):
                raise FilterParseError(f"unparseable condition {part!r} in filter {raw!r}")
            pieces = _VALUE_SEP.split(tail) if tail else []
            conditions.append(
                FilterCondition(
                    head,
                    op,
                    tuple(pieces[0::2]),
                    sep=sep,
                    value_seps=tuple(pieces[1::2]),
                )
            )
            break
        else:
            raise FilterParseError(f"unparseable condition {part!r} in filter {raw!r}")
    return conditions


def serialize_filter(conditions: list[FilterCondition]) -> str:
    """Render conditions back to a filter string, reusing each one's own separators."""
    return "".join((c.sep if i else "") + f"{c.field}{c.op}{c.joined_values()}" for i, c in enumerate(conditions))


def _house_style(conditions: list[FilterCondition]) -> tuple[str, str]:
    """The separators this particular filter already uses, so anything we add matches it.

    Appending a `|` clause to a filter Plex Web wrote with `&` is precisely what produced the
    unreadable three-fragment value in issue #77 — plex.tv stores whatever it is handed, so a string
    mixing both separators is what the next reader (Plex Web) has to cope with, and it cannot.
    """
    condition_sep = "&" if any(c.sep == "&" for c in conditions[1:]) else "|"
    value_seps = [s for c in conditions for s in c.value_seps]
    value_sep = next((s for s in value_seps if s.casefold() == "%2c"), ",")
    return condition_sep, value_sep


def merge_label_excludes(raw: str, labels: set[str]) -> str:
    """Union `labels` into the first ``label!=`` condition, byte-preserving everything else.

    Membership is case-insensitive (Plex tag matching is) and encoding-insensitive, so a case- or
    percent-encoded variant of an already excluded label is never appended as a duplicate.
    """
    conditions = parse_filter(raw)
    _, house_value_sep = _house_style(conditions)
    for i, cond in enumerate(conditions):
        if cond.field == "label" and cond.op == "!=":
            missing = [v for v in sorted(labels) if not any(_same_value(v, present) for present in cond.values)]
            if not missing:
                return raw
            # Which separator to join the NEW labels with. The clause's own, when it has one. When it
            # holds a single value it has none, and defaulting to a plain comma there put a `,` inside
            # a filter written entirely with `%2C` — so fall back to the encoding the rest of this
            # filter uses, not to ours. Existing gaps keep exactly the separators they were read with.
            existing = cond._padded_seps()
            fill = cond.value_seps[-1] if cond.value_seps else house_value_sep
            grown = cond.values + tuple(missing)
            conditions[i] = replace(
                cond,
                values=grown,
                value_seps=existing + (fill,) * (len(grown) - 1 - len(existing)),
            )
            return serialize_filter(conditions)
    if labels:
        condition_sep, value_sep = _house_style(conditions)
        ordered = tuple(sorted(labels))
        conditions.append(
            FilterCondition(
                "label",
                "!=",
                ordered,
                sep=condition_sep,
                value_seps=(value_sep,) * max(0, len(ordered) - 1),
            )
        )
    return serialize_filter(conditions)


def remove_label_excludes(raw: str, labels: set[str]) -> str:
    """Remove exactly `labels` from ``label!=`` conditions; drop the condition if it empties."""
    conditions = parse_filter(raw)
    out = []
    for cond in conditions:
        if cond.field == "label" and cond.op == "!=":
            seps = cond._padded_seps()
            kept = [
                (value, index)
                for index, value in enumerate(cond.values)
                if not any(_same_value(value, t) for t in labels)
            ]
            if not kept:
                continue
            # Keep the separator that PRECEDED each surviving value (the first has none), so removing
            # a value out of the middle cannot change the encoding of the ones left behind.
            cond = replace(
                cond,
                values=tuple(value for value, _ in kept),
                value_seps=tuple(seps[index - 1] for _, index in kept[1:]),
            )
        out.append(cond)
    return serialize_filter(out)


def shortlist_labels_in(raw: str, label_prefix: str) -> set[str]:
    """Return the shortlist-owned labels currently excluded in a filter string.

    Matched URL-DECODED: our own labels never need encoding (a slug is `[a-z0-9_]`), but a writer
    that encodes everything still hands them back percent-encoded, and reading them as foreign made
    Shortlist blind to its OWN excludes — so it could neither count them nor remove them at uninstall.
    """
    prefix = f"{label_prefix}_".lower()
    found = set()
    for cond in parse_filter(raw):
        if cond.field == "label" and cond.op == "!=":
            found.update(v for v in cond.values if unquote(v).lower().startswith(prefix))
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
    # A falsy label is never a real row, and writing one produces `label!=A,,B` — a filter Plex
    # cannot act on, which fails OPEN. Dropped here as well as at the source (`deliver_rows` only
    # records a label it actually stored) because this is the last gate before a real share filter.
    stored_labels = {k: v for k, v in stored_labels.items() if v}
    shared_labels = shared_labels or {}
    excludes: set[str] = set()
    own_lower = (own_label or "").lower()
    for label in stored_labels.values():
        # Case-insensitive to match the self-exclusion prune below. Both read the same PMS-cased
        # dict today, but if that ever drifted a case-sensitive compare here would ADD the label
        # while the prune REMOVED it — a flip-flop written to plex.tv every night.
        if own_label and label.lower() == own_lower:
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
    label_prefix: str = LABEL_PREFIX,
    shared_labels: dict[str, set[int] | None] | None = None,
    hide_all_shared: bool = False,
    collections_known: bool = False,
    departed_slugs: set[str] | None = None,
    # Slugs whose collection is still on the server according to the TITLE MARKER — the second,
    # independent source `dead_private` needs before it removes another account's private exclude
    # (rule 4). None = "we could not read it", which never licenses a removal.
    marker_present_slugs: set[str] | None = None,
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

    `collections_known` says whether `stored_labels` is a COMPLETE enumeration of what is on the
    server. Only then may a dead shared-row exclude be pruned — see the prune block below. It defaults
    to False so a caller that hasn't thought about it gets the fail-safe behaviour.

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
    if remote.restricted and remote.restriction_profile:
        # A managed account with a PARENTAL PRESET ("little_kid" / "older_kid" / "teen"). Plex refuses
        # label restrictions outright while one is applied — its own docs: "For Managed users, the
        # restriction profile must be set to None if you wish to edit Rating and Label restrictions on
        # the library types" (support.plex.tv/articles/204232573-restricting-the-shares/). Live-confirmed
        # 2026-07-29: the write comes back 422. Skipping is therefore NECESSARY — and it is what keeps
        # one such account from blocking promotion for the whole server (#14).
        #
        # It is not, however, HARMLESS, which this comment claimed for a year: "the account sees ZERO
        # collections of any kind anyway". True of `little_kid`, FALSE of `older_kid` — one listed
        # three collections on a real server (2026-08-11, #76). Since Plex refuses the filter, nothing
        # here can hide them; what the run does instead is MEASURE it (`unhidden_rows_visible_to`, and
        # `pipeline._record_unhideable`) and tell the owner, who has two fixes we do not.
        #
        # Gated on the PROFILE, not on `restricted` alone. `/api/users` sets `restricted="1"` for every
        # managed account, preset or not — so keying on it also skipped managed users with NO age
        # restriction, who see everything and genuinely need their excludes (#20).
        #
        # BOTH are required so the new skip is a strict SUBSET of the old one: no account that used to
        # receive excludes can lose them here. The two flags come from different endpoints and nothing
        # enforces a relationship, so a `restricted="0"` account that somehow reports a profile keeps
        # its excludes rather than silently losing them.
        logger.debug(
            "{}: managed account with a '{}' profile — Plex refuses label filters for these, so it "
            "is left out of them; what it can actually see is measured separately (#76)",
            user.username,
            remote.restriction_profile,
        )
        return None

    wanted = desired_excludes(
        own_label,
        stored_labels,
        account_id=user.plex_account_id,
        shared_labels=shared_labels,
        hide_all_shared=hide_all_shared,
    )
    # Converge SHARED-row excludes: drop any shortlist SHARED-row exclude the account should no longer
    # have (re-enabled after a disable, or added to a subset row's audience). Un-hiding a *shared* row
    # only ever reveals a public or in-audience row, so one gate is enough there.
    #
    # A PRIVATE-row exclude is the leak direction, and used to be exempt from pruning entirely for that
    # reason. It no longer is — but only under THREE guards, because a departed person's exclude
    # otherwise sits in every account's filter for ever (a real server reached 990-character filter
    # strings, growing by one entry per departure). All three must agree the row is gone:
    #
    #   1. The SERVER, BY LABEL: a complete, non-empty enumeration in which no collection carries the
    #      label (`existing_lower`, from `collection.labels`).
    #   2. The SERVER, BY MARKER: no collection whose invisible title marker encodes that person's
    #      account (`marker_present_slugs`, from `PlexClient.marked_account_ids`).
    #   3. OUR OWN DATABASE: `departed_slugs` — people plex.tv has stopped listing, or whom the owner
    #      has removed. POSITIVE evidence that this person is gone.
    #
    # 1 and 2 are two DIFFERENT reads of the same server, and that is the point. Rule 4: a label
    # re-read that succeeds carrying no `<Label>` is indistinguishable from a genuinely unlabelled row,
    # so guard 1 alone can be satisfied by a read that simply failed to see anything. The marker rides
    # in the title, which the collections listing returns inline, so the two fail independently. Guard
    # 2 was added 2026-08-12 after a review demonstrated the single-source version removing a live
    # person's exclude from another account's filter on one label-less read.
    #
    # (In the assembled pipeline a single bad read was already survivable — `stored_labels` is a union
    # of two enumerations and `sweep_broken_rows` deletes marker-carrying label-less rows first — but
    # that was accidental, undocumented and one refactor away from being lost. This makes it explicit.)
    #
    # Guard 2 is deliberately an ASSERTION that someone left, not the ABSENCE of them from tonight's
    # user list. The first version of this was the latter, and it was unsound: `engine_run(ctx, [])`
    # is a real and frequent call — every `privacy.sync` job and `user.restore` — and a scoped or
    # paused run is narrower still, so "not in tonight's users" is true of almost everybody almost all
    # the time. That reduced the pair back to guard 1 alone, which is the one the code already says can
    # lie. Absence of scope proves nothing; departure is a fact we recorded. `departed_slugs=None`
    # means the caller could not say — and not knowing never licenses a removal.
    #
    # Foreign filters are untouched (both primitives byte-preserve them).
    #
    # EVERY removal below is gated on `existing_lower is not None` — a complete, non-empty enumeration
    # of what is on the server. `wanted` is derived from that same enumeration, so a PMS that answers
    # 200 with no collections (mid library-index rebuild, or just restarted) makes every shared exclude
    # look unwanted at once. Without the gate, one such read strips them from every account on the
    # server in a single pass, and nothing re-adds them until a read succeeds.
    shared_lower = set(shared_labels or {})
    wanted_lower = {w.lower() for w in wanted}
    # Every label that EXISTS on the server right now, lowercased — but only when the caller could
    # actually enumerate them. `None` means "we don't know", and not knowing must never license a
    # removal (see `dead_shared` below).
    # `collections_known and stored_labels`: an EMPTY enumeration is not evidence of absence. A PMS
    # mid library-index rebuild answers 200 with no collections, which is indistinguishable from "every
    # row is gone" — and acting on that reading removes excludes across every account on the server.
    existing_lower = {v.lower() for v in stored_labels.values()} if (collections_known and stored_labels) else None
    # NOT shared-only despite the section header above: it also collects `excluded_from_self` (an
    # account's own label sitting in its own filter), a private-row exclude — but one whose removal
    # is still leak-safe, since un-hiding someone from their OWN row can't expose it to anyone else.
    prunable: set[str] = set()
    for fieldname in RESTRICTED_FILTER_FIELDS:
        for lbl in shortlist_labels_in(remote.filters[fieldname], label_prefix):
            stale_shared = (
                existing_lower is not None and lbl.lower() in shared_lower and lbl.lower() not in wanted_lower
            )
            # A `shortlist__shared_*` exclude for a row that no longer EXISTS on the server. Left
            # alone it accumulated for ever: deleting a shared row, or flipping it to per-person,
            # takes it out of `shared_labels`, so the prune above stopped considering it and the dead
            # entry sat in all ~48 accounts' filters permanently.
            #
            # Safe because the collection is gone: removing an exclude that matches nothing cannot
            # reveal anything. That reasoning depends entirely on KNOWING it is gone, which is why
            # this is gated on a successful enumeration rather than on an empty lookup — a failed or
            # partial PMS read would otherwise read as "deleted" and un-hide a live row.
            dead_shared = (
                existing_lower is not None
                and lbl.lower().startswith(SHARED_LABEL_PREFIX.lower())
                # NOT declared shared by the config either. Belt to the enumeration's braces: a row the
                # config still declares is a LIVE row whose visibility is `stale_shared`'s business, and
                # only a row that is gone from BOTH the config and the server is provably dead. Without
                # this, a PMS that answers an empty (but successful) collections read strips the
                # excludes hiding a configured, restricted-audience shared row from everyone.
                and lbl.lower() not in shared_lower
                and lbl.lower() not in existing_lower
            )
            if dead_shared:
                stale_shared = True
            # A PRIVATE row's exclude whose collection is gone AND whose owner is nobody we still
            # build for. This is the ONE prune that removes another account's private-row exclude, so
            # it is the one pointed at the leak direction: get it wrong and a live row becomes visible
            # to everyone, with no Privacy Check left to notice (removed 2026-07-16).
            #
            # It needs TWO INDEPENDENT sources agreeing the collection is gone, because rule 4 says one
            # is not enough. `existing_lower` comes from `collection.labels`, i.e. plexapi's silent
            # per-collection re-read, and a re-read that SUCCEEDS carrying no `<Label>` is
            # indistinguishable from a genuinely unlabelled row — so on its own it can license
            # un-hiding a row that is still there. `marker_present_slugs` is the second source: the
            # 64-zero-width-char title marker, which arrives inline in the collections listing and is
            # therefore not the read that can come back empty (`PlexClient.marked_account_ids`).
            #
            # `wanted_lower` is NOT a third source and never was: `wanted` is
            # `desired_excludes(own_label, stored_labels, ...)`, built from the same `stored_labels`
            # that produced `existing_lower`, so for another account's private label it is strictly
            # implied by the `existing_lower` clause. It is kept because it costs nothing and reads as
            # intent, not because it is independent — the comment here used to claim it was.
            #
            # None means "we could not read it", and not knowing must never license a removal — the
            # same rule `existing_lower` follows.
            #
            # Compared with `_same_value` (unquote + casefold) like every other comparison in this
            # module, NOT raw. Plex Web writes filter values percent-encoded, so the same label reaches
            # us written both ways — and this is the only prune whose condition is NON-membership, so a
            # raw compare that fails to recognise an encoded live label licenses its removal instead of
            # preventing it. The slug is taken from the decoded label for the same reason.
            decoded = unquote(lbl)
            decoded_slug = decoded[len(label_prefix) + 1 :].lower()
            dead_private = (
                existing_lower is not None
                and departed_slugs is not None
                and marker_present_slugs is not None
                and not decoded.lower().startswith(SHARED_LABEL_PREFIX.lower())
                and not any(_same_value(lbl, e) for e in existing_lower)
                and not any(_same_value(lbl, w) for w in wanted_lower)
                and decoded_slug not in {m.lower() for m in marker_present_slugs}
                and decoded_slug in {d.lower() for d in departed_slugs}
            )
            # An account's OWN label must never sit in its own filter — that hides a person from
            # their own row permanently, because private-row excludes are otherwise union-only.
            # Reachable: delete a user's DB row while their collection still exists on Plex, so
            # `own_label` is None and `desired_excludes` adds their own label to their own filter;
            # re-adding them later never undid it. Un-hiding someone's OWN row cannot leak to anyone
            # else — the same reasoning that makes the shared case safe to prune.
            excluded_from_self = bool(own_label) and lbl.lower() == (own_label or "").lower()
            if stale_shared or excluded_from_self or dead_private:
                prunable.add(lbl)

    desired_fields = {}
    for fieldname in RESTRICTED_FILTER_FIELDS:
        current = remote.filters[fieldname]
        merged = merge_label_excludes(current, wanted)
        if prunable:
            merged = remove_label_excludes(merged, prunable)
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


def clear_our_excludes(
    plextv: PlexTvClient,
    user: UserProfile,
    remote: PlexTvUser | None,
    *,
    label_prefix: str = LABEL_PREFIX,
    dry_run: bool = False,
) -> dict[str, tuple[str, str]] | None:
    """Take every Shortlist exclude back out of one account's share filters, and add none.

    The "leave this account's Plex sharing alone" path. It is the only write in this module that makes
    the server LESS private, and it does so because the owner asked: an account whose own Plex
    restrictions conflict with our excludes (an "allow only" label list, discussion #92) needs its
    filters left as its owner wrote them, and the price is that the account can see other people's
    rows. Every OTHER account still excludes this one's label, so nothing here exposes a row to anyone
    but the person the owner named.

    Idempotent by construction — it converges on "no per-person `shortlist_<slug>` value anywhere in
    this filter", so the steady state writes nothing and a single pass after the switch is flipped is
    enough. Foreign conditions are byte-preserved (`remove_label_excludes`), so a filter that holds
    only somebody else's rules comes back untouched.

    A RESTRICTED shared row's exclude is deliberately NOT removed — see the loop below.

    No snapshot is taken (rule 2 covers writes that RESTRICT). An account with our labels in its
    filter has been through `sync_user_restrictions`, which snapshotted the true pre-Shortlist value
    already; an account without them is a no-op here. Taking one now would capture our own pollution
    and hand uninstall the wrong thing to restore.

    Args:
        plextv: The plex.tv client.
        user: The account to leave alone.
        remote: Their current plex.tv record, or None if they no longer share the server.
        label_prefix: The label prefix Shortlist owns.
        dry_run: Log the would-be diff instead of writing (rule 8).

    Returns:
        The ``{field: (before, after)}`` diff written, or None when there was nothing of ours to
        remove.

    Raises:
        FilterParseError: If a filter cannot be parsed — the caller decides, and refusing to touch it
            is the safe answer here too.
    """
    if user.user_type is UserType.OWNER:
        return None
    if remote is None:
        return None
    changed: dict[str, tuple[str, str]] = {}
    for fieldname in RESTRICTED_FILTER_FIELDS:
        current = remote.filters.get(fieldname, "")
        if not current:
            continue
        # PER-PERSON excludes only. `SHARED_LABEL_PREFIX` is `shortlist__shared_`, which starts with
        # `shortlist_` — so the obvious `shortlist_labels_in()` also matches a shared row's label, and
        # stripping one would hand this account a shared row whose audience the owner RESTRICTED in
        # the audience picker. That exclude is the only thing hiding it, and nothing re-adds it: the
        # next run skips this account entirely.
        #
        # Two explicit owner decisions collide here — "leave this account's sharing alone" and "this
        # row is only for those people" — and the tie goes to the one whose failure mode is a leak.
        # The cost is that a later audience WIDENING never reaches a left-alone account, so their
        # exclude goes stale in the direction of seeing less; switching management back on resyncs it.
        # #92's actual complaint is the accumulating per-person excludes, which this still clears.
        ours = {
            label
            for label in shortlist_labels_in(current, label_prefix)
            if not unquote(label).lower().startswith(SHARED_LABEL_PREFIX.lower())
        }
        if not ours:
            continue
        cleaned = remove_label_excludes(current, ours)
        if cleaned != current:
            changed[fieldname] = (current, cleaned)
    if not changed:
        return None
    if dry_run:
        logger.info(
            "[dry-run] {}: would remove Shortlist's excludes and leave the rest — {}",
            user.username,
            summarise_filter_diff(changed, label_prefix),
        )
        return changed
    plextv.update_user_filters(user.plex_account_id, {k: after for k, (_before, after) in changed.items()})
    logger.info(
        "{}: Shortlist's excludes removed — this account's sharing is left alone from now on ({})",
        user.username,
        summarise_filter_diff(changed, label_prefix),
    )
    return changed


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
    # `.get` on BOTH sides. `snapshot.filters` is a JSON column holding whatever was persisted at
    # snapshot time, not a validated five-field dict — so a snapshot missing a field the remote now
    # has raised KeyError here. The caller has no per-user guard, so one such snapshot aborted the
    # whole restore loop and left every remaining account carrying Shortlist's excludes for ever,
    # after the operator had already typed UNINSTALL. A missing field restores as empty, which is
    # what "this user had no filter here" means.
    changed = {
        k: snapshot.filters.get(k, "")
        for k in FILTER_FIELDS
        if remote.filters.get(k, "") != snapshot.filters.get(k, "")
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


_HUB_COLLECTION_KEY = re.compile(r"/library/collections/(\d+)")


def unhidden_rows_on_home(hubs: list[dict], owned: dict[str, object], user_slug: str) -> list[int]:
    """ratingKeys of OUR per-person rows on this account's HOME that are not its own.

    The hub twin of `unhidden_rows_visible_to`, and the surface deliberately chosen for the
    filter-enforcement canary. Two reasons it is the right one. It is what the complaint is actually
    about — "every user could see all six rows on the home screen" (#88) — and it is one read rather
    than a walk of every library's collections, which matters for something that runs each night.

    The other surface is left alone on purpose: whether a real PMS applies a share `label!=` filter to
    the library COLLECTIONS listing (as opposed to Home) is not something this repo has a recorded
    answer for, and plex-safety rule 11 says an assumption about PMS behaviour needs a fixture from a
    real server before code leans on it. Reporting a leak from an unverified assumption would be the
    worst of both worlds: a privacy alarm nobody can act on.

    Args:
        hubs: `PlexClient.user_hubs(token)` for the account under test — their Home, as they see it.
        owned: `PlexClient.owned_collections()`, label-slug -> row with `rating_keys`.
        user_slug: Whose account this is, so their own row is not reported against them.

    Returns:
        Sorted ratingKeys of other people's rows on this account's Home. Empty means clean.
    """
    shared_marker = SHARED_LABEL_PREFIX[len(LABEL_PREFIX) + 1 :].lower()
    ours: set[int] = set()
    theirs: set[int] = set()
    for slug, row in owned.items():
        if slug.lower().startswith(shared_marker):
            continue  # a shared row is meant to be seen; its audience is enforced elsewhere
        keys = {int(k) for k in getattr(row, "rating_keys", ())}
        ours |= keys
        if slug.lower() == user_slug.lower():
            theirs |= keys
    if not ours:
        return []
    visible: set[int] = set()
    for hub in hubs:
        match = _HUB_COLLECTION_KEY.search(str(hub.get("key") or hub.get("hubKey") or ""))
        if match:
            visible.add(int(match.group(1)))
    return sorted((visible & ours) - theirs)


def unhidden_rows_visible_to(pms_as_user, owned: dict[str, object], user_slug: str) -> list[int]:
    """ratingKeys of OUR per-person rows this account can see that are not its own.

    Answers, by measurement, the question the skip above only ever assumed: when Plex refuses a label
    filter for an account, can that account actually see anyone else's row?

    The justification for skipping profiled managed accounts is that they "see zero collections of any
    kind anyway, so there is nothing for an exclude to hide". That was confirmed against a
    ``little_kid`` account and generalised to every profile. Measured on a real server 2026-08-11, an
    ``older_kid`` account saw three collections — so for that account we neither hid other people's
    rows nor reported that we could not. Whether one actually leaks then depends on whether the row's
    contents survive the parental content filter, which is luck rather than a guarantee.

    Identity is the ratingKey, never the title: every row is named from the same template
    (``✨ {library} Picked for You``) and tells them apart only by an invisible marker, so a title
    comparison reports a person's OWN row as somebody else's. That mistake was made while
    investigating this, which is why it is spelled out here.

    Args:
        pms_as_user: A PMS client authenticated AS the account under test — what it can see, not what
            the owner can.
        owned: ``PlexClient.owned_collections()``, label-slug -> row with ``rating_keys``. The FRESH
            server read the privacy phase already performs, deliberately not the delivery ledger: the
            ledger is loaded when the run's context is built, so on a first run it is empty, and an
            empty set of ours makes every account read as clean — the exact silence this exists to
            break.
        user_slug: Whose account this is, so their own rows are not reported against them.

    Returns:
        Sorted ratingKeys of other people's rows visible to this account. Empty means genuinely clean.

    Raises:
        Whatever the PMS read raises. A failure must NOT read as "nothing visible" — an empty answer
        from a failed read is the shape plex-safety rule 4 exists to refuse.
    """
    shared_marker = SHARED_LABEL_PREFIX[len(LABEL_PREFIX) + 1 :].lower()  # "_shared_" -> what the slug keeps
    ours: set[int] = set()
    theirs: set[int] = set()
    for slug, row in owned.items():
        # A shared row is one collection everybody is meant to see, subject to its own audience — not
        # an exposure. Including it would fire this on every server that runs one.
        if slug.lower().startswith(shared_marker):
            continue
        keys = {int(k) for k in getattr(row, "rating_keys", ())}
        ours |= keys
        if slug.lower() == user_slug.lower():
            theirs |= keys
    if not ours:
        return []
    visible: set[int] = set()
    for section in pms_as_user.sections():
        for collection in pms_as_user._section_collections(section):
            key = getattr(collection, "rating_key", None) or getattr(collection, "ratingKey", None)
            if key is not None:
                visible.add(int(key))
    return sorted((ours & visible) - theirs)
