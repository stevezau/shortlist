"""What Shortlist wants to tell the owner: update available, a failed run, paused runs, service errors.

A registry of small builder functions, each returning a notification dict (or nothing when its
condition isn't firing). Notifications reflect CURRENT state and are recomputed on every request, so
most clear themselves the moment the underlying condition resolves (a good run, an un-pause).

Everything here is dismissable EXCEPT the two alerts that describe a condition still true right now —
runs paused, and an account that can see other people's rows — where hiding the alert hides the thing
itself. Every dismissable id encodes its state (a version, a run id, the newest failed job, the newest
error), so dismissing acknowledges what has happened so far and the next occurrence surfaces again
rather than staying hidden behind the old dismissal.

Shape (rendered by the React bell, so the fields are plain text — no HTML, no sanitiser needed):
    {id, severity: info|warning|error, title, body, action_url, action_label, dismissable}
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from shortlist.server.db.models import Event, Run
from shortlist.server.settings_store import SettingsStore
from shortlist.server.version_check import check_for_update

DISMISSED_KEY = "notifications.dismissed"  # list of dismissed notification ids (each id encodes its state)


def _update_available(store: SettingsStore, current_version: str) -> dict | None:
    update = check_for_update(current_version)
    if not update:
        return None
    return {
        "id": f"update-{update['latest']}",
        "severity": "info",
        "title": "Update available",
        "body": f"v{current_version} → v{update['latest']}",
        "action_url": update["url"],
        "action_label": "View release",
        "dismissable": True,
    }


def _runs_paused(store: SettingsStore) -> dict | None:
    if not store.get("paused_all"):
        return None
    return {
        "id": "runs-paused",
        "severity": "warning",
        "title": "Runs are paused",
        "body": "Scheduled and manual runs are paused, so no rows are being rebuilt. Resume in Settings.",
        "action_url": "/settings",
        "action_label": "Settings",
        "dismissable": False,
    }


def _last_run_problem(session: Session) -> dict | None:
    last = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
    if last is None:
        return None
    if last.status == "error":
        # A whole-run failure is usually a service being down (Plex/plex.tv unreachable, PMS too old).
        return {
            "id": f"run-failed-{last.id}",
            "severity": "error",
            "title": "The last run failed",
            "body": "The most recent run ended in an error — open it to see what went wrong.",
            "action_url": f"/runs/{last.id}",
            "action_label": "See the run",
            "dismissable": True,  # id is per-run, so a NEW failed run re-surfaces
        }
    failed = (last.stats or {}).get("users_error", 0)
    if failed:
        return {
            "id": f"run-partial-{last.id}",
            "severity": "warning",
            "title": f"{failed} {'person' if failed == 1 else 'people'} failed in the last run",
            "body": "Some people didn't rebuild in the most recent run. The rest finished fine.",
            "action_url": f"/runs/{last.id}",
            "action_label": "See the run",
            "dismissable": True,
        }
    return None


def _recent_service_errors(session: Session) -> dict | None:
    """A count of service-level error events in the last day that AREN'T already covered by a failed
    run — e.g. a plex.tv write that 429'd repeatedly, or a request send that errored.

    Dismissable, and the id encodes the NEWEST error, so dismissing acknowledges everything up to
    that point and the next error re-surfaces it.

    It used to be neither: a stable id and `dismissable: False`, which left the bell showing a badge
    for a full day over errors that had already happened, with nothing the owner could do but wait
    for them to age out. That is what dismissing is FOR. The two alerts that stay undismissable are
    the two that describe a condition still true right now — runs paused, and an account that can see
    other people's rows — where hiding it hides the thing itself. A count of what already happened is
    not one of those.

    Keyed to the newest event id rather than to the day: a second error the same afternoon must not
    stay hidden behind the morning's dismissal, and the COUNT is unusable as a key because it falls
    as old events age out of the window, which would re-surface an alert nothing new had happened to.
    """
    since = datetime.now(UTC) - timedelta(days=1)
    recent = session.query(Event).filter(Event.level == "error", Event.ts >= since, ~Event.scope.startswith("run"))
    count = recent.count()
    if not count:
        return None
    newest = recent.order_by(Event.id.desc()).first()
    if newest is None:
        # `count` and this are two separate queries, so the nightly retention prune landing between
        # them gives a non-zero count with nothing behind it — and `recent-errors-0` would be a STABLE
        # dismissable id, the one combination the state-encoded id exists to avoid. Dismissing it once
        # would silence the alert for good.
        return None
    return {
        "id": f"recent-errors-{newest.id}",
        "severity": "warning",
        "title": f"{count} error{'s' if count != 1 else ''} in the last day",
        "body": "Shortlist logged some errors recently. Check the recent runs and the container log.",
        "action_url": "/runs",
        "action_label": "See runs",
        "dismissable": True,
    }


def _mdblist_quota(session: Session) -> dict | None:
    """MDBList hit its daily request cap in a recent run, so some ratings fell back to TMDB. The id
    encodes the day so a fresh hit re-surfaces after dismissal, but the same day's stays dismissed."""
    since = datetime.now(UTC) - timedelta(days=1)
    event = (
        session.query(Event)
        .filter(Event.scope == "requests.rate_limited", Event.ts >= since)
        .order_by(Event.ts.desc())
        .first()
    )
    if event is None:
        return None
    return {
        "id": f"mdblist-quota-{event.ts.date().isoformat()}",
        "severity": "warning",
        "title": "MDBList daily limit reached",
        "body": (
            "A recent run used up your MDBList request quota, so some titles were rated from TMDB "
            "instead of your chosen source. It resets daily — or raise your MDBList plan for more."
        ),
        "action_url": "/settings#requests",
        "action_label": "Requests settings",
        "dismissable": True,
    }


def _owner_sees_all_rows(session: Session) -> dict | None:
    """The owner has per-person rows on a library's Recommended shelf, so their own shelf shows
    everyone's row — Plex hides rows through each person's SHARE, and the owner has no share.

    This is the single most-asked support question, and the copy explaining it already exists in
    three places (the row editor's placement grid, the Users page's owner note, the wizard). All
    three are passive: the owner reads them while setting up, then meets the actual problem days
    later in Plex. This fires when the condition becomes TRUE, which is the moment it can be acted
    on, and points at the guide that offers a way out rather than restating the limitation.

    Dismissable, because "I don't mind seeing them" is a legitimate answer — Ssvvois's, in fact.
    The id is stable (not state-encoded) so dismissing it means dismissing it for good.
    """
    from shortlist.server.db.models import Collection, User

    # NOT gated on the owner being enabled. An owner who turned their OWN row off still sees every
    # friend's row on the library shelf — they own the server, so nothing hides them — and gating on
    # `enabled` silenced the notification for exactly the person most likely to be surprised by it.
    owner = session.query(User).filter(User.user_type == "owner").first()
    if owner is None:
        return None
    others = session.query(func.count(User.id)).filter(User.user_type != "owner", User.enabled.is_(True)).scalar() or 0
    if others < 1:
        return None
    # Only a per_person row stacks one collection per person onto the shelf. A shared row is ONE
    # collection everybody sees on purpose, so it is not this problem.
    on_shelf = (
        session.query(func.count(Collection.id))
        .filter(
            Collection.enabled.is_(True),
            Collection.build == "per_person",
            Collection.placement_friends.in_(("both", "library")),
        )
        .scalar()
        or 0
    )
    if not on_shelf:
        return None
    return {
        "id": "owner-sees-all-rows",
        "severity": "info",
        "title": "You see everyone's rows in your libraries",
        # Deliberately NO number. The true count is rows-on-the-shelf x their resolved audience, and
        # audience="subset" plus per-user `CollectionUserOverride` mutes make that neither `others`
        # nor `others + 1`. A confident wrong number in a notification is worse than no number: the
        # page it links to counts properly from the roster it has loaded.
        "body": (
            "You own this server, so the Recommended shelf inside each library shows you everyone's "
            "row, not just yours — Plex hides rows through each person's share, and you don't have "
            "one. There are three ways to deal with it."
        ),
        "action_url": "/watching-account",
        "action_label": "See the options",
        "dismissable": True,
    }


def _failed_jobs(session: Session) -> dict | None:
    """Background jobs that ran out of retries.

    Without this the retry machinery is invisible exactly when it matters. A job only reaches
    `failed` after exhausting every attempt, and these are the destructive/privacy-relevant ones —
    removing a disabled user's rows, hiding a paused user's, writing share filters. A silent failure
    there means Plex is left in a state the operator believes was corrected.

    The id encodes the newest failed job id, so a NEW failure re-surfaces after a dismissal rather
    than staying hidden behind the old one.
    """
    from shortlist.server.db.models import Job

    failed = session.query(Job).filter(Job.status == "failed").order_by(Job.id.desc()).all()
    if not failed:
        return None
    kinds = sorted({job.kind for job in failed})
    return {
        "id": f"failed-jobs-{failed[0].id}",
        "severity": "error",
        "title": f"{len(failed)} background job{'s' if len(failed) != 1 else ''} failed",
        "body": (
            f"Shortlist gave up on {', '.join(kinds)} after retrying. Plex may not reflect what you "
            "asked for — open Jobs to see the error and run it again."
        ),
        "action_url": "/jobs",
        "action_label": "See jobs",
        "dismissable": True,
    }


def _rows_we_cannot_hide(session: Session) -> dict | None:
    """An account exists that Plex refuses a hide-list for, and it can see other people's rows.

    Plex declines label restrictions on a managed account while a parental Restriction Profile is set.
    Shortlist skipped those accounts on the assumption that they see no collections anyway — true of
    `little_kid`, false of `older_kid` (measured on a real server, 2026-08-11). So for those accounts
    nothing hid other people's rows and nothing said so.

    Nothing in Shortlist can fix it: changing someone's parental profile is not ours to do, and there
    is no other way to hide one collection from one account. The owner has exactly ONE remedy —
    clearing the Restriction Profile — and this says so. Disabling the account is deliberately NOT
    offered: it removes that person's own row, while the exposure is their view of everyone else's,
    which needs the very filter Plex is refusing. NOT dismissable while it is true — it is a live
    privacy exposure, not a preference.
    """
    from shortlist.server.db.models import Run

    # The latest run that actually RECORDED a measurement — not merely the latest that finished. A run
    # that failed early, was aborted, or never reached the privacy phase carries no `unhideable_rows`
    # key at all; treating that as "{}" would let one bad run clear a real finding from the alert while
    # the exposure is untouched. That silence is the thing this whole check exists to end.
    run = next(
        (
            r
            for r in session.query(Run).filter(Run.finished_at.isnot(None)).order_by(Run.finished_at.desc()).limit(50)
            if "unhideable_rows" in (r.stats or {})
        ),
        None,
    )
    exposed = ((run.stats or {}).get("unhideable_rows") or {}) if run else {}
    if not exposed:
        return None

    # One paragraph, no markup: the bell renders `body` as a single unformatted <p>. So the order has
    # to carry the meaning — who, how much, why nobody here can fix it, what the owner does instead.
    names = sorted(exposed)
    if len(names) == 1:
        who = names[0]
        count = len(exposed[who])
        title = f"{who} can see other people's rows"
        lead = f"{who} can see {count} {'row that belongs' if count == 1 else 'rows that belong'} to other people."
        fix = who
    else:
        who = f"{', '.join(names[:-1])} and {names[-1]}"
        title = f"{len(names)} accounts can see other people's rows"
        lead = f"{who} can see rows that belong to other people."
        fix = "those accounts"
    return {
        "id": f"unhideable-rows-{run.id}",
        "severity": "error",
        "title": title,
        "body": (
            f"{lead} Plex won't let Shortlist hide anything from an account with a parental profile "
            f"set, so this can't be fixed from here. Clear the Restriction Profile for {fix} in Plex "
            "(Settings → Users & Sharing) and the normal privacy filter starts applying again."
        ),
        "action_url": "/users",
        "action_label": "Open Users",
        "dismissable": False,
    }


#: How many times one row must be put back, inside `_CONTENTION_WINDOW`, before we call it a fight.
#: A settled shelf re-orders NOTHING — an ordering pass returns "already in place" and writes no event
#: at all — so any repeat is already abnormal. Three is chosen to clear the one legitimate way a row
#: moves more than once: it was delivered, then re-delivered under a new title (a rename, a
#: `{top_seed}` row) within the window. Nothing benign moves the same row three times in a day.
_CONTENTION_REPEATS = 3
_CONTENTION_WINDOW = timedelta(days=1)


def _shelf_contention(session: Session) -> dict | None:
    """Something OUTSIDE Shortlist is reordering the Recommended shelf, and we keep undoing each other.

    This cannot be detected from a single ordering pass, which is why it went unnoticed for weeks on
    the maintainer's server: each pass moved its rows, re-read the shelf, confirmed the new order and
    reported success. It was right. Ten minutes later another tool moved them back. `verified` answers
    "did our write land", and the answer was yes every time — the question that matters is "did it
    STAY", and only the next pass can answer it.

    So the signal is repetition: the same row needing to be put back, over and over. A settled shelf
    produces no ordering events whatsoever, and a genuine one-off (a new user's row placed for the
    first time) moves each row exactly once. A row that moves three times in a day is being moved
    against us.

    Deliberately says "something else", never names a culprit as fact: Plex does not report who moved
    a hub, so the tools listed in the body are the likely suspects, not a finding.
    """
    since = datetime.now(UTC) - _CONTENTION_WINDOW
    events = (
        session.query(Event)
        .filter(Event.scope.in_(("shelf.order", "run.hub_order")), Event.ts >= since)
        .order_by(Event.ts.desc())
        .limit(500)
        .all()
    )
    # library -> {row title -> how many separate passes had to move it}
    per_library: dict[str, dict[str, int]] = {}
    for event in events:
        message = event.message if isinstance(event.message, dict) else {}
        if message.get("dry_run"):
            continue  # a preview moved nothing, so it is no evidence of anything
        library = message.get("library") or "a library"
        moved = message.get("moved")
        if not isinstance(moved, list):
            continue
        counts = per_library.setdefault(library, {})
        for title in moved:
            counts[title] = counts.get(title, 0) + 1

    contended = {library: max(counts.values()) for library, counts in per_library.items() if counts}
    contended = {lib: n for lib, n in contended.items() if n >= _CONTENTION_REPEATS}
    if not contended:
        return None

    names = sorted(contended)
    worst = max(contended.values())
    where = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return {
        # Keyed to the day so dismissing it hides today's, and a fight still running tomorrow says so
        # again. Not keyed to the count, which changes every half hour and would re-surface constantly.
        "id": f"shelf-contention-{datetime.now(UTC).date().isoformat()}",
        "severity": "warning",
        "title": f"Something else is reordering your {where} shelf",
        "body": (
            f"Shortlist has had to put the same row back on the Recommended shelf {worst} times in the "
            f"last day in {where}, so something else is moving it. This is almost always another tool "
            "that manages Plex recommendations — Kometa, Agregarr, Plex-Meta-Manager.\n\n"
            "The fix is to tell that tool to leave Shortlist's rows alone. Every row carries the "
            "label 'shortlist', so that one word is all it needs — in Agregarr it goes in Settings → "
            "General → 'Exclude from Ordering (Plex Label)'. (That field is on the maintained fork's "
            ":develop image; it is newer than the v2.9.1 release.) Failing that, turn off 'Let "
            "Shortlist order the Recommended shelf' here so Shortlist stops competing. Your rows are "
            "built, delivered and kept private either way — only their position on the shelf is "
            "affected.\n\n"
            "If it is Agregarr, it is worth checking which one you run. The original is no longer "
            "actively released, and reordering a shelf on it re-promotes collections with Plex's "
            "defaults — which puts other people's rows on the SERVER OWNER'S Home, yours, the one "
            "place no share filter can cover. Shortlist clears that on every run, so it is a gap "
            "between runs rather than something permanent. The maintained fork at "
            "github.com/bitr8/agregarr-dev (Docker: bitr8/agregarr) fixes it at the source and is a "
            "drop-in swap. It still reorders this shelf, so the exclusion above applies either way."
        ),
        "action_url": "/settings#placement",
        "action_label": "Shelf settings",
        "dismissable": True,
    }


def build_notifications(session: Session, store: SettingsStore, current_version: str) -> list[dict]:
    """Every currently-firing notification the owner hasn't dismissed, most severe first. Dismissal is
    by id, and each dismissable id encodes its state (the run id, the version), so a NEW failure or a
    newer release surfaces again rather than staying hidden forever."""
    candidates = [
        _update_available(store, current_version),
        _runs_paused(store),
        _last_run_problem(session),
        _failed_jobs(session),
        _mdblist_quota(session),
        _recent_service_errors(session),
        _rows_we_cannot_hide(session),
        _owner_sees_all_rows(session),
        _shelf_contention(session),
    ]
    dismissed = set(store.get(DISMISSED_KEY) or [])
    order = {"error": 0, "warning": 1, "info": 2}
    # `dismissable` is enforced HERE, not just at the dismiss endpoint: a "runs are paused" alert that
    # could be silenced for good would leave the owner with a server they believe is building rows
    # nightly and isn't. Enforcing on read also re-surfaces one that some earlier call already wrote
    # into the dismissed list, which validating only on write would not.
    return sorted(
        (n for n in candidates if n and not (n["dismissable"] and n["id"] in dismissed)),
        key=lambda n: order.get(n["severity"], 3),
    )
