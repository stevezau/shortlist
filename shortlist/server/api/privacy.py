"""Sharing and privacy, as an owner-readable screen rather than a support-mode text blob.

Three verifications already exist and run today: a batched plex.tv read-back that blocks promotion
(`pipeline.py:864`), an enforcement spot-check through a real account's eyes (`pipeline.py:551`), and
a live per-account filter audit (`support.py`'s `sharing`). What did NOT exist was a screen. This
module is that screen's data, over the same computation the support tool uses.

**Nothing here is derived from what Shortlist wrote.** Not `report.filter_writes`, not the
`run.privacy_sync` events, not a run's own success. Those record an intention that reached plex.tv;
they are not evidence about what plex.tv stores now, and reading one back as "hidden" would be a
false-privacy bug on the surface least able to afford one. Every account row below is a live read.

What this endpoint therefore refuses to claim, at any cost:

* **Anything outside Home.** `privacy.unhidden_rows_on_home` says plainly that whether a real PMS
  applies a share `label!=` filter to the library COLLECTIONS listing is not something this repo has
  a recorded answer for, and rule 11 forbids leaning on an unverified assumption.
* **Anything for a parental-profile account.** plex.tv rejects the filter write outright (422,
  live-confirmed 2026-07-29), so no exclude can ever be stored for them.
* **Anything about the owner.** Plex has no share for the account that owns the server (rule 5).
  A Plex limitation, not a fault.
* **Anything about the moment between checks.** Every answer carries `read_at` for that reason.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Run, Server, User, iso_utc
from shortlist.server.services import privacy_status
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/privacy", tags=["privacy"], dependencies=[Depends(require_owner)])

#: How far back to look for a run that actually measured enforcement. Matches
#: `notifications._filters_not_enforced`, which reads the same key the same way.
_ENFORCEMENT_RUN_LOOKBACK = 50


class AccountPrivacyOut(PassthroughModel):
    """One Plex account's share filter, as plex.tv reports it RIGHT NOW."""

    user: str
    display_name: str
    slug: str
    account_id: int
    #: The Shortlist user row's id, for the link to their detail page. Null for an account plex.tv
    #: knows and our roster does not — a share added since the last user sync.
    user_id: int | None
    user_type: str  # shared | managed | owner
    restriction_profile: str  # "" unless Plex refuses filters for this account
    manage_sharing: bool  # False = the owner asked us to leave this account alone
    #: Which of the six things is true of this account, decided here so the copy lives in one place
    #: in the SPA: "hiding" | "missing" | "left_alone" | "refused_by_plex" | "owner" | "unknown".
    #: "unknown" means the PMS row read failed, so there is nothing to check the filters against —
    #: never render it as clean.
    state: str
    #: The `shortlist_*` labels in this account's filters, read from plex.tv this second.
    hides: list[str]
    #: Every per-person row that exists on Plex right now, minus this account's own.
    should_hide: list[str]
    #: `should_hide` minus `hides` — rows this account can see that are not theirs. Empty means
    #: plex.tv is STORING every rule. It does NOT mean Plex is applying them (see `enforcement`).
    missing: list[str]
    #: The account's own filter conditions, untouched by Shortlist — shown so the owner can see we
    #: preserved them byte-for-byte (rule 3, made visible).
    other_conditions: list[str]


class EnforcementOut(PassthroughModel):
    """The last time a run looked through a real account's eyes at their Home screen.

    `measured` is the whole point. An empty `not_enforced` on an unmeasured run is not "all clear" —
    it is "nobody looked", and rendering the two the same is how a live alert gets cleared.
    """

    measured: bool
    run_id: int | None
    measured_at: str | None
    #: username -> ratingKeys of other people's rows visible on THEIR Home. Empty + measured = clean.
    not_enforced: dict[str, list[int]]


class PrivacyStatusOut(PassthroughModel):
    """One live reading of the whole server's sharing state."""

    read_at: str
    #: The headline, in priority order: "unreadable" (plex.tv failed) | "rows_unknown" (the PMS row
    #: read failed) | "not_enforced" (a run looked through a real account's eyes and Plex was serving
    #: other people's rows anyway) | "missing" (a hide rule is absent from someone's share) | "clean".
    #:
    #: "missing" outranks "not_enforced": the two CAN co-occur (the engine's spot-check gate is
    #: `any` of our labels, not all), and only a missing rule is something the owner's next run
    #: fixes. The exposure still reaches the enforcement panel on its own either way.
    summary: str
    accounts: list[AccountPrivacyOut]
    #: The per-person row labels that exist on Plex right now — what the verdict was measured
    #: against. Lets the screen tell "everyone is covered" from "there was nothing to cover".
    rows_on_plex: list[str]
    rows_error: str | None
    error: str | None
    enforcement: EnforcementOut


@router.get("/status", response_model=PrivacyStatusOut)
def privacy_status_endpoint(request: Request) -> dict:
    """Every account's live share filter, plus the last enforcement spot-check.

    Costs one plex.tv roster read and one PMS collections read. Writes nothing and mints no token, so
    it is safe to call from a screen the owner leaves open.

    Args:
        request: The live request, for app state.

    Returns:
        The whole server's sharing state as of now, with the enforcement measurement beside it.
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        server = session.query(Server).first()
        status = privacy_status.read_sharing_status(session, store, server.machine_id if server else "")
        rows_known = status.rows_error is None
        ids = {u.plex_account_id: u.id for u in session.query(User).all()}
        accounts = [
            _account_out(row, rows_known=rows_known, user_id=ids.get(row["account_id"])) for row in status.accounts
        ]
        # The owner is absent from `list_users` — plex.tv returns shared and Home users only — so
        # without this the one account that sees EVERY row simply would not appear. Said out loud as a
        # Plex limitation rather than left off the page, because a screen that silently omits the
        # least private account on the server is worse than one that explains why.
        #
        # Suppressed when the roster read FAILED, even though this one row needs no roster: the page
        # then says "nothing below is current", and that sentence has to be literally true of
        # everything below it. One row that quietly is current teaches the reader to doubt the banner.
        owner = session.query(User).filter_by(user_type="owner").first() if not status.error else None
        if owner is not None:
            accounts.insert(0, _owner_out(owner, status.rows_on_plex))
        enforcement = _enforcement(session)

    return {
        "read_at": status.read_at,
        "summary": _summary(status, accounts, enforcement),
        "accounts": accounts,
        "rows_on_plex": status.rows_on_plex,
        "rows_error": status.rows_error,
        "error": status.error,
        "enforcement": enforcement,
    }


def _account_out(row: dict, *, rows_known: bool, user_id: int | None) -> dict:
    """One roster account, with its state decided.

    `rows_known` comes FIRST and that is load-bearing. When the PMS row read failed there is no list
    of rows to check a filter against, so every account trivially "hides" all zero of them and would
    render as clean — a green, reassuring page produced by an outage. "Unknown" is the only honest
    answer there, and it is the same fail-safe `existing_row_labels` applies one layer down.

    Then the order of the branches is the order of the truths. "Plex refuses filters for this
    account" outranks "rules are missing", because for those accounts a missing rule is not a fault
    anyone can fix here; and "the owner asked us to leave it alone" outranks it too, for the same
    reason — it is the setting they chose, not a failure.
    """
    if not rows_known:
        state = "unknown"
    elif row["restriction_profile"]:
        state = "refused_by_plex"
    elif not row["manage_sharing"]:
        state = "left_alone"
    elif row["missing"]:
        state = "missing"
    else:
        state = "hiding"
    return {
        "user": row["user"],
        "display_name": row["display_name"],
        "slug": row["slug"],
        "account_id": row["account_id"],
        "user_id": user_id,
        "user_type": row["user_type"],
        "restriction_profile": row["restriction_profile"],
        "manage_sharing": row["manage_sharing"],
        "state": state,
        "hides": row["shortlist_excludes"],
        "should_hide": row["should_hide"],
        "missing": row["missing"],
        "other_conditions": row["other_conditions"],
    }


def _owner_out(owner: User, rows_on_plex: list[str]) -> dict:
    """The owner, whose account Plex has no share to filter.

    `missing` is empty, not "every row": there is no share here for a rule to be absent from, and
    listing every other person's row as missing would render a Plex limitation as a server-wide leak.
    """
    return {
        "user": owner.username,
        "display_name": owner.display_name,
        "slug": owner.slug,
        "account_id": owner.plex_account_id,
        "user_id": owner.id,
        "user_type": "owner",
        "restriction_profile": "",
        "manage_sharing": False,
        "state": "owner",
        "hides": [],
        "should_hide": sorted(set(rows_on_plex) - {f"{privacy_status.PER_PERSON_LABEL_PREFIX}{owner.slug}".lower()}),
        "missing": [],
        "other_conditions": [],
    }


def _summary(status: privacy_status.SharingStatus, accounts: list[dict], enforcement: dict) -> str:
    """The headline, in priority order — the worst true thing, never an average.

    A failed read comes first because everything below it is then meaningless, and a page that reads
    green off an outage is the failure this whole item exists to prevent.

    **A measured exposure beats a CLEAN filter set, and loses to a missing rule.** Both halves are
    load-bearing and neither is obvious.

    Against clean: `_verify_filters_enforced` only spot-checks accounts that already carry our
    excludes, so the state discussion #88 reported — Plex storing every rule and serving other
    people's rows anyway — has `missing` EMPTY on every account. Ranking on `missing` alone printed
    "Every account hides all N rows that aren't theirs" above the red panel saying Plex is ignoring
    the filter. Stored is not enforced.

    Against missing: the two CAN co-occur, which a first pass here assumed they could not. The
    engine's gate is `any(...)` — at least ONE of our labels, not all of them (`pipeline.py:602`) —
    and `unhidden_rows_on_home` reports any of our rows on that Home whether or not that row's
    exclude was ever stored. So an account carrying one exclude and missing another is both. When
    that happens the missing rule is the one the owner can act on (the next run merges it back),
    while "Plex is ignoring the filter" tells them to file an issue — so the actionable verdict
    leads, and the measurement still reaches the enforcement panel on its own.
    """
    if status.error:
        return "unreadable"
    if status.rows_error:
        return "rows_unknown"
    if any(a["state"] == "missing" for a in accounts):
        return "missing"
    # Only a run that actually LOOKED can report an exposure. An unmeasured empty result is "nobody
    # checked", never "somebody is exposed" — the same distinction `measured` exists to hold.
    if enforcement["measured"] and enforcement["not_enforced"]:
        return "not_enforced"
    # Below `not_enforced` for the same reason that sits below `missing`: an unhideable row is the
    # LEAST actionable of the three — the owner has to change a Plex parental profile, where a
    # missing rule fixes itself on the next run. Above `clean`, because a run that looked through a
    # profiled account's eyes and SAW other people's rows is a measured exposure, and printing
    # "every account hides every row" over the top of it is the exact defect this page exists to
    # prevent. `filters_not_enforced` was given this treatment and its sibling was missed.
    if enforcement["measured"] and enforcement["unhideable"]:
        return "unhideable"
    # `left_alone` deliberately does NOT escalate. It is the owner's own per-account choice
    # (`manage_sharing=0`), not a fault, and `test_a_left_alone_account_is_reported_as_a_setting_not_a_fault`
    # pins that. What it does mean is that the clean headline cannot claim "EVERY account" — the copy
    # is the fix on that half, not the verdict, so `sharing.tsx` narrows the sentence when any account
    # is left alone.
    return "clean"


def _enforcement(session) -> dict:
    """The newest run that MEASURED whether Plex is applying our excludes — not the newest run.

    An errored run carries no measurement, so reading it would report "nothing exposed" for a night
    nobody looked. The presence of the `filters_not_enforced` key is the measured flag: run
    persistence writes it on every run that got as far as looking, empty included, and that empty
    dict is what lets a fixed server clear the alert. Same read as
    `notifications._filters_not_enforced`, which pins this shape for the notification card.
    """
    run = next(
        (
            r
            for r in session.query(Run)
            .filter(Run.finished_at.isnot(None))
            .order_by(Run.finished_at.desc())
            .limit(_ENFORCEMENT_RUN_LOOKBACK)
            if "filters_not_enforced" in (r.stats or {})
        ),
        None,
    )
    if run is None:
        return {
            "measured": False,
            "run_id": None,
            "measured_at": None,
            "not_enforced": {},
            "unhideable": {},
        }
    exposed = (run.stats or {}).get("filters_not_enforced") or {}
    # Its sibling, written by `pipeline._record_unhideable` on the same run, from the same look: a
    # row Plex refuses to hide from an account at all — a parental profile, where plex.tv rejects the
    # filter write outright. Read here rather than left to the accounts table, because a summary that
    # ignores it prints a green universal claim over a measured exposure.
    unhideable = (run.stats or {}).get("unhideable_rows") or {}
    return {
        "measured": True,
        "run_id": run.id,
        "measured_at": iso_utc(run.finished_at),
        "not_enforced": {name: list(keys) for name, keys in exposed.items()},
        "unhideable": {name: list(keys) for name, keys in unhideable.items()},
    }
