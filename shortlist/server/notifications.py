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

import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from shortlist.engine.placeholders import names_a_seed
from shortlist.server.db.models import Event, Run
from shortlist.server.scheduler import SCHEDULE_MISSED_SCOPE
from shortlist.server.services.audit import RESTRICTION_RESTORED_SCOPE
from shortlist.server.services.watch_stream import STREAM_DOWN_ALERT_MINUTES, STREAM_DOWN_SINCE_KEY
from shortlist.server.settings_store import SettingsStore
from shortlist.server.version_check import check_for_update

DISMISSED_KEY = "notifications.dismissed"  # list of dismissed notification ids (each id encodes its state)


def _update_available(current_version: str) -> dict | None:
    update = check_for_update(current_version)
    if not update:
        return None
    return update_alert(current_version, update["latest"], update["url"])


def update_alert(current_version: str, latest: str, url: str) -> dict:
    """A newer release exists. Shared by the bell and the webhook's `update.available`.

    Args:
        current_version: The running version.
        latest: The newest published version.
        url: The release page.

    Returns:
        A notification dict.
    """
    return {
        "id": f"update-{latest}",
        "severity": "info",
        "title": "Update available",
        "body": f"v{current_version} → v{latest}",
        "action_url": url,
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


def _spell_duration(minutes: float) -> str:
    """A duration the way the design doc's voice says one: "50 minutes", "an hour", "3 days".

    The unit is chosen from the ROUNDED value, not the raw one. Choosing it from the raw value and
    then rounding independently produced "1 hours" for anything from 60 to 89 minutes, and "60
    minutes" at 59.6 — the two halves disagreeing about which unit they were in.
    """
    mins = round(minutes)
    if mins < 60:
        return f"{mins} minutes"
    hours = round(mins / 60)
    if hours < 24:
        return "an hour" if hours == 1 else f"{hours} hours"
    days = round(hours / 24)
    return "a day" if days == 1 else f"{days} days"


def _playback_listener_down(store: SettingsStore) -> dict | None:
    """The playback listener has been unable to connect for a long time.

    Worth telling the owner because the failure is SILENT in a way the other sources are not. The
    nightly play-log sweep still runs and still credits completed watches, so the dashboard keeps
    showing plausible numbers — what stops is the partial-watch signal, which only the live socket can
    see (Plex records no progress for an unfinished title). "Nobody abandoned anything this week"
    looks exactly like a healthy week.

    Not dismissable, for the same reason "runs are paused" is not: a silenced alert would leave the
    owner believing a feature is running that isn't. It clears itself the moment the socket connects.
    """
    down_since = store.get(STREAM_DOWN_SINCE_KEY)
    if not down_since:
        return None
    try:
        started = datetime.fromisoformat(str(down_since))
    except ValueError:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    minutes = (datetime.now(UTC) - started).total_seconds() / 60
    if minutes < STREAM_DOWN_ALERT_MINUTES:
        return None
    return {
        # A CONSTANT id, like `_runs_paused`. An earlier version encoded the outage's start hour so a
        # new outage would "re-surface" — but `build_notifications` only consults the dismissed list
        # for dismissable alerts, and this one is not, so the id was never compared against anything
        # and the hour did nothing at all. The docs asserted the mechanism as fact; both are fixed.
        "id": "playback-listener-down",
        "severity": "warning",
        "title": "Playback tracking is offline",
        "body": (
            f"Shortlist has not been able to watch playback for {_spell_duration(minutes)}. Finished "
            "titles are still counted from Plex's own history, but partial watches — someone starting "
            "a pick and giving up — are not being recorded while this is down. It retries on its own; "
            "check that Plex is reachable."
        ),
        "action_url": "/logs",
        "action_label": "Logs",
        "dismissable": False,
    }


def _secrets_we_cannot_read(store: SettingsStore) -> dict | None:
    """Credentials encrypted with a key this instance no longer holds — a lost `/config/secret.key`.

    Not dismissable, for the same reason "runs are paused" is not: every one of these is a credential
    the app cannot use and cannot recover, so silencing the alert leaves an owner believing a server
    is working that quietly is not. It clears itself the moment each key is re-entered.
    """
    lost = store.undecryptable_secrets()
    if not lost:
        return None
    return {
        "id": "secrets-we-cannot-read",
        "severity": "error",
        "title": "Some saved credentials can no longer be read",
        "body": (
            f"{len(lost)} saved credential(s) were encrypted with a different /config/secret.key than "
            f"the one here now, so Shortlist cannot read them: {', '.join(lost)}. This usually means "
            "secret.key was lost or the container was recreated without its /config volume. They "
            "cannot be recovered without the original file — restore it from a backup, or re-enter "
            "each one in Settings. Nothing has been overwritten, so restoring the old key still works."
        ),
        "action_url": "/settings",
        "action_label": "Settings",
        "dismissable": False,
    }


def run_failed_alert(run: Run) -> dict:
    """The alert for a run that failed outright — one wording, two destinations.

    Public because `services/notify.py` sends this same dict to the owner's webhook. Keeping one
    definition is the point: a second wording written for the external channel is how a webhook
    message and the bell start describing the same failure differently.

    Args:
        run: The failed run. Only its `id` is read.

    Returns:
        A notification dict in the registry's usual shape. It names no account, which is what makes it
        safe to send off the server (see `notify.py`).
    """
    # A whole-run failure is usually a service being down (Plex/plex.tv unreachable, PMS too old).
    return {
        "id": f"run-failed-{run.id}",
        "severity": "error",
        "title": "The last run failed",
        "body": "The most recent run ended in an error — open it to see what went wrong.",
        "action_url": f"/runs/{run.id}",
        "action_label": "See the run",
        "dismissable": True,  # id is per-run, so a NEW failed run re-surfaces
    }


def run_partial_alert(run: Run) -> dict:
    """The alert for a run that finished with some people failing. Shared by the bell and the webhook.

    Args:
        run: The run. Its `id` and `stats["users_error"]` are read.

    Returns:
        A notification dict that names no account.
    """
    failed = (run.stats or {}).get("users_error", 0)
    return {
        "id": f"run-partial-{run.id}",
        "severity": "warning",
        "title": f"{failed} {'person' if failed == 1 else 'people'} failed in the last run",
        "body": "Some people didn't rebuild in the most recent run. The rest finished fine.",
        "action_url": f"/runs/{run.id}",
        "action_label": "See the run",
        "dismissable": True,
    }


def run_started_alert(run: Run) -> dict:
    """A run began. Webhook only (`run.started`); the app shows a live run on its own.

    Args:
        run: The run. Only its `id` is read.

    Returns:
        A notification dict that names no account.
    """
    return {
        "id": f"run-started-{run.id}",
        "severity": "info",
        "title": "A run started",
        "body": "Shortlist is rebuilding rows. Open the run to follow along.",
        "action_url": f"/runs/{run.id}",
        "action_label": "See the run",
        "dismissable": True,
    }


def run_finished_alert(run: Run) -> dict:
    """A run finished with nobody failing. Webhook only (`run.finished`).

    Args:
        run: The run. Its `id` and the `users_ok`/`titles_added` counters are read.

    Returns:
        A notification dict that names no account.
    """
    stats = run.stats or {}
    people, titles = int(stats.get("users_ok") or 0), int(stats.get("titles_added") or 0)
    return {
        "id": f"run-finished-{run.id}",
        "severity": "info",
        "title": "The last run finished",
        "body": (
            f"Rebuilt rows for {people} {'person' if people == 1 else 'people'} and added "
            f"{titles} {'title' if titles == 1 else 'titles'}."
        ),
        "action_url": f"/runs/{run.id}",
        "action_label": "See the run",
        "dismissable": True,
    }


def run_stopped_alert(run: Run) -> dict:
    """A run ended before it finished: stopped by the owner, or cut short by a restart (`run.stopped`).

    Args:
        run: The run. Only its `id` is read.

    Returns:
        A notification dict that names no account.
    """
    return {
        "id": f"run-stopped-{run.id}",
        "severity": "warning",
        "title": "A run stopped before it finished",
        "body": "It was stopped, or Shortlist restarted part-way through. Open the run to see who it reached.",
        "action_url": f"/runs/{run.id}",
        "action_label": "See the run",
        "dismissable": True,
    }


def job_alert(event: str, job_id: int, label: str) -> dict:
    """A background job started, finished, or ran out of retries (`job.*`). Webhook only.

    Names the job by its catalogue label and nothing else: a job's `detail` and `error` can name a
    person ("Put 2 row(s) back for sarah"), so neither is ever part of this.

    Args:
        event: `job.started`, `job.finished` or `job.failed`.
        job_id: The job.
        label: The job kind's label from the jobs catalogue.

    Returns:
        A notification dict that names no account.
    """
    verb, severity, body = {
        "job.started": ("started", "info", "Open Jobs to follow it."),
        "job.finished": ("finished", "info", "Open Jobs to see what it did."),
        "job.failed": ("failed", "error", "It ran out of retries. Open Jobs to see why."),
    }[event]
    return {
        "id": f"{event.replace('.', '-')}-{job_id}",
        "severity": severity,
        "title": f"Job {verb}: {label}",
        "body": body,
        "action_url": "/jobs",
        "action_label": "Open Jobs",
        "dismissable": True,
    }


def job_skipped_alert(job_key: str, label: str, scheduled_for: str) -> dict:
    """A scheduled job was skipped for starting too late (`job.skipped`). Webhook only.

    Names the job by its catalogue label (or the scheduler's id for a row run) and when it was due; a
    row job's id is a cron expression, never a person.

    Args:
        job_key: The scheduler's id for the job.
        label: What to call the job.
        scheduled_for: When it was due, ISO 8601.

    Returns:
        A notification dict that names no account.
    """
    return {
        "id": f"job-skipped-{re.sub(r'[^A-Za-z0-9]+', '-', f'{job_key}-{scheduled_for}')}",
        "severity": "warning",
        "title": f"Scheduled job skipped: {label}",
        "body": f"It was due {scheduled_for} but started too late, so it was skipped. Open Jobs to run it now.",
        "action_url": "/jobs",
        "action_label": "Open Jobs",
        "dismissable": True,
    }


def _newest_run_with(session: Session, key: str) -> Run | None:
    """The newest finished run whose stats carry ``key`` — the latest run that actually MEASURED it.

    Not merely the latest that finished: a run that failed early or never reached the privacy phase
    carries no key at all, and reading its absence as "nothing found" would let one bad run clear a
    real finding while the exposure is untouched.
    """
    runs = session.query(Run).filter(Run.finished_at.isnot(None)).order_by(Run.finished_at.desc()).limit(50)
    return next((r for r in runs if key in (r.stats or {})), None)


def exposed_accounts(session: Session) -> set[str]:
    """Every account the newest measurement says can see rows that aren't theirs.

    The union of the three privacy findings a run records, each read from the newest run that measured
    it, exactly as `_rows_we_cannot_hide`, `_filters_not_enforced` and `_filters_plex_cannot_read` read
    them. For counting only: the names never leave the server.

    Args:
        session: A database session.

    Returns:
        Account names, possibly empty.
    """
    names: set[str] = set()
    for key in ("unhideable_rows", "filters_not_enforced", "unreadable_filters"):
        newest = _newest_run_with(session, key)
        if newest is not None:
            names.update((newest.stats or {}).get(key) or {})
    return names


def privacy_exposure_alert(accounts: int) -> dict:
    """Accounts can see rows that aren't theirs (`privacy.exposure`). A count, never names (design §6).

    Args:
        accounts: How many accounts are exposed.

    Returns:
        A notification dict that names no account.
    """
    return {
        "id": "privacy-exposure",
        "severity": "error",
        "title": f"{accounts} {'account' if accounts == 1 else 'accounts'} can see rows that aren't theirs",
        "body": "Open Shortlist to see who, and what to do about it.",
        "action_url": "/sharing",
        "action_label": "See sharing",
        "dismissable": False,
    }


def requests_waiting_alert(waiting: int, new: int) -> dict:
    """Titles are waiting for the owner's approval (`requests.waiting`).

    Args:
        waiting: How many wait now.
        new: How many more than the last time this was sent.

    Returns:
        A notification dict that names no account.
    """
    return {
        "id": f"requests-waiting-{waiting}",
        "severity": "info",
        "title": f"{waiting} {'title' if waiting == 1 else 'titles'} waiting for your approval",
        "body": f"{new} new since the last message. Open Requests to send or reject them.",
        "action_url": "/requests",
        "action_label": "Open Requests",
        "dismissable": True,
    }


def _last_run_problem(session: Session) -> dict | None:
    last = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
    if last is None:
        return None
    if last.status == "error":
        return run_failed_alert(last)
    if (last.stats or {}).get("users_error", 0):
        return run_partial_alert(last)
    return None


def _usable_fallback(row) -> bool:
    """A fallback name that can actually produce a title — non-blank, and not itself needing a seed."""
    value = (row.fallback_name or "").strip()
    return bool(value) and not names_a_seed(value)


def _row_display_name(name: str) -> str:
    """A row's configured name with its ``{placeholder}`` segments removed.

    The SPA's ``rowDisplayName`` (``web/src/lib/run-rows.ts``), on this side of the wire and for the
    same reason: a row is stored as a TEMPLATE, so an alert that quotes the name verbatim reads
    "Because you watched {top_seed} won't be built for…" — braces and all, in a sentence otherwise
    written for a person. Rendering the template instead is no good either; the whole point of this
    alert is that there is nobody to render it for.

    Args:
        name: The row's configured name or name template.

    Returns:
        The name with every ``{...}`` segment dropped and the whitespace it left collapsed. Empty
        when the name is nothing but placeholders, which callers must have a fallback for.
    """
    return re.sub(r"\s+", " ", re.sub(r"\{[^}]*\}", " ", name)).strip()


def _rows_with_no_name_for_newcomers(session: Session, store: SettingsStore) -> dict | None:
    """A row named after a watch, with no name for the people who haven't got one.

    Since issue #84 Shortlist will not invent a title, so such a row is simply not built for anyone
    below the history threshold. That is the right behaviour and the wrong silence: the rows just stop
    updating, which to an operator upgrading into this looks exactly like the bug it fixes. This is
    the difference between "we stopped guessing" and "we stopped guessing AND told you".

    Dismissable, and the id encodes the affected rows, so naming one — or adding another — surfaces it
    again rather than staying hidden behind an old dismissal.
    """
    from shortlist.server.db.models import DEFAULT_SLUG, Collection

    rows = [
        row
        for row in session.query(Collection).filter(Collection.enabled.is_(True), Collection.build == "per_person")
        # A fallback that itself needs a seed can never render, so it is no fallback — the API refuses
        # new ones, and this keeps the alert firing on databases that already hold one.
        if names_a_seed((row.name_template or row.name) or "") and not _usable_fallback(row)
    ]
    # The DEFAULT row takes its title from the global setting, never its own column.
    global_name = store.get("row.name_template") or ""
    display: dict[str, str] = {row.slug: (row.name_template or row.name) for row in rows}
    if names_a_seed(global_name):
        for row in session.query(Collection).filter(Collection.enabled.is_(True), Collection.slug == DEFAULT_SLUG):
            if _usable_fallback(row):
                continue
            # NOT gated on its `name_template` being empty. A stale value in that column is a real
            # state on any database written before the API guarded it, and the ENGINE ignores it —
            # `context_builder` forces the default row's template to "" so the global setting wins.
            # Gating on it silenced this alert on exactly the upgraded servers it exists for.
            if row.slug not in display:
                rows.append(row)
            # Named by the GLOBAL template, never its own column: the default row's `name` is the
            # stale "✨ Picked for You" from migration 0001, which is both a title the operator will
            # not find on their Rows page and the hardcoded string this whole issue is about.
            display[row.slug] = global_name
    if not rows:
        return None
    names = sorted(set(display.values()))
    # Stripped, never raw. Every row here has `{top_seed}` in its name BY DEFINITION — that is the
    # condition being reported — so quoting the name verbatim guaranteed a brace-laden title:
    # "Because you watched {top_seed} won't be built for…". Stripping can leave nothing at all (a
    # row named only after the placeholder), which is what the last fallback is for.
    if len(names) == 1:
        stripped = _row_display_name(names[0])
        shown = f"“{stripped}”" if stripped else "A row"
    else:
        shown = f"{len(names)} rows"
    return {
        "id": "rows-unnamed-" + ",".join(sorted({row.slug for row in rows})),
        "severity": "info",
        "title": f"{shown} won\u2019t be built for people with nothing watched yet",
        "body": (
            "Its name follows a watch, and someone new to your server hasn\u2019t got one — so Shortlist "
            "has no name for their copy of it and doesn\u2019t invent one. Open the row and set "
            "\u201cName for people with nothing watched yet\u201d if you\u2019d like them to have it; "
            "leave it and they get this row once they\u2019ve watched enough. Nothing has been deleted."
        ),
        "action_url": "/rows",
        "action_label": "Open Rows",
        "dismissable": True,
    }


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


def _is_evidence_of_nothing_requested(event: Event) -> bool:
    """Whether one ``requests.none_qualified`` event says anything about the owner's SETTINGS.

    See :func:`_requests_found_nothing` for why a dry run and a demand-unreachable run don't. An
    event predating those fields counts as evidence — the old behaviour, which is the safe default
    for an alert whose whole job is to break a five-day silence.
    """
    data = event.message if isinstance(event.message, dict) else {}
    return not data.get("dry_run") and not data.get("demand_unreachable")


def _requests_found_nothing(session: Session) -> dict | None:
    """Recent runs wanted titles but the rating gate passed none of them, so nothing reached
    Sonarr/Radarr and nothing reached the inbox either.

    This is the one request outcome with no trace anywhere the owner looks: a run that sends nothing
    and queues nothing shows "0 requested" on a green run, which is also what a run with nothing to
    do shows. It went unnoticed for five days in production. Fires only after TWO runs, so a single
    quiet night — genuinely common — never nags.

    Two shapes of that zero are NOT evidence and are filtered out before counting, because both
    fired this alert on the maintainer's server while the nightly run was requesting normally
    (2026-09-03: six events, every one of them from a one-user manual run):

    * a **dry run** asked for nothing by definition, so it can't be short of things to ask for;
    * a run covering fewer people than its own ``min_demand`` (``demand_unreachable``) could not have
      filled the pool whatever the settings were — telling that owner to loosen their floors is
      advice they can follow forever without effect. Same mis-attribution the language branch below
      exists to prevent, one level up.

    Filtered in Python rather than SQL: both facts live inside the event's JSON message, and the
    fetch is widened so a burst of skipped test runs cannot crowd the real ones out of the window.
    """
    since = datetime.now(UTC) - timedelta(days=3)
    candidates = (
        session.query(Event)
        .filter(Event.scope == "requests.none_qualified", Event.ts >= since)
        .order_by(Event.ts.desc())
        .limit(50)
        .all()
    )
    events = [e for e in candidates if _is_evidence_of_nothing_requested(e)][:5]
    if len(events) < 2:
        return None
    latest = events[0]
    data = latest.message if isinstance(latest.message, dict) else {}
    wanted, pool = data.get("wanted", 0), data.get("pool_size", 0)
    examined = data.get("examined", 0)
    # Two different problems wearing the same "0 requested". Only one is about the rating floor.
    if not wanted and not pool:
        # Nothing missing, or an event written before `wanted` was recorded. Either way there is no
        # honest sentence to write — "found 0 titles you don't have" reads as a fault and isn't one.
        return None
    if not pool:
        # Name the limit that ACTUALLY bound. Since the language mode became a base floor, a pool can
        # be empty purely because "only these languages" removed everything — and telling that owner
        # to loosen their demand or year settings is advice they can follow forever without effect.
        dropped_by_language = data.get("dropped_by_language", 0)
        if dropped_by_language and dropped_by_language >= wanted:
            body = (
                f"The last {len(events)} runs found {wanted} titles people wanted that you don't have, and "
                "your Language setting ruled out every one of them. Allow another language, or switch "
                "to 'Prefer these' so the rest wait in your inbox instead."
            )
        elif dropped_by_language:
            body = (
                f"The last {len(events)} runs found {wanted} titles people wanted that you don't have. "
                f"Your Language setting ruled out {dropped_by_language} of them, and the rest didn't clear "
                "your minimum number of people or your release-year range."
            )
        else:
            body = (
                f"The last {len(events)} runs found {wanted} titles people wanted that you don't have, and "
                "none of them cleared your minimum number of people or your release-year range. Loosen "
                "either to let some through."
            )
    elif data.get("exhausted_pool"):
        body = (
            f"The last {len(events)} runs rated every title they checked ({pool} checks across the rows), "
            "and none cleared your minimum rating. Lower it, or widen the year range, to let some "
            "through."
        )
    else:
        body = (
            f"The last {len(events)} runs got through {examined} of {pool} checks before running out of "
            "rating lookups, and none of those cleared your minimum rating. "
            "Raise how many to auto-request per run so each run looks further, or lower the minimum."
        )
    return {
        "id": f"requests-none-qualified-{latest.ts.date().isoformat()}",
        "severity": "warning",
        "title": "Nothing is being requested",
        "body": body,
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


def _aware(moment: datetime) -> datetime:
    """SQLite hands back naive datetimes for values stored as UTC; compare them as UTC."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


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
    from shortlist.server.services import jobs as jobs_service

    failed = session.query(Job).filter(Job.status == "failed").order_by(Job.id.desc()).all()
    # A failure a later successful run of the same whole-job kind has already repaired is not news. By FINISH
    # time, not id: ids follow queue order, and a pass retrying through an outage can fail after a later one
    # already succeeded.
    for kind in {e.kind for e in jobs_service.CATALOG if e.later_success_clears_failure}:
        last_success = session.query(func.max(Job.finished_at)).filter(Job.kind == kind, Job.status == "done").scalar()
        if last_success is not None:
            failed = [
                job
                for job in failed
                if not (
                    job.kind == kind and job.finished_at is not None and _aware(job.finished_at) < _aware(last_success)
                )
            ]
    if not failed:
        return None
    kinds = sorted({job.kind for job in failed})
    # The body must not claim "Plex may not reflect this" or "run it again" for every failure. For
    # `watch.reconcile` — the live credit pass — both are false: it never touches Plex, and it is not
    # in the manual allow-list, so "run it again" points at a button that returns 422. The same holds
    # for `backup.take` and `maintenance.prune`.

    entries = {e.kind: e for e in jobs_service.CATALOG}
    touched_plex = any(entries[k].writes_plex for k in kinds if k in entries)
    rerunnable = any(entries[k].manual for k in kinds if k in entries)
    consequence = " Plex may not reflect what you asked for —" if touched_plex else " Nothing on Plex changed —"
    remedy = " and run it again." if rerunnable else "."
    return {
        "id": f"failed-jobs-{failed[0].id}",
        "severity": "error",
        "title": f"{len(failed)} background job{'s' if len(failed) != 1 else ''} failed",
        "body": (
            f"Shortlist gave up on {', '.join(kinds)} after retrying.{consequence} Open Jobs to see the error{remedy}"
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
    # The latest run that actually RECORDED a measurement — not merely the latest that finished. A run
    # that failed early, was aborted, or never reached the privacy phase carries no `unhideable_rows`
    # key at all; treating that as "{}" would let one bad run clear a real finding from the alert while
    # the exposure is untouched. That silence is the thing this whole check exists to end.
    run = _newest_run_with(session, "unhideable_rows")
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
#: at all — so any repeat is already abnormal. Three clears the legitimate ways a row moves more than
#: once in a day: placed when it is first delivered, then again when the owner changes where it sits.
#: A rename is not one of them — hubs are matched by ratingKey, so a renamed row stays where it is.
#: Nothing benign moves the same row three times in a day.
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
        if message.get("verified") is not True:
            # We asked, and the re-read said the shelf did NOT end up as asked — so the row was never
            # put back, and there is nothing here for another tool to have undone. Counting these was
            # reading our own failures as somebody else's interference: on the maintainer's server
            # (2026-09-08) all 50 records in the window were `verified: False`, Plex was answering 200
            # to every move and applying none, and the bell reported "Shortlist has had to put the same
            # row back 50 times ... so something else is moving it" and named Kometa and Agregarr.
            #
            # `is not True` rather than `is False`: a record with no verdict at all — an older row, a
            # shape from before this field existed — is not evidence either. An unverified move is written
            # as a warning, so it shows in the event log, not in the bell.
            continue
        library = message.get("library") or "a library"
        moved = message.get("moved")
        if not isinstance(moved, list):
            continue
        counts = per_library.setdefault(library, {})
        # ONE count per row per PASS, which is what this dict has always claimed to hold. Counting
        # every occurrence instead made the alert fire on servers where nothing was fighting us: a
        # `{top_seed}` row renders the SAME title for everyone who watched that title, so two people
        # who both watched one film give one pass two entries for "one row". Two ordinary passes then
        # crossed a threshold meant for three genuine re-moves, and the notification named Kometa and
        # Agregarr as the likely cause of something neither had done. Measured on the maintainer's
        # server: two rows over the line on three consecutive days, with no other tool involved.
        for title in set(moved):
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


def _filters_not_enforced(session: Session) -> dict | None:
    """Plex is showing an account rows that its share filter says to hide.

    A different fault from `_rows_we_cannot_hide`, and the difference is the whole point. There, Plex
    REFUSES the filter and the owner has a remedy (clear the restriction profile). Here the filter was
    accepted, stored, and read back — and Plex is serving the rows anyway. Nothing the owner does in
    Plex fixes that, so this alert asks for a bug report instead of an action, and says plainly that
    the rows are visible now rather than implying a setting is wrong.

    Reported on discussion #88: six Plex Home accounts, restriction profile None on every one of them,
    each seeing all six per-person rows. Nothing could see it — the read-back proves plex.tv STORED the
    filter, and the only look-through-their-eyes check was gated on the account having a profile.
    """
    run = _newest_run_with(session, "filters_not_enforced")
    exposed = ((run.stats or {}).get("filters_not_enforced") or {}) if run else {}
    if not exposed:
        return None
    names = sorted(exposed)
    who = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return {
        "id": f"filters-not-enforced-{run.id}",
        "severity": "error",
        "title": "Plex is ignoring the privacy filter",
        "body": (
            f"{who} can see rows belonging to other people, even though Shortlist wrote the hide "
            "rules to their Plex account and confirmed Plex saved them. Their Restriction Profile is "
            "already None, so there is nothing to clear — Plex is storing the rule and not applying "
            "it. Those rows are visible to them right now. Shortlist spot-checks ONE account of each "
            "kind, so this is likely every shared or managed account on the server, not only the "
            f"{'one' if len(names) == 1 else 'ones'} named. Please open an issue and include this "
            "run's id and what the Sharing page shows for them, including any restrictions of your own "
            "on that account."
        ),
        "action_url": f"/runs/{run.id}",
        "action_label": "See the run",
        "dismissable": False,
    }


def _filters_plex_cannot_read(session: Session) -> dict | None:
    """An account whose share filter Plex itself cannot read, so no row can be hidden from it.

    A literal `&` inside one of the owner's labels ("Kids & Family") makes Plex answer that account's
    Home with HTTP 500 — measured 2026-09-13. Shortlist refuses to write its exclude into a filter
    nothing can verify, and by owner decision does not block everyone else's rows over it, so this card
    is the whole warning. Error, undismissable, and cleared by the next run that looked and found none.
    """
    run = _newest_run_with(session, "unreadable_filters")
    unreadable = ((run.stats or {}).get("unreadable_filters") or {}) if run else {}
    if not unreadable:
        return None
    lines = "\n".join(f"• {name}: {why}" for name, why in sorted(unreadable.items()))
    return {
        "id": f"filters-unreadable-{run.id}",
        "severity": "error",
        "title": "Shortlist can't hide rows from some accounts",
        "body": (
            "Shortlist can't hide other people's rows from these accounts in the libraries named, because "
            f"Plex can't read the restrictions set on them:\n\n{lines}\n\n"
            "Everyone else's rows are still hidden and still showing on Home. Rename the label in Plex "
            "(Settings → Manage Library Access → the person → Restrictions, and the label itself on your "
            "titles) and the next run fixes it."
        ),
        "action_url": "/sharing",
        "action_label": "See sharing",
        "dismissable": False,
    }


def _restrictions_restored(session: Session) -> dict | None:
    """A privacy pass moved our excludes out from behind a `|`, and an account's OWN Plex restriction applies again.

    Before #116 Shortlist joined its excludes to an account's existing restriction with `|`, which Plex
    reads as OR — so a rating exclude or an allow-list the owner set stopped applying, silently. The
    repair turns it back on, and the people on those accounts will find less on the server than they
    had yesterday. Said for a week, so the owner hears it from Shortlist before they hear it from a
    friend. Info, not a fault.

    Read from audit events rather than run stats: several jobs run the privacy pass without persisting
    a run, and whichever runs first after upgrading is the one that repairs.
    """
    since = datetime.now(UTC) - timedelta(days=7)
    events = (
        session.query(Event)
        .filter(Event.scope == RESTRICTION_RESTORED_SCOPE, Event.ts >= since)
        .order_by(Event.ts.desc())
        .limit(200)
        .all()
    )
    if not events:
        return None
    names = sorted({str((e.message or {}).get("username") or "") for e in events} - {""})
    if not names:
        return None
    who = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return {
        "id": f"restrictions-restored-{events[0].id}",
        "severity": "info",
        "title": "Plex restrictions you set are working again",
        "body": (
            f"{who} had a restriction of your own in Plex — a content rating or label rule. An earlier "
            "version of Shortlist joined its hide rule to it in a way Plex reads as 'either/or', which "
            "quietly switched your restriction off: an exclude rule also let them see other people's rows, "
            "and an allow-list let them see the whole library. Shortlist has fixed that, so your "
            "restriction applies again and they may notice less on the server than before.\n\n"
            "One thing to know if it is an allow-list: Shortlist fills their own row only with titles the "
            "allow-list lets them see, so a narrow list gives a short row."
        ),
        "action_url": "/sharing",
        "action_label": "See sharing",
        "dismissable": True,
    }


def _missed_job_in_words(message: dict) -> str:
    """'"Back up the database" due at 03:00 on 29 September' — the owner's clock, never a raw job id."""
    name = message.get("name")
    what = f'"{name}"' if name else "a scheduled job"
    try:
        local = datetime.fromisoformat(str(message.get("scheduled_for"))).astimezone()
    except ValueError:
        return what
    return f"{what} due at {local:%H:%M} on {local.day} {local:%B}"


def _scheduled_jobs_missed(session: Session) -> dict | None:
    """A scheduled job APScheduler skipped because it started more than its grace late.

    The scheduler drops such a job and moves on to its next time, so until this the only trace was a log
    line — the whole 2026-09-29 nightly row run went that way unnoticed. Row runs no longer have a grace
    (`scheduler._register`), so only the fixed timers land here. One item for every miss in the last day,
    keyed to the newest so a later miss re-surfaces after a dismissal.
    """
    since = datetime.now(UTC) - timedelta(days=1)
    events = (
        session.query(Event)
        .filter(Event.scope == SCHEDULE_MISSED_SCOPE, Event.ts >= since)
        .order_by(Event.id.desc())
        .limit(200)
        .all()
    )
    if not events:
        return None
    messages = [e.message or {} for e in events]
    missed = [_missed_job_in_words(m) for m in messages]
    shown = missed[:3] if len(missed) <= 3 else [*missed[:2], f"{len(missed) - 2} more"]
    listed = shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"
    if len(events) == 1:
        title = "A scheduled job didn't run"
        body = f"{listed[0].upper()}{listed[1:]} was skipped because Shortlist was busy at that moment. "
        body += "It will run again at its next scheduled time."
    else:
        title = f"{len(events)} scheduled jobs didn't run"
        body = f"Shortlist was busy when these were due, so they were skipped: {listed}. "
        body += "Each will run again at its next scheduled time."
    return {
        "id": f"schedule-missed-{events[0].id}",
        "severity": "warning",
        "title": title,
        "body": body,
        "action_url": "/jobs",
        "action_label": "Open Jobs",
        "dismissable": True,
    }


def build_notifications(session: Session, store: SettingsStore, current_version: str) -> list[dict]:
    """Every currently-firing notification the owner hasn't dismissed, most severe first. Dismissal is
    by id, and each dismissable id encodes its state (the run id, the version), so a NEW failure or a
    newer release surfaces again rather than staying hidden forever."""
    candidates = [
        _update_available(current_version),
        _runs_paused(store),
        _secrets_we_cannot_read(store),
        _last_run_problem(session),
        _failed_jobs(session),
        _scheduled_jobs_missed(session),
        _mdblist_quota(session),
        _requests_found_nothing(session),
        _recent_service_errors(session),
        _rows_with_no_name_for_newcomers(session, store),
        _rows_we_cannot_hide(session),
        _filters_not_enforced(session),
        _filters_plex_cannot_read(session),
        _restrictions_restored(session),
        _owner_sees_all_rows(session),
        _shelf_contention(session),
        _playback_listener_down(store),
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
