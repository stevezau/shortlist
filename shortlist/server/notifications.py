"""What Shortlist wants to tell the owner: update available, a failed run, paused runs, service errors.

A registry of small builder functions, each returning a notification dict (or nothing when its
condition isn't firing). Notifications reflect CURRENT state and are recomputed on every request, so
most clear themselves the moment the underlying condition resolves (a good run, an un-pause). Only
the "update available" note is dismissable, because it otherwise persists until you actually update —
its dismissal is keyed to the version, so a newer release surfaces again.

Shape (rendered by the React bell, so the fields are plain text — no HTML, no sanitiser needed):
    {id, severity: info|warning|error, title, body, action_url, action_label, dismissable}
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    run — e.g. a plex.tv write that 429'd repeatedly, or a request send that errored."""
    since = datetime.now(UTC) - timedelta(days=1)
    count = (
        session.query(Event).filter(Event.level == "error", Event.ts >= since, ~Event.scope.startswith("run")).count()
    )
    if not count:
        return None
    return {
        "id": "recent-errors",
        "severity": "warning",
        "title": f"{count} error{'s' if count != 1 else ''} in the last day",
        "body": "Shortlist logged some errors recently. Check the recent runs and the container log.",
        "action_url": "/runs",
        "action_label": "See runs",
        "dismissable": False,
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
        "action_url": "/tools",
        "action_label": "See jobs",
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
    ]
    dismissed = set(store.get(DISMISSED_KEY) or [])
    order = {"error": 0, "warning": 1, "info": 2}
    return sorted(
        (n for n in candidates if n and n["id"] not in dismissed),
        key=lambda n: order.get(n["severity"], 3),
    )
