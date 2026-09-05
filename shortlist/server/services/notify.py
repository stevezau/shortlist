"""The pipe out: send what the notification bell already decided to say, somewhere the owner will see it.

Shortlist has never been short of things to tell its owner — `shortlist/server/notifications.py` is a
registry of builders, each answering "is this true right now" and returning
`{id, severity, title, body, action_url, action_label, dismissable}`. What it lacked was anywhere for
that to land but an in-app bell, on a tool whose whole job happens unattended at 3am. This module is
that pipe. It is deliberately NOT a second opinion about what is worth saying: every external message
is a notification dict the registry produced, so the webhook and the bell can never disagree about the
wording.

**v1 carries one event — a whole run failing** — because that is the gap: a run that dies at 03:31
is invisible until somebody happens to open the app. Everything else the registry raises is either
visible at the owner's leisure (an update is available) or a workflow item (a request needs approval),
and paging for those is how an owner learns to ignore the pager.

Three constraints, all load-bearing:

* **The URL is a credential.** A Discord or Slack webhook address is a bearer token in a URL, so it
  lives in `SECRET_KEYS` (Fernet at rest, `•••••` on read) and every string derived from a send
  failure goes through `scrub` — see its docstring for why `http_retry.redact` alone is not enough.
* **The queue is the retry.** `services/jobs.py` already has backoff, a `failed` terminal state and
  dead-letter visibility. A webhook that stays down long enough lands its job on `failed`, which fires
  the EXISTING `_failed_jobs` alert in the bell — the one destination that cannot be broken by the
  thing that is broken. That falls out of reusing the queue; there is nothing here to build for it.
* **No external message may name an account.** The registry's highest-priority alerts describe who can
  see whose row. Handing that to a third-party webhook would make a privacy incident MORE exposed, not
  less. v1 sends nothing of the kind; anything added later says "N accounts can see rows that aren't
  theirs — open Shortlist to see who" and keeps the names in the app.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
from loguru import logger

from shortlist.engine.clients.http_retry import redact
from shortlist.server import notifications
from shortlist.server.db.models import Run
from shortlist.server.services import jobs
from shortlist.server.settings_store import SettingsStore

#: Long enough for a chat provider having a slow moment, short enough that a black-holed address does
#: not hold a job worker for a minute. A timeout is a failure like any other — the queue retries it.
TIMEOUT_S = 15.0

#: Everything after a URL's authority is where a webhook token lives. All three separators, not just
#: `/`: Discord and Slack put the token in the PATH, but a generic endpoint is just as likely to want
#: `?token=…`, and a pattern anchored on `/` alone would hand that one straight through.
_URL_TAIL = re.compile(r"(https?://[^/\s?#]+)[/?#]\S*")


class NotifyError(Exception):
    """Base for the two ways a send does not happen. Both carry text safe to log and to show."""


class NotifyNotConfigured(NotifyError):
    """Nothing was sent because there is nothing to send to. Not a failure — a setting."""


class NotifyFailed(NotifyError):
    """The webhook was called and did not accept it. The message is already scrubbed."""


def scrub(text: str) -> str:
    """Strip the webhook token, then every other credential shape, from text bound for a log or a row.

    `http_retry.redact` is not enough on its own, and that is measured rather than assumed
    (`test_redact_alone_does_not_strip_a_webhook_url`): it knows bearer headers, `user:pass@host`,
    labelled `token`/`apikey` pairs and provider key shapes, and a webhook URL is none of those. httpx
    puts the full request URL into the message of a `ConnectError` and of an `HTTPStatusError`, so an
    unscrubbed exception would write the owner's token into `Job.error` — and from there into the job
    list, the audit event and the support bundle (plex-safety rule 9).

    The URL's PATH goes and its host stays: the host says which service refused, and the owner
    configured it. Stripping the tail rather than matching the stored value on the nose also survives
    whatever normalisation httpx applied on the way to the error message.

    Args:
        text: Any string derived from a send failure.

    Returns:
        The same text with webhook tokens and every other known credential shape removed.
    """
    return redact(_URL_TAIL.sub(r"\1/<redacted>", text))


def test_item() -> dict:
    """The message the Settings test button sends.

    A notification dict of the same shape the registry builds, so it travels the identical
    `deliver` → `webhook_body` → HTTP path a 3am failure does. A test button with its own send path is
    worse than none: it is a button that teaches the owner to trust something it never exercised.

    `info`, not `error`, so a receiver that routes errors to a pager is not paged by a button press.
    """
    return {
        "id": "notify-test",
        "severity": "info",
        "title": "Shortlist test message",
        "body": (
            "This is the test from Shortlist's notification settings. A real alert — a run that failed "
            "overnight — arrives here exactly the same way."
        ),
        "action_url": "/settings#notifications",
    }


def webhook_body(item: dict, *, now: datetime | None = None) -> dict:
    """The generic JSON POSTed to the webhook.

    Generic on purpose: this is the shape Sonarr, Radarr and Overseerr owners already know how to
    point at Discord, Slack, Home Assistant or n8n, and one flat object is easier to write a routing
    rule against than a vendor's envelope.

    `path` is a path, not a URL, and is named so. Shortlist runs behind whatever reverse proxy,
    hostname and port its owner chose and is never told what they are, so a field called `url` here
    would be a guess presented as a fact.

    Args:
        item: A notification dict from `shortlist/server/notifications.py`.
        now: Overridable for tests; defaults to the moment of the call.

    Returns:
        A JSON-serialisable dict. `id` is stable per occurrence, so a receiver can dedupe on it.
    """
    return {
        "source": "shortlist",
        "version": 1,
        "id": item["id"],
        "severity": item["severity"],
        "title": item["title"],
        "message": item["body"],
        "path": item.get("action_url", ""),
        "sent_at": (now or datetime.now(UTC)).isoformat(),
    }


def deliver(store: SettingsStore, item: dict) -> str:
    """Send one notification to the configured webhook. The single sender; everything reaches here.

    Args:
        store: A store that can decrypt secrets — the webhook address is one.
        item: A notification dict from the registry (or `test_item`).

    Returns:
        A plain-English line for the operator who pressed Test.

    Raises:
        NotifyNotConfigured: Notifications are off, or no address is saved.
        NotifyFailed: The webhook was reached and refused, or could not be reached. Already scrubbed.
    """
    if not store.get("notify.webhook.enabled"):
        raise NotifyNotConfigured("Notifications are switched off. Turn them on to send anything.")
    url = str(store.get("notify.webhook.url") or "").strip()
    if not url:
        raise NotifyNotConfigured("No webhook address is saved yet — paste the one your chat app gave you.")
    try:
        # A bare `httpx.post`, not `http_retry.post`, and deliberately: the job queue is already
        # retrying this over ~4.6 hours. Layering the client's three attempts underneath would turn
        # that into three times the requests at a service that is plainly having a bad day.
        #
        # `follow_redirects=False` is httpx's default, and is stated rather than inherited: the URL is
        # owner-supplied and checked against `net_guard` at the settings boundary, and a followed
        # redirect is how a checked address reaches an unchecked one.
        response = httpx.post(url, json=webhook_body(item), timeout=TIMEOUT_S, follow_redirects=False)
        response.raise_for_status()
    except Exception as e:
        # `from None`, not `from e`: a chained cause is rendered in full by `logger.exception`, which
        # would put the unscrubbed URL back into the log this scrub exists to keep it out of.
        raise NotifyFailed(scrub(f"{type(e).__name__}: {e}")) from None
    return f"Sent — your webhook answered {response.status_code}."


def enqueue_run_failure(sessions, run_id: int) -> int | None:
    """Queue the external alert for a run that failed outright. Returns the job id, or None.

    Enqueue only. The queue is the retry, and a run drains it on its way out
    (`RunService._drain_jobs_after_run` is in a `finally`, so a failed run drains too), which is why
    this needs no scheduler of its own and still leaves within seconds of the failure.

    Never raises. The caller is the run's own error path — the one place already handling a failure —
    and an exception here would replace the run's real error with a notification's.

    Args:
        sessions: The session factory.
        run_id: The run that has just been finalised.

    Returns:
        The queued job's id, or None when there is nothing to send (a healthy run, a dry run, or
        notifications switched off).
    """
    try:
        with sessions() as session:
            run = session.get(Run, run_id)
            if run is None or run.status != "error":
                return None
            # A dry run is a preview somebody is sitting and watching. This alert exists for the
            # failure nobody saw.
            if run.dry_run:
                return None
            # No SecretBox needed, and none wanted: the switch decides whether to queue, and only the
            # handler that actually sends has any business decrypting the address.
            if not SettingsStore(session).get("notify.webhook.enabled"):
                return None
            item = notifications.run_failed_alert(run)
        return jobs.enqueue(sessions, "notify.send", {"item": item}, max_attempts=jobs.NOTIFY_ATTEMPTS)
    except Exception as e:
        logger.warning(
            "run {}: could not queue the failure alert ({}: {}) — the run is unaffected",
            run_id,
            type(e).__name__,
            scrub(str(e)),
        )
        return None
