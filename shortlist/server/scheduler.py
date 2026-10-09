"""APScheduler wiring — one job per distinct per-row cron; the runs table is the durable queue.

Every enabled row carries its own cron (``Collection.schedule``); rows that share a cron fire together
as one run scoped to just them. A row with no schedule never fires here. There is no global schedule —
the whole "when does this run" question is answered per row.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, tzinfo

from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from shortlist.server.db.models import Collection

_JOB_PREFIX = "row-schedule::"
# A fixed daily reconcile of every user's watch status, independent of any row's cron — so the
# effectiveness report stays fresh (hit rate, recent watches) even for rows that only run weekly, or
# users with no scheduled row. Read-only: fetches history and marks hits, never writes to Plex.
WATCH_SYNC_JOB_ID = "watch-sync"
USER_SYNC_JOB_ID = "user-sync"
BACKUP_JOB_ID = "db-backup"
PRIVACY_SYNC_JOB_ID = "privacy-sync"
ROW_VISIBILITY_JOB_ID = "rows-visibility"
SYNC_CHECK_JOB_ID = "sync-check"
MAINTENANCE_PRUNE_JOB_ID = "maintenance-prune"
THEMES_ROTATE_JOB_ID = "themes-rotate"
JOBS_DRAIN_JOB_ID = "jobs.drain"
JOBS_SWEEP_JOB_ID = "jobs.sweep"

#: One event per scheduled job APScheduler skipped for starting later than its grace. Read by the bell.
SCHEDULE_MISSED_SCOPE = "schedule.missed"
# The queue worker's own ticks. A late one is caught by the next tick a minute later, so recording it would
# only bury the misses that matter.
_UNRECORDED_MISSES = frozenset({JOBS_DRAIN_JOB_ID, JOBS_SWEEP_JOB_ID})


#: The built-in cron for each schedulable settings key, and the ONLY place each of these expressions
#: is written down — `settings_store.DEFAULTS` derives its blank placeholders from this dict. Keeping a
#: second copy beside the settings defaults is what let the drift check be documented as off-by-default
#: for months while running nightly.
#:
#: A BLANK setting means "use this" for every key here except `sync.check_cron`, whose blank genuinely
#: means off — which is why the Schedule view must show the EFFECTIVE cron rather than the stored one.
#: Reading the raw setting made a backup that runs nightly at 03:00 appear under "Not scheduled".
DEFAULT_CRONS: dict[str, str] = {
    # 04:17 local daily — a quiet hour, offset off the top of the hour.
    "sync.watch_cron": "17 4 * * *",
    # 04:47 — 30 min after the watch sync so the two don't overlap.
    "sync.users_cron": "47 4 * * *",
    # 03:00 — before any syncs or row runs.
    "backup.cron": "0 3 * * *",
    # Every 30 minutes. It reads the plex.tv account list itself, so this is how soon an account newly
    # shared with the server stops seeing everyone's rows in the Collections tab — once a day left that
    # open for up to 24 hours. Only ever makes the server MORE private; a clean pass takes ~20s and
    # writes nothing (measured on a 48-account server: 320 passes in a week, 2 filter writes).
    "privacy.sync_cron": "*/30 * * * *",
    # 00:00 exactly, and deliberately NOT offset off the hour like the others: this is the one
    # schedule whose whole meaning is "the day changed" (issue #102). A row set to Mondays that
    # turned over at 00:17 would be wrong for the first seventeen minutes of every day it owns, and
    # a Sunday row would linger into Monday — the thing the screen promises is the calendar day.
    "rows.visibility_cron": "0 0 * * *",
    # 05:45 — after the rows build (03:30), the syncs, and the 05:30 privacy pass, so it checks the
    # state those actually left behind. Still turn-off-able: clearing the box stores "" and
    # `blank_means_off` keeps it off rather than falling back to this.
    "sync.check_cron": "45 5 * * *",
    # 06:15 — last of the night, after every other schedule has finished writing runs and events, so
    # the retention pass trims a settled database rather than one still being appended to.
    "maintenance.prune_cron": "15 6 * * *",
    # Once a day. Rows have their own crons and read the theme the DB holds, so when this fires is not
    # load-bearing for a row's build: one that builds before it simply uses the theme it already has. The job
    # itself judges "due" on the calendar day (UTC), so firing any time on the day gives the same answer.
    "themes.rotate_cron": "30 1 * * *",
}


#: Schedules the owner can switch off entirely. For these, a stored blank means OFF; for every other
#: key it means "inherit the built-in default".
_OFF_ABLE = {"sync.check_cron", "themes.rotate_cron"}


def effective_cron(app, key: str) -> str:
    """The cron this key ACTUALLY runs on — the stored value, or the built-in default it falls back
    to, or "" when the schedule is genuinely off. Resolved exactly as the scheduler resolves it, so
    the UI and the running triggers cannot disagree."""
    return _resolve_cron(app, key, DEFAULT_CRONS.get(key, ""), blank_means_off=key in _OFF_ABLE)


_WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _weekday_number(token: str) -> int:
    if token.lower() in _WEEKDAYS:
        return _WEEKDAYS.index(token.lower())
    if not token.isdigit() or int(token) > 7:
        raise ValueError(f"invalid day of week {token!r}")
    return int(token)


def crontab_trigger(expr: str, timezone: tzinfo | str | None = None) -> BaseTrigger:
    """A trigger for a standard five-field crontab, read the way cron reads it.

    Use this, never `CronTrigger.from_crontab`. APScheduler 3 departs from cron twice:

    - It counts weekdays from 0 = MONDAY where cron counts from 0 = Sunday (its own docstring admits
      it; only 4.x fixes it), so `0 4 * * 1,4` ran on Tuesday and Friday (issue #123). The day-of-week
      field is expanded here and handed over as day NAMES, which APScheduler reads correctly.
    - It requires the day of the month AND the weekday to match. Cron runs when EITHER does, if both
      are restricted, so `0 4 1 * 1` means every Monday and every 1st, not a 1st that is a Monday. A
      field starting with `*` is not a restriction (Vixie cron's DOM_STAR/DOW_STAR), so `*/2` still
      combines with AND.

    Raises:
        ValueError: the expression is not a valid five-field cron.
    """
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Wrong number of fields; got {len(fields)}, expected 5")
    minute, hour, day, month, day_of_week = fields
    either = not day.startswith("*") and not day_of_week.startswith("*")
    if day_of_week != "*":
        days: set[int] = set()
        for part in day_of_week.split(","):
            span, _, step = part.partition("/")
            if "/" in part and (not step.isdigit() or int(step) < 1):
                raise ValueError(f"invalid step in day of week {part!r}")
            if span == "*":
                first, last = 0, 6
            elif "-" in span:
                low, high = span.split("-", 1)
                first, last = _weekday_number(low), _weekday_number(high)
                # `sun` is 0, so a named range ending on it (`sat-sun`, `mon-sun`) ends on 7, the second
                # Sunday. APScheduler's own names always allowed it, so schedules saved that way exist.
                if high.lower() == "sun" and first > 0:
                    last = 7
            else:
                first = _weekday_number(span)
                last = 6 if step else first
            if first > last:
                raise ValueError(f"invalid day-of-week range {part!r}")
            days.update(number % 7 for number in range(first, last + 1, int(step or 1)))  # 7 is Sunday too
        day_of_week = ",".join(_WEEKDAYS[number] for number in sorted(days))
    common = {"minute": minute, "hour": hour, "month": month, "timezone": timezone}
    if either:
        return OrTrigger(
            [CronTrigger(day=day, day_of_week="*", **common), CronTrigger(day="*", day_of_week=day_of_week, **common)]
        )
    return CronTrigger(day=day, day_of_week=day_of_week, **common)


def _job_id(cron: str) -> str:
    return f"{_JOB_PREFIX}{cron}"


def schedule_groups(app) -> dict[str, list[int]]:
    """cron -> ids of the enabled rows that run on it. Blank or invalid crons are skipped (never fire)."""
    groups: dict[str, list[int]] = defaultdict(list)
    with app.state.sessions() as session:
        for row in session.query(Collection).filter_by(enabled=True).all():
            cron = (row.schedule or "").strip()
            if not cron:
                continue
            try:
                crontab_trigger(cron)
            except ValueError:
                # A bad cron must never crash-loop the container; it just means that row won't fire.
                logger.error("row {!r} has an invalid cron {!r} — skipping its schedule", row.slug, cron)
                continue
            groups[cron].append(row.id)
    return dict(groups)


def _local_now() -> datetime:
    """Now in the server's local time zone (the zone APScheduler evaluates crontabs in)."""
    return datetime.now().astimezone()


def _make_job(app, cron: str, collection_ids: list[int]):
    last_slot: datetime | None = None

    async def fire() -> None:
        nonlocal last_slot
        # On the autumn clock change the repeated hour makes a cron slot inside it come round twice: same
        # wall-clock minute, an hour apart. The second firing would queue a full duplicate run behind the first.
        # Accepted: this keys on the actual fire minute, so an hourly cron's legitimately repeated-hour fire
        # is dropped once a year too.
        slot = _local_now().replace(tzinfo=None, second=0, microsecond=0)
        if slot == last_slot:
            logger.info("scheduled run skipped: cron '{}' already fired for {} (repeated hour)", cron, slot)
            return
        last_slot = slot
        logger.info("scheduled run firing: cron '{}' for {} row(s)", cron, len(collection_ids))
        try:
            await app.state.run_service.start_run(trigger="schedule", dry_run=False, collection_ids=collection_ids)
        except Exception:
            # Unguarded, this exception escapes into APScheduler and the run silently never happens —
            # the "why didn't 03:30 fire" case. Log with full context so it lands in the durable file.
            # start_run only inserts a Run row + spawns the background task here; the token-bearing Plex
            # I/O runs inside _execute (its own redaction), so this traceback never carries a secret.
            logger.exception("scheduled run failed to start (cron '{}', {} row(s))", cron, len(collection_ids))

    return fire


def _register(scheduler: AsyncIOScheduler, app, groups: dict[str, list[int]]) -> None:
    for cron, ids in groups.items():
        # Owner decision 2026-10-02: a late row run still runs (`None` = no grace). A restart replays nothing
        # (in-memory job store, rebuilt at boot), but a paused host/VM or a forward clock jump wakes the live
        # process past due and starts one full run (coalesced) then. Accepted: leak-safe ordering still holds.
        scheduler.add_job(
            _make_job(app, cron, ids),
            crontab_trigger(cron),
            id=_job_id(cron),
            misfire_grace_time=None,
            replace_existing=True,
        )


async def _queue_and_drain(app, kind: str, payload: dict | None = None) -> None:
    """Put a scheduled task on the durable queue and run it now.

    APScheduler stays the TRIGGER; the `jobs` table is what makes the work survive. Before this, each
    of these was a bare coroutine whose only failure path was `logger.exception` — so the three tasks
    that keep a server correct between runs (roster reconcile, watch history, backups) failed
    invisibly, were never retried, and left nothing on the Jobs page to notice.
    """
    from shortlist.server.services import jobs

    try:
        jobs.enqueue(app.state.sessions, kind, payload or {})
    except Exception:
        logger.exception("could not queue {}", kind)
        return
    await jobs.drain_now(app.state, f"scheduled {kind}")


def _resolve_cron(app, key: str, fallback: str, *, blank_means_off: bool = False) -> str:
    """A cron from settings, falling back to the built-in default.

    A bad expression must never crash-loop the container, so an invalid value is logged and the
    default used — the schedule keeps running rather than the app failing to boot.

    ``blank_means_off`` separates "never configured" from "deliberately cleared", which are the same
    thing for most jobs and must not be for a turn-off-able one: without it, the schedule editor's
    "Turn this schedule off" writes "" and the very next resolve falls straight back to the default,
    so the off switch silently does nothing. Only set it for schedules the UI offers to disable.
    """
    from shortlist.server.db.models import Setting

    # The RAW row, not SettingsStore.get: that folds `DEFAULTS` in, so an unset key and a key the
    # owner deliberately cleared both come back as "" and the two become impossible to tell apart —
    # which is the whole distinction `blank_means_off` exists to make.
    with app.state.sessions() as session:
        row = session.get(Setting, key)
    # `.get`, not `["v"]`: a settings row whose JSON is not shaped {"v": ...} would raise here, and
    # this runs inside `build_scheduler` at BOOT — the one place a bad value must never
    # crash-loop the container (see the docstring above).
    custom = (row.value or {}).get("v") if row is not None else None
    if custom and isinstance(custom, str) and custom.strip():
        try:
            crontab_trigger(custom.strip())
            return custom.strip()
        except ValueError:
            logger.warning("invalid {} {!r} — falling back to default", key, custom)
            # For an off-able schedule the fallback direction is OFF, not on. These are the jobs that
            # WRITE to Plex, so a typo in the box must not quietly schedule one at the built-in time.
            return "" if blank_means_off else fallback
    # Stored, and empty: an explicit "off" rather than an absent setting.
    if blank_means_off and isinstance(custom, str):
        return ""
    return fallback


def _register_watch_sync(scheduler: AsyncIOScheduler, app) -> None:
    """The daily watch-status reconcile — one fixed job, unaffected by row schedules."""
    cron = _resolve_cron(app, "sync.watch_cron", DEFAULT_CRONS["sync.watch_cron"])

    async def fire() -> None:
        # Queued, not called: watch history drives every recommendation, so a silent failure here
        # degrades picks server-wide while everything still looks healthy.
        await _queue_and_drain(app, "sync.history")

    scheduler.add_job(fire, crontab_trigger(cron), id=WATCH_SYNC_JOB_ID, replace_existing=True)


def _register_user_sync(scheduler: AsyncIOScheduler, app) -> None:
    """Daily user-list reconcile — pull shared/Home users from plex.tv + Tautulli."""
    cron = _resolve_cron(app, "sync.users_cron", DEFAULT_CRONS["sync.users_cron"])

    async def fire() -> None:
        # Queued, not called. This is the sync that notices a NEW account (and writes the filters that
        # stop them seeing everyone's rows) and notices someone leaving the share — and its only
        # failure path used to be a log line nobody reads. As a job it retries with backoff, shows up
        # on the Jobs page, and raises a notification if it gives up.
        await _queue_and_drain(app, "sync.users")

    scheduler.add_job(fire, crontab_trigger(cron), id=USER_SYNC_JOB_ID, replace_existing=True)


def _resolve_backup_settings(app) -> tuple[str, int]:
    """Read backup cron + max_keep from settings, falling back to defaults."""
    from shortlist.server.services.backup import DEFAULT_MAX_BACKUPS
    from shortlist.server.settings_store import SettingsStore

    cron = _resolve_cron(app, "backup.cron", DEFAULT_CRONS["backup.cron"])
    max_keep = DEFAULT_MAX_BACKUPS
    with app.state.sessions() as session:
        custom_keep = SettingsStore(session).get("backup.max_keep")
    if custom_keep and isinstance(custom_keep, int) and 1 <= custom_keep <= 100:
        max_keep = custom_keep
    return cron, max_keep


def _register_backup(scheduler: AsyncIOScheduler, app) -> None:
    """Scheduled DB backup — keeps the last N copies so a bad migration or data loss is recoverable."""
    cron, max_keep = _resolve_backup_settings(app)

    async def fire():
        # The one job whose failure nobody notices until they need the backup — which is the worst
        # possible moment to discover a month of unread log lines.
        await _queue_and_drain(app, "backup.take", {"label": "scheduled", "max_keep": max_keep})

    scheduler.add_job(fire, crontab_trigger(cron), id=BACKUP_JOB_ID, replace_existing=True)


def _register_privacy_sync(scheduler: AsyncIOScheduler, app) -> None:
    """Scheduled re-merge of every account's share filter (every 30 minutes by default).

    The automatic Privacy Check + write gate that used to VERIFY hiding before each write was removed
    on 2026-07-16 at the owner's request, so nothing checks it after the fact any more — leak-safe
    write ordering is the only remaining guarantee. This pass is the cheapest safety net against
    drift: it builds nothing, delivers nothing and promotes nothing, so it can only ever make the
    server more private.

    ONE EXCEPTION, and it is bounded: for an account the owner set `manage_sharing=0` ("leave their Plex sharing
    alone"), the pass REMOVES our per-person excludes from that one account instead of merging into it
    (`pipeline._leave_sharing_alone`). That widens what that account sees, by request. It stays handler-safe
    because it creates and promotes nothing — no row becomes visible that was not already on the server — and it
    touches nobody else's filter, so every other account still excludes that person's row. Anything else that
    widens visibility needs its own argument; rule 1 does not cover it.
    """
    cron = _resolve_cron(app, "privacy.sync_cron", DEFAULT_CRONS["privacy.sync_cron"])

    async def fire() -> None:
        from shortlist.server.db.models import Job

        # Writer jobs wait out a run, so a long run would otherwise leave one pass queued per tick, all run
        # back to back afterwards. One still waiting will read the state it finds when it starts — and so
        # will one waiting to RETRY, which `_finish` puts back to `queued` with its `started_at` kept. Its
        # longest backoff (15 minutes) is shorter than a tick, so it never delays a pass by more than that.
        with app.state.sessions() as session:
            waiting = session.query(Job).filter(Job.kind == "privacy.sync", Job.status == "queued").first()
        if waiting is not None:
            logger.debug("privacy sync already queued (job {}) — not queuing another", waiting.id)
            return
        # `scheduled`: only a pass the timer started may count as quiet and stay out of Recent.
        await _queue_and_drain(app, "privacy.sync", {"scheduled": True})

    scheduler.add_job(fire, crontab_trigger(cron), id=PRIVACY_SYNC_JOB_ID, replace_existing=True)


def _register_row_visibility(scheduler: AsyncIOScheduler, app) -> None:
    """Apply each row's day schedule when the day turns over ("When it appears", issue #102).

    Rows build at 03:30, so a run is far too late to be what shows and hides them: a Monday row would
    stay up until 03:30 Tuesday, and a row that rebuilds weekly for days. This tick is therefore the
    mechanism, not a tidy-up behind one.

    Free on a server where no row narrows its days and no seasonal row has opened or closed a season in
    the last week, which is every server until somebody uses either: the handler answers that from one
    query and returns before building a Plex client. Where a row IS scheduled, the pass costs a privacy
    sync (`engine_run` with no users) plus one ~5ms hub visibility write per collection — it holds no
    state, so it simply reapplies today's answer every night, and anything it cannot do is done by the
    next one.
    """
    cron = _resolve_cron(app, "rows.visibility_cron", DEFAULT_CRONS["rows.visibility_cron"])

    async def fire() -> None:
        await _queue_and_drain(app, "rows.visibility")

    scheduler.add_job(fire, crontab_trigger(cron), id=ROW_VISIBILITY_JOB_ID, replace_existing=True)


def _register_sync_check(scheduler: AsyncIOScheduler, app) -> None:
    """The drift check — nightly at 05:45 by default; clearing the cron turns it off entirely.

    It ships ON because drift is the failure nobody notices. It is still the one schedule that can be
    switched OFF, because it WRITES corrections to Plex: an owner who wants to review drift before it
    is repaired must be able to say so. That is what `blank_means_off` buys — a STORED blank means off
    rather than "inherit the default". Deletion is separately gated behind `confirmed`, so the
    unattended pass corrects without removing anything.
    """
    cron = _resolve_cron(app, "sync.check_cron", DEFAULT_CRONS["sync.check_cron"], blank_means_off=True)
    if not cron:
        # Remove any trigger left from a previous setting, so clearing the box really turns it off.
        if scheduler.get_job(SYNC_CHECK_JOB_ID):
            scheduler.remove_job(SYNC_CHECK_JOB_ID)
        return

    async def fire() -> None:
        await _queue_and_drain(app, "sync.check")

    scheduler.add_job(fire, crontab_trigger(cron), id=SYNC_CHECK_JOB_ID, replace_existing=True)


def _register_maintenance_prune(scheduler: AsyncIOScheduler, app) -> None:
    """The nightly retention pass — a FLOOR under the prune each run persist already queues.

    Retention is queued after every run, which is fine until runs stop happening: a server whose rows
    all have blank crons, or one paused with `paused_all`, never prunes anything again, so `runs`,
    `events` and the expired cache rows grow without bound in the same file the backup copies whole
    and keeps ten of. Local database housekeeping only — nothing on Plex changes.
    """
    cron = _resolve_cron(app, "maintenance.prune_cron", DEFAULT_CRONS["maintenance.prune_cron"])

    async def fire() -> None:
        await _queue_and_drain(app, "maintenance.prune")

    scheduler.add_job(fire, crontab_trigger(cron), id=MAINTENANCE_PRUNE_JOB_ID, replace_existing=True)


def _register_theme_rotation(scheduler: AsyncIOScheduler, app) -> None:
    """The daily pass that gives explore rows' people their next theme (#138). Database only; clearing the
    cron turns it off."""
    cron = _resolve_cron(app, "themes.rotate_cron", DEFAULT_CRONS["themes.rotate_cron"], blank_means_off=True)
    if not cron:
        if scheduler.get_job(THEMES_ROTATE_JOB_ID):
            scheduler.remove_job(THEMES_ROTATE_JOB_ID)
        return

    async def fire() -> None:
        await _queue_and_drain(app, "themes.rotate")

    scheduler.add_job(fire, crontab_trigger(cron), id=THEMES_ROTATE_JOB_ID, replace_existing=True)


def _register_jobs_worker(scheduler: AsyncIOScheduler, app) -> None:
    """Drain the durable job queue on a short interval, and sweep abandoned jobs on a long one.

    APScheduler is the TRIGGER only — durability lives in the `jobs` table, exactly as it already
    does for `runs`. That is why no second process or broker is needed: the worker is a coroutine on
    this loop, and anything it was mid-way through when the process died is requeued by the sweep.

    `max_instances=1` matters: without it a slow drain would overlap itself and two workers would
    race for the same job.
    """
    from shortlist.server.services import jobs

    async def drain():
        try:
            await jobs.run_pending(app.state)
        except Exception:
            logger.exception("job worker tick failed")  # never let a bad tick kill the scheduler

    async def sweep():
        try:
            jobs.recover_stale(app.state.sessions)
        except Exception:
            logger.exception("job sweep failed")

    # 60s, not 10s: every path that queues a job also drains it inline, so this tick only catches
    # work queued while something else held the lock, or retries after a backoff. Ten seconds bought
    # nothing and cost a pair of scheduler log lines every ten seconds, all day.
    scheduler.add_job(drain, "interval", seconds=60, id=JOBS_DRAIN_JOB_ID, max_instances=1, replace_existing=True)
    scheduler.add_job(sweep, "interval", minutes=5, id=JOBS_SWEEP_JOB_ID, max_instances=1, replace_existing=True)


def _record_missed_runs(app):
    """A listener that writes one audit event for each scheduled job APScheduler skipped as too late.

    APScheduler drops a job that starts more than `misfire_grace_time` late and says so only in a log
    line — the whole 2026-09-29 nightly row run went that way, and the owner learned of it from a log
    audit. It calls this on the event-loop thread, so the audit commit runs on the default executor. It
    swallows anything the listener raises, but the guards here are ours so the reason lands in our log.
    """

    def write(job_id: str, fields: dict[str, object]) -> None:
        try:
            from shortlist.server.services.audit import write_audit

            # Warning, not error: `_recent_service_errors` counts error events, which would alert twice.
            write_audit(app.state, SCHEDULE_MISSED_SCOPE, "warning", **fields)
        except Exception:
            logger.exception("could not record that scheduled job {} was skipped", job_id)

    def listener(event: JobExecutionEvent) -> None:
        if event.job_id in _UNRECORDED_MISSES:
            return
        try:
            from shortlist.server.services.jobs import CATALOG

            due = event.scheduled_run_time
            fields: dict[str, object] = {"job": event.job_id}
            name = next((entry.label for entry in CATALOG if entry.schedule_job_id == event.job_id), None)
            if name:
                fields["name"] = name
            fields["scheduled_for"] = due.astimezone(UTC).isoformat()
            # Measured at dispatch, moments after APScheduler's own lateness check: a skipped job never ran.
            fields["late_by_s"] = round((datetime.now(UTC) - due).total_seconds())
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                write(event.job_id, fields)  # no loop to stall (a synchronous dispatch): write it here
                return
            # A miss means the loop is already congested; a commit waiting out another writer's lock here
            # would stall it for up to the busy timeout. `write` logs its own failure, so the future is
            # never left holding an unretrieved exception.
            loop.run_in_executor(None, write, event.job_id, fields)
        except Exception:
            logger.exception("could not record that scheduled job {} was skipped", event.job_id)

    return listener


def _register_settings_driven_jobs(scheduler: AsyncIOScheduler, app) -> None:
    """Every fixed job whose cron comes from a setting — what a rebuild must re-derive.

    The jobs worker is not here: its cadence is not a setting, and a rebuild must not re-add it.
    """
    _register_watch_sync(scheduler, app)
    _register_user_sync(scheduler, app)
    _register_backup(scheduler, app)
    _register_privacy_sync(scheduler, app)
    _register_row_visibility(scheduler, app)
    _register_sync_check(scheduler, app)
    _register_maintenance_prune(scheduler, app)
    _register_theme_rotation(scheduler, app)


def build_scheduler(app) -> AsyncIOScheduler:
    # The one-second default skips nightly runs after even a brief event-loop delay.
    scheduler = AsyncIOScheduler(job_defaults={"misfire_grace_time": 30})
    # Here and never in `rebuild_schedule`: a rebuild re-adds jobs to this same scheduler, whose listeners
    # persist, so registering there would record every miss once per rebuild.
    scheduler.add_listener(_record_missed_runs(app), EVENT_JOB_MISSED)
    groups = schedule_groups(app)
    _register(scheduler, app, groups)
    _register_settings_driven_jobs(scheduler, app)
    _register_jobs_worker(scheduler, app)
    logger.info(
        "scheduled {} row cron group(s) + watch-sync + user-sync + backup + privacy-sync + row-visibility "
        "+ prune{} + job worker",
        len(groups),
        " + sync-check" if scheduler.get_job(SYNC_CHECK_JOB_ID) else "",
    )
    return scheduler


def rebuild_schedule(app) -> None:
    """Re-derive every scheduled job from the DB. Call after any row's schedule or sync cron
    changes so the live scheduler matches the settings exactly."""
    scheduler = app.state.scheduler
    groups = schedule_groups(app)
    wanted = {_job_id(cron) for cron in groups}
    for job in scheduler.get_jobs():
        if job.id.startswith(_JOB_PREFIX) and job.id not in wanted:
            job.remove()  # a cron that no longer has any row
    _register(scheduler, app, groups)
    _register_settings_driven_jobs(scheduler, app)
    logger.info(
        "rebuilt schedule: {} row cron group(s) + watch-sync + user-sync + backup + privacy-sync "
        "+ row-visibility + prune{}",
        len(groups),
        " + sync-check" if scheduler.get_job(SYNC_CHECK_JOB_ID) else "",
    )
