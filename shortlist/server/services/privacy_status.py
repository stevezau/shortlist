"""What every Plex account's share filter says RIGHT NOW — one computation, two consumers.

This is a MOVE out of `api/support.py`, not a reimplementation, and that is deliberate. The logic
below is hard-won: the `existing_row_labels` fail-safe, counting LABELS rather than clauses, the
`manage_sharing` split, and the username-vs-slug bug were each a real reported failure. A second copy
would relearn every one of them. `GET /api/support/sharing` renders it as a copy-for-support text
block; `GET /api/privacy/status` renders it as a screen.

**Every value here is a LIVE READ.** Nothing in this module may be sourced from
`report.filter_writes`, from `run.privacy_sync` events, or from anything else recording what
Shortlist WROTE. Those record an intention that reached plex.tv, and reading one back as a privacy
verdict is a false-privacy bug on the one surface whose entire job is to be believed. A read that
FAILS is reported as an error, never as "nothing to hide".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import unquote

from sqlalchemy.orm import Session

from shortlist.engine import privacy
from shortlist.engine.models import LABEL_PREFIX, SHARED_LABEL_PREFIX
from shortlist.server.db.models import User
from shortlist.server.services.redaction import known_identifiers, redact_all, scrub_secrets
from shortlist.server.settings_store import SettingsStore

#: The label prefix every Shortlist PER-PERSON exclusion carries, lowercased. Plex title-cases new
#: labels, so comparisons are always case-insensitive.
#:
#: The trailing underscore is load-bearing and this is NOT `engine.models.LABEL_PREFIX`, which is
#: `"shortlist"` with no underscore (plex-safety rule 4). Every row also carries a constant
#: `shortlist` label; matching on that would resolve an empty slug, which is in no roster — so every
#: row on the server would classify as an orphan.
PER_PERSON_LABEL_PREFIX = f"{LABEL_PREFIX}_".lower()

#: A short PMS timeout, matching `support._PROBE_TIMEOUT_S`: every caller here is a screen someone is
#: waiting in front of, not a run. Past a few seconds the tab reads as broken and the person
#: retries, which is the worst thing to do to an already-slow server.
_PROBE_TIMEOUT_S = 8


@dataclass
class SharingStatus:
    """One live reading of the whole server's sharing state, with the time it was taken.

    Attributes:
        read_at: When this reading was taken, ISO/UTC. Every claim it carries is as of this moment
            and no later — another tool, or the owner in Plex Web, can rewrite a filter a second
            after it.
        accounts: One entry per plex.tv account, in roster order.
        rows_on_plex: The per-person row labels that exist on Plex right now — what the verdict was
            measured AGAINST. Lets a caller tell "everyone is covered" from "there was nothing to
            cover", which used to be the same empty answer.
        rows_error: Why the PMS row read failed, when it did. Non-empty means the verdict is UNKNOWN.
        error: Why the plex.tv read failed, when it did. Non-empty means `accounts` is empty and
            nothing may be reported as hidden.
    """

    read_at: str
    accounts: list[dict] = field(default_factory=list)
    rows_on_plex: list[str] = field(default_factory=list)
    rows_error: str | None = None
    error: str | None = None


def is_our_label(value: str) -> bool:
    """Is this filter value one of Shortlist's own labels?

    Matched URL-DECODED, the same way `privacy` matches them: plex.tv stores whatever encoding the
    last writer used, so the same label reaches us written more than one way. Comparing raw bytes
    here would report a label this account already excludes as missing — the opposite of the one
    question this module exists to answer.

    Args:
        value: One value from a parsed share-filter condition.

    Returns:
        Whether Shortlist wrote it.
    """
    return unquote(value).lower().startswith(PER_PERSON_LABEL_PREFIX)


def per_person_excludes(account: dict) -> list[str]:
    """The Shortlist excludes on one account that "leave their sharing alone" would actually remove.

    A restricted shared row's `shortlist__shared_*` exclude is deliberately kept, so it is not
    evidence that a removal is owed. `is_our_label` matches on `shortlist_`, which a shared label
    also starts with — the same collision that made the writer strip them in the first place.

    Args:
        account: One entry from :attr:`SharingStatus.accounts`.

    Returns:
        The per-person labels only, shared-row labels excluded.
    """
    shared = SHARED_LABEL_PREFIX.lower()
    return [v for v in account["shortlist_excludes"] if not unquote(v).lower().startswith(shared)]


def existing_row_labels(
    store: SettingsStore,
    fail: Callable[[BaseException], str],
    plex_factory: Callable[[SettingsStore], object] | None = None,
) -> tuple[set[str], str | None]:
    """Lowercased labels of the PER-PERSON rows that exist on Plex right now, plus why not if unread.

    The engine hides a row by excluding the label it found on the PMS (`privacy.desired_excludes`
    works off `stored_labels`), so "which labels belong in everyone's filter" is a question only the
    server can answer — the user table cannot, because a user with no row yet has no label anywhere.

    Shared rows are left out. They are public (or audience-scoped) by design and are deliberately NOT
    excluded from everyone, so counting them here would report every account as leaking one.

    "No labels came back" is checked against the title MARKER, not just against exceptions. A read
    that succeeds and returns nothing looks identical to a server with no rows — and on a server
    whose rows have LOST their labels, which is the state this whole area exists to defend against,
    those rows are visible to everyone. Reporting "there is nothing for anyone to hide" there would
    be the most reassuring possible lie. The marker is independent of the label, so the two
    disagreeing says which of the two is true.

    Args:
        store: Settings, for the Plex URL and token.
        fail: Turns an exception into a string safe to hand a client — scrubbed of credentials and of
            this install's own identity (rule 9). Passed in because the support report scrubs with the
            per-request literals its own `ContextVar` carries.
        plex_factory: Optional PMS client factory; defaults to this module's.

    Returns:
        ``(labels, error)``. An error means UNKNOWN, never "none": a read that failed must not be
        reported as a clean bill of health, which is the same fail-safe the engine applies to this
        enumeration (see ``collections_known`` in ``pipeline._privacy_sync_phase``).
    """
    try:
        plex = (plex_factory or _plex_client)(store)
        if plex is None:
            return set(), "Plex isn't connected."
        owned = plex.owned_collections(LABEL_PREFIX)
        shared_prefix = SHARED_LABEL_PREFIX.lower()
        labels = {row.label.lower() for row in owned.values() if not row.label.lower().startswith(shared_prefix)}
        if not labels:
            # Same client, so the collection list is already cached — this costs no extra listing read.
            marked = sum(1 for row in plex.owned_row_surfaces(flags=False) if row.get("marked"))
            if marked:
                return set(), f"{marked} collection(s) are ours by title but carry no label"
    except Exception as e:
        # Building the client is inside the guard too. It is documented never to raise, but this is
        # the fail-safe path for the read that reports leaks — one that raises here would take out
        # the whole section instead of reporting "unknown".
        return set(), fail(e)
    return labels, None


def read_sharing_status(
    session: Session,
    store: SettingsStore,
    machine_id: str,
    fail: Callable[[BaseException], str] | None = None,
    plex_factory: Callable[[SettingsStore], object] | None = None,
    plextv_factory: Callable[[SettingsStore, str], object] | None = None,
) -> SharingStatus:
    """Read every account's live share filters and separate OUR exclusions from everything else.

    Two reads: one plex.tv roster, one PMS collections listing. Nothing is written and no token is
    minted, so this is safe to call from a screen.

    Args:
        session: An open DB session, for the user roster and for redaction literals.
        store: Settings, for the Plex credentials.
        machine_id: This server's machine id, for the plex.tv client.
        fail: Optional exception-to-string conversion. The support report passes its own, which
            scrubs with the per-request literals its `ContextVar` carries; everyone else gets the
            equivalent built from this session.
        plex_factory: Optional PMS client factory, so a caller that already builds one its own way
            (and whose tests already stub it) keeps a single seam.
        plextv_factory: Optional plex.tv client factory, same reason.

    Returns:
        The whole server's sharing state as of now.
    """
    fail = fail or _failure_scrubber(session)
    plex_factory = plex_factory or _plex_client
    plextv_factory = plextv_factory or _plextv_client
    status = SharingStatus(read_at=datetime.now(UTC).isoformat(timespec="seconds"))

    # Which labels need hiding: the per-person rows that EXIST ON PLEX, read from the server.
    #
    # NOT the enabled-user list, which is what this used to do. The engine only ever excludes labels
    # it found on the PMS (`desired_excludes` <- `stored_labels`), so an enabled user who has never
    # received a row — a cold start, zero picks, a delivery that failed — contributes a label that
    # can never appear in anybody's filter. Every account then read as "missing" it, and this
    # reported `0 of N accounts hide every row` on a perfectly healthy server. One reporter's server
    # had 13 of 24 users on `picks=0`; the tool told them their privacy was entirely broken (#76).
    #
    # Nor `len(accounts) - 1`, which is wrong in both directions: the OWNER has a row but is absent
    # from the plex.tv roster (`list_users` returns shared + Home users only), so that undercounts;
    # and a DISABLED user is in the roster with no row, so it also overcounts.
    #
    # Whose row is whose comes from ALL users, not just enabled ones: a paused or disabled person
    # still owns their collection, and their own label must never count as something they should be
    # hiding from themselves (see `privacy.desired_excludes`).
    all_users = session.query(User).all()
    labelled = {u.plex_account_id: f"{PER_PERSON_LABEL_PREFIX}{u.slug}".lower() for u in all_users}
    all_labels, status.rows_error = existing_row_labels(store, fail, plex_factory)
    status.rows_on_plex = sorted(all_labels)
    # plex.tv gives us a USERNAME; `person()` and every other tool key on a SLUG, and `slugify`
    # lowercases and replaces punctuation — so they differ for essentially every real account
    # ("MooHouse" -> "moohouse", "Chris Smith" -> "chris_smith"). Passing a username on as if it were
    # a slug made the per-person section 404 for exactly the people with a privacy fault.
    slug_of = {u.plex_account_id: u.slug for u in all_users}
    name_of = {u.plex_account_id: u.display_name for u in all_users}
    type_of = {u.plex_account_id: u.user_type for u in all_users}
    profile_of = {u.plex_account_id: (u.restriction_profile or "") for u in all_users}
    # Accounts the owner asked us to leave alone. They legitimately hide nothing, so counting them as
    # a fault would make this cry wolf on a server that is exactly as its owner set it up.
    unmanaged = {u.plex_account_id for u in all_users if not u.manage_sharing}

    client = plextv_factory(store, machine_id)
    if client is None:
        status.error = "Plex isn't linked."
        return status
    try:
        accounts = client.list_users()
    except Exception as e:
        # `accounts` stays empty on purpose: with no roster, NOTHING may be reported as hidden.
        status.error = fail(e)
        return status

    for account in accounts:
        ours: dict[str, list[str]] = {}
        theirs: list[str] = []
        for name in ("filterMovies", "filterTelevision"):
            raw = account.filters.get(name) or ""
            if not raw:
                continue
            try:
                conditions = privacy.parse_filter(raw)
            except Exception:
                # A filter we cannot parse is reported verbatim rather than mis-attributed — the
                # engine refuses to rewrite one too.
                theirs.append(f"{name}: {raw} (unparseable)")
                continue
            for condition in conditions:
                mine = [v for v in condition.values if is_our_label(v)]
                others = [v for v in condition.values if not is_our_label(v)]
                if mine:
                    ours.setdefault(name, []).extend(sorted(mine))
                if others or not mine:
                    joined = ",".join(others)
                    theirs.append(f"{name}: {condition.field}{condition.op}{joined}" if joined else f"{name}: —")
        ours_flat = sorted({label for labels in ours.values() for label in labels})
        should_hide = all_labels - {labelled.get(account.id, "")}
        status.accounts.append(
            {
                "user": account.username,
                "display_name": name_of.get(account.id, account.username),
                "slug": slug_of.get(account.id, ""),
                "account_id": account.id,
                "user_type": str(type_of.get(account.id, "shared")),
                # Non-empty when Plex refuses a share filter for this account outright (422,
                # live-confirmed 2026-07-29). For those the excludes below can never be written, and
                # nothing on any screen may report them as hidden.
                "restriction_profile": profile_of.get(account.id, ""),
                # The owner turned sharing management off for this account: `missing` below is still
                # reported truthfully, but it is a setting, not a fault.
                "manage_sharing": account.id not in unmanaged,
                # LABELS, not clauses. `merge_label_excludes` unions every shortlist label into ONE
                # `label!=` condition, so counting clauses reported "2 exclusions" on a server hiding
                # forty rows — and classified a whole clause as ours whenever it contained any
                # shortlist label, blaming the owner's own `label!=Kids` on Shortlist. That made this
                # unable to answer the one question it exists for: is every row excluded for this
                # person.
                "shortlist_excludes": ours_flat,
                "shortlist_excludes_by_filter": {k: sorted(set(v)) for k, v in ours.items()},
                "other_conditions": theirs,
                "filters": {k: v for k, v in account.filters.items() if v},
                # Every row that should be hidden from THIS person: all labelled people bar
                # themselves. Their own label must never sit in their own filter — that would hide
                # them from their own row (see `privacy.py`).
                "should_hide": sorted(should_hide),
                "missing": sorted(should_hide - {unquote(v).lower() for v in ours_flat}),
            }
        )
    return status


def _failure_scrubber(session: Session):
    """An exception-to-string conversion with this install's identity and credentials stripped.

    Built once per call rather than per failure: `known_identifiers` is a query, and a roster read
    that fails fails once.
    """
    literals = known_identifiers(session)

    def fail(e: BaseException) -> str:
        return redact_all(scrub_secrets(f"{type(e).__name__}: {e}"), literals)

    return fail


def _plex_client(store: SettingsStore):
    """A short-timeout PlexClient, or None when Plex isn't connected. Never raises: every caller is a
    read that has to report the failure rather than become it."""
    from shortlist.engine.clients.plex_pms import PlexClient

    url, token = store.get("plex.url"), store.get("plex.token")
    if not url or not token:
        return None
    return PlexClient(str(url), str(token), timeout=_PROBE_TIMEOUT_S)


def _plextv_client(store: SettingsStore, machine_id: str):
    """A plex.tv client, or None when Plex isn't linked. Never raises, for the same reason."""
    from shortlist.engine.clients.plextv import PlexTvClient

    token = store.get("plex.token")
    if not token or not machine_id:
        return None
    return PlexTvClient(str(token), machine_id)
