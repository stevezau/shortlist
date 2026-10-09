"""The pipe out: send what the notification bell already decided to say, somewhere the owner will see it.

Shortlist has never been short of things to tell its owner — `shortlist/server/notifications.py` is a
registry of builders, each answering "is this true right now" and returning
`{id, severity, title, body, action_url, action_label, dismissable}`. What it lacked was anywhere for
that to land but an in-app bell, on a tool whose whole job happens unattended at 3am. This module is
that pipe. It is deliberately NOT a second opinion about what is worth saying: every external message
is a notification dict the registry produced, so the webhook and the bell can never disagree about the
wording.

**The owner picks the events** (`EVENTS`, `notify.webhook.events`). The default is the two an owner
cannot see before their next login — a whole run failing, and a live privacy exposure — so a notifier
nobody asked to be chatty is not. The rules under each event are what make the rest usable: a dry run is
a preview somebody is watching, routine jobs start and finish every few minutes, and this sender never
reports on itself (see `.claude/docs/notifications-design.md`, v1.1).

Three constraints, all load-bearing:

* **The URL is a credential**, and so is the optional auth header's value. A Discord or Slack webhook
  address is a bearer token in a URL, so both live in `SECRET_KEYS` (Fernet at rest, `•••••` on read)
  and every string derived from a send failure goes through `scrub` — see its docstring for why
  `http_retry.redact` alone is not enough.
* **The queue is the retry.** `services/jobs.py` already has backoff, a `failed` terminal state and
  dead-letter visibility. A webhook that stays down long enough lands its job on `failed`, which fires
  the EXISTING `_failed_jobs` alert in the bell — the one destination that cannot be broken by the
  thing that is broken. That falls out of reusing the queue; there is nothing here to build for it.
* **No external message may name an account.** The registry's highest-priority alerts describe who can
  see whose row. Handing that to a third-party webhook would make a privacy incident MORE exposed, not
  less, so `privacy.exposure` says "N accounts can see rows that aren't theirs" and keeps the names in
  the app, and a job's message never carries its detail or error, which can name a person.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
from loguru import logger
from sqlalchemy.orm import Session

from shortlist.engine.clients.http_retry import redact
from shortlist.server import notifications
from shortlist.server.db.models import Job, RequestCandidate, Run
from shortlist.server.services import jobs
from shortlist.server.settings_store import SettingsStore
from shortlist.server.version_check import check_for_update

#: Every event the owner can switch on, in the order the settings card lists them.
EVENTS: tuple[str, ...] = (
    "run.started",
    "run.finished",
    "run.partial",
    "run.failed",
    "run.stopped",
    "job.started",
    "job.finished",
    "job.failed",
    "job.skipped",
    "privacy.exposure",
    "requests.waiting",
    "update.available",
)

#: A standing privacy exposure is repeated no more often than this while it stays true. A day, less an hour:
#: the stamp is when a nightly run ENDS, and a night that finishes a few minutes sooner would otherwise
#: skip to the one after.
PRIVACY_REPEAT = timedelta(hours=23)

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


def scrub(text: str, *values: str) -> str:
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

    The auth header's value is removed by exact match, because a bearer token or an API key has no
    shape that `redact` could be relied on to recognise.

    Args:
        text: Any string derived from a send failure.
        *values: Known secret strings to remove as well, such as the auth header's value.

    Returns:
        The same text with webhook tokens and every other known credential shape removed.
    """
    # The URL first: replacing a value that happens to occur in `https://` would break the pattern that
    # finds the token.
    text = _URL_TAIL.sub(r"\1/<redacted>", text)
    for value in values:
        if value:
            # Also as h11 quotes a header value it refuses: bytes repr, so a tab arrives as `\\t`.
            escaped = repr(value.encode("ascii", "backslashreplace"))[2:-1]
            text = text.replace(value, "<redacted>").replace(escaped, "<redacted>")
    return redact(text)


def sample_item() -> dict:
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
        "event": "test",
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
    # Discord refuses a body with no `content` and Slack one with no `text` (both documented
    # requirements), so the line a chat app shows is sent under both names. Other receivers ignore them.
    chat_line = f"{item['title']}\n{item['body']}"
    return {
        "source": "shortlist",
        "version": 1,
        "id": item["id"],
        "severity": item["severity"],
        "title": item["title"],
        "message": item["body"],
        "event": item.get("event", ""),
        "path": item.get("action_url", ""),
        "sent_at": (now or datetime.now(UTC)).isoformat(),
        "content": chat_line,
        "text": chat_line,
    }


def deliver(store: SettingsStore, item: dict) -> str:
    """Send one notification to the configured webhook. The single sender; everything reaches here.

    Args:
        store: A store that can decrypt secrets — the webhook address is one.
        item: A notification dict from the registry (or `sample_item`).

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
    auth_value = str(store.get("notify.webhook.auth_header_value") or "")
    # Both or nothing: a blank name is how the owner stops sending a saved value (Settings → Connections).
    auth_name = str(store.get("notify.webhook.auth_header_name") or "").strip()
    headers = {auth_name: auth_value} if auth_name and auth_value else {}
    try:
        # A bare `httpx.post`, not `http_retry.post`, and deliberately: the job queue is already
        # retrying this over ~4.6 hours. Layering the client's three attempts underneath would turn
        # that into three times the requests at a service that is plainly having a bad day.
        #
        # `follow_redirects=False` is httpx's default, and is stated rather than inherited: the URL is
        # owner-supplied and checked against `net_guard` at the settings boundary, and a followed
        # redirect is how a checked address reaches an unchecked one.
        response = httpx.post(url, json=webhook_body(item), headers=headers, timeout=TIMEOUT_S, follow_redirects=False)
        response.raise_for_status()
    except Exception as e:
        # `from None`, not `from e`: a chained cause is rendered in full by `logger.exception`, which
        # would put the unscrubbed URL back into the log this scrub exists to keep it out of.
        raise NotifyFailed(scrub(f"{type(e).__name__}: {e}", auth_value)) from None
    return f"Sent — your webhook answered {response.status_code}."


def _wanted(session: Session, event: str) -> bool:
    """Is the webhook on, and is this event one the owner switched on?"""
    store = SettingsStore(session)
    return bool(store.get("notify.webhook.enabled")) and event in (store.get("notify.webhook.events") or [])


def _queue(sessions, event: str, item: dict) -> int:
    """Put one message on the queue, tagged with its event. The queue is the retry."""
    return jobs.enqueue(sessions, "notify.send", {"item": {**item, "event": event}}, max_attempts=jobs.NOTIFY_ATTEMPTS)


def _could_not_queue(what: str, error: Exception) -> None:
    logger.warning(
        "could not queue the {} alert ({}: {}) — nothing else is affected",
        what,
        type(error).__name__,
        scrub(str(error)),
    )


def enqueue_run_started(sessions, run_id: int) -> int | None:
    """Queue `run.started` for a real run that has just begun. Returns the job id, or None.

    Never raises: the caller is the run itself, and telling the owner must not change what it does.

    Args:
        sessions: The session factory.
        run_id: The run that has just started.

    Returns:
        The queued job's id, or None when there is nothing to send.
    """
    try:
        with sessions() as session:
            run = session.get(Run, run_id)
            if run is None or run.dry_run or not _wanted(session, "run.started"):
                return None
            item = notifications.run_started_alert(run)
        return _queue(sessions, "run.started", item)
    except Exception as e:
        _could_not_queue(f"run {run_id} start", e)
        return None


def enqueue_run_outcome(sessions, run_id: int) -> int | None:
    """Queue the one event that says how a run ended. Returns the job id, or None.

    `ok` is `run.finished`, or `run.partial` when anybody failed; `error` is `run.failed`; `aborted` is
    `run.stopped`. A dry run sends nothing — it is a preview somebody is sitting and watching.

    Enqueue only. The queue is the retry, and a run drains it on its way out
    (`RunService._drain_jobs_after_run` is in a `finally`, so a failed run drains too), which is why
    this needs no scheduler of its own and still leaves within seconds of the run ending.

    Never raises. The caller is the run's own finishing path — for a failure, the one place already
    handling it — and an exception here would replace the run's real outcome with a notification's.

    Args:
        sessions: The session factory.
        run_id: The run that has just been finalised.

    Returns:
        The queued job's id, or None when there is nothing to send.
    """
    try:
        with sessions() as session:
            run = session.get(Run, run_id)
            if run is None or run.dry_run:
                return None
            if run.status == "ok":
                partial = bool((run.stats or {}).get("users_error"))
                event = "run.partial" if partial else "run.finished"
                build = notifications.run_partial_alert if partial else notifications.run_finished_alert
            elif run.status == "error":
                event, build = "run.failed", notifications.run_failed_alert
            elif run.status == "aborted" and run.began_at is not None:
                # `began_at` is stamped when the engine starts: a run cancelled or orphaned while still
                # queued never started, so there is nothing to say it stopped.
                event, build = "run.stopped", notifications.run_stopped_alert
            else:
                return None
            if not _wanted(session, event):
                return None
            item = build(run)
        return _queue(sessions, event, item)
    except Exception as e:
        _could_not_queue(f"run {run_id} outcome", e)
        return None


def enqueue_job_event(sessions, job_id: int, event: str) -> int | None:
    """Queue `job.started`, `job.finished` or `job.failed` for one job. Returns the job id, or None.

    Some jobs never report: `notify.send` itself (a webhook that is down would otherwise queue an
    alert about failing to send an alert, for ever), and a dry run. A `routine` kind reports only a
    failure, a retry does not start again, and the 30-minute privacy sync reports a start only when
    someone pressed the button and a finish only when it was not quiet.

    Never raises: the caller is the job queue, and one alert must not stop the jobs behind it.

    Args:
        sessions: The session factory.
        job_id: The job.
        event: One of the three job events.

    Returns:
        The queued job's id, or None when there is nothing to send.
    """
    try:
        with sessions() as session:
            job = session.get(Job, job_id)
            if job is None or job.kind == "notify.send" or (job.payload or {}).get("dry_run"):
                return None
            if event != "job.failed" and job.kind in jobs.routine_kinds():
                return None
            if event == "job.started" and job.attempts > 1:
                return None  # a retry, not a new job
            # Not a `routine` kind, but it runs every 30 minutes: the rule the Jobs page's Recent list uses.
            # A scheduled pass beginning is never news, and one that finishes having changed nothing is quiet.
            if job.kind == "privacy.sync" and (
                (event == "job.started" and (job.payload or {}).get("scheduled"))
                or (event == "job.finished" and (job.result or {}).get("quiet"))
            ):
                return None
            if not _wanted(session, event):
                return None
            entry = jobs.BY_KIND.get(job.kind)
            item = notifications.job_alert(event, job.id, entry.label if entry else job.kind)
        return _queue(sessions, event, item)
    except Exception as e:
        _could_not_queue(f"job {job_id} {event}", e)
        return None


def enqueue_job_skipped(sessions, job_key: str, label: str, scheduled_for: str) -> int | None:
    """Queue `job.skipped` for a scheduled job APScheduler dropped for starting too late.

    Never raises: the caller is the scheduler's listener, which must not be disturbed by an alert.

    Args:
        sessions: The session factory.
        job_key: The scheduler's id for the job; names the alert so each skip is its own message.
        label: What to call the job in the message.
        scheduled_for: When it was due, ISO 8601.

    Returns:
        The queued job's id, or None when there is nothing to send.
    """
    try:
        with sessions() as session:
            if not _wanted(session, "job.skipped"):
                return None
        return _queue(sessions, "job.skipped", notifications.job_skipped_alert(job_key, label, scheduled_for))
    except Exception as e:
        _could_not_queue(f"job {job_key} skipped", e)
        return None


def after_run(sessions, run_id: int, current_version: str) -> None:
    """The checks every finished real run makes: a privacy exposure, requests waiting, a newer release.

    Blocking — the update check can reach GitHub — so the run calls this from an executor. Each check
    is independent and none raises: a failure in one is logged and the others still run.

    Args:
        sessions: The session factory.
        run_id: The run that has just been finalised.
        current_version: The running build's version.
    """
    try:
        with sessions() as session:
            run = session.get(Run, run_id)
            if run is None or run.dry_run:
                return
    except Exception as e:
        _could_not_queue(f"run {run_id} follow-up", e)
        return
    for check in (_check_privacy, _check_requests, _check_update):
        try:
            check(sessions, current_version)
        except Exception as e:
            _could_not_queue(check.__name__.removeprefix("_check_"), e)


def _check_privacy(sessions, _current_version: str) -> None:
    """`privacy.exposure`, at most once per `PRIVACY_REPEAT` while it stays true.

    When the exposure clears, the clock resets, so a new one is sent at once rather than waiting out
    the day the previous one started.
    """
    now = datetime.now(UTC)
    with sessions() as session:
        store = SettingsStore(session)
        accounts = len(notifications.exposed_accounts(session))
        sent_at = str(store.get("notify.webhook.privacy_sent_at") or "")
        if not accounts:
            if sent_at:
                store.set("notify.webhook.privacy_sent_at", "")
            return
        if not _wanted(session, "privacy.exposure"):
            return
        if sent_at and now - datetime.fromisoformat(sent_at) < PRIVACY_REPEAT:
            return
        item = notifications.privacy_exposure_alert(accounts)
    _queue(sessions, "privacy.exposure", item)
    with sessions() as session:
        SettingsStore(session).set("notify.webhook.privacy_sent_at", now.isoformat())


def _check_requests(sessions, _current_version: str) -> None:
    """`requests.waiting`, when more titles wait than the last time this looked.

    The count is recorded on every look, falls included, so "new" always means new since then.
    """
    with sessions() as session:
        store = SettingsStore(session)
        waiting = session.query(RequestCandidate).filter(RequestCandidate.status == "pending").count()
        seen = int(store.get("notify.webhook.requests_seen") or 0)
        if waiting != seen:
            store.set("notify.webhook.requests_seen", waiting)
        if waiting <= seen or not _wanted(session, "requests.waiting"):
            return
        item = notifications.requests_waiting_alert(waiting, waiting - seen)
    _queue(sessions, "requests.waiting", item)


def _check_update(sessions, current_version: str) -> None:
    """`update.available`, once per newer version."""
    with sessions() as session:
        if not _wanted(session, "update.available"):
            return
        store = SettingsStore(session)
        update = check_for_update(current_version)
        if not update or store.get("notify.webhook.update_sent") == update["latest"]:
            return
        item = notifications.update_alert(current_version, update["latest"], update["url"])
    _queue(sessions, "update.available", item)
    with sessions() as session:
        SettingsStore(session).set("notify.webhook.update_sent", update["latest"])
