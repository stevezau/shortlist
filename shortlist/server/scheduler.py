"""APScheduler wiring — one job per distinct per-row cron; the runs table is the durable queue.

Every enabled row carries its own cron (``Collection.schedule``); rows that share a cron fire together
as one run scoped to just them. A row with no schedule never fires here. There is no global schedule —
the whole "when does this run" question is answered per row.
"""

from __future__ import annotations

from collections import defaultdict

from apscheduler.schedulers.asyncio import AsyncIOScheduler
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
SYNC_CHECK_JOB_ID = "sync-check"


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
    # 05:15 — after the watch (04:17) and user (04:47) syncs, so it merges filters for a roster that
    # has already been refreshed. Only ever makes the server MORE private, so a daily pass costs nothing.
    "privacy.sync_cron": "15 5 * * *",
    # 05:45 — after the rows build (03:30), the syncs, and the privacy pass (05:15), so it checks the
    # state those actually left behind. Still turn-off-able: clearing the box stores "" and
    # `blank_means_off` keeps it off rather than falling back to this.
    "sync.check_cron": "45 5 * * *",
}


#: Schedules the owner can switch off entirely. For these, a stored blank means OFF; for every other
#: key it means "inherit the built-in default".
_OFF_ABLE = {"sync.check_cron"}


def effective_cron(app, key: str) -> str:
    """The cron this key ACTUALLY runs on — the stored value, or the built-in default it falls back
    to, or "" when the schedule is genuinely off. Resolved exactly as the scheduler resolves it, so
    the UI and the running triggers cannot disagree."""
    return _resolve_cron(app, key, DEFAULT_CRONS.get(key, ""), blank_means_off=key in _OFF_ABLE)


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
                CronTrigger.from_crontab(cron)
            except ValueError:
                # A bad cron must never crash-loop the container; it just means that row won't fire.
                logger.error("row {!r} has an invalid cron {!r} — skipping its schedule", row.slug, cron)
                continue
            groups[cron].append(row.id)
    return dict(groups)


def _make_job(app, cron: str, collection_ids: list[int]):
    async def fire() -> None:
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
        scheduler.add_job(
            _make_job(app, cron, ids), CronTrigger.from_crontab(cron), id=_job_id(cron), replace_existing=True
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
    # this runs inside `build_scheduler` at BOOT — the one place the docstring below promises a bad
    # value can never crash-loop the container.
    custom = (row.value or {}).get("v") if row is not None else None
    if custom and isinstance(custom, str) and custom.strip():
        try:
            CronTrigger.from_crontab(custom.strip())
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


def _resolve_watch_cron(app) -> str:
    """The watch sync cron, from the DB setting or the built-in default."""
    return _resolve_cron(app, "sync.watch_cron", DEFAULT_CRONS["sync.watch_cron"])


def _register_watch_sync(scheduler: AsyncIOScheduler, app) -> None:
    """The daily watch-status reconcile — one fixed job, unaffected by row schedules."""
    cron = _resolve_watch_cron(app)

    async def fire() -> None:
        # Queued, not called: watch history drives every recommendation, so a silent failure here
        # degrades picks server-wide while everything still looks healthy.
        await _queue_and_drain(app, "sync.history")

    scheduler.add_job(fire, CronTrigger.from_crontab(cron), id=WATCH_SYNC_JOB_ID, replace_existing=True)


def _resolve_users_cron(app) -> str:
    """The user sync cron, from the DB setting or the built-in default."""
    return _resolve_cron(app, "sync.users_cron", DEFAULT_CRONS["sync.users_cron"])


def _register_user_sync(scheduler: AsyncIOScheduler, app) -> None:
    """Daily user-list reconcile — pull shared/Home users from plex.tv + Tautulli."""
    cron = _resolve_users_cron(app)

    async def fire() -> None:
        # Queued, not called. This is the sync that notices a NEW account (and writes the filters that
        # stop them seeing everyone's rows) and notices someone leaving the share — and its only
        # failure path used to be a log line nobody reads. As a job it retries with backoff, shows up
        # on the Jobs page, and raises a notification if it gives up.
        await _queue_and_drain(app, "sync.users")

    scheduler.add_job(fire, CronTrigger.from_crontab(cron), id=USER_SYNC_JOB_ID, replace_existing=True)


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

    scheduler.add_job(fire, CronTrigger.from_crontab(cron), id=BACKUP_JOB_ID, replace_existing=True)


def _register_privacy_sync(scheduler: AsyncIOScheduler, app) -> None:
    """Nightly re-merge of every account's share filter.

    The automatic Privacy Check + write gate that used to VERIFY hiding before each write was removed
    on 2026-07-16 at the owner's request, so nothing checks it after the fact any more — leak-safe
    write ordering is the only remaining guarantee. This pass is the cheapest safety net against
    drift: it builds nothing, delivers nothing and promotes nothing, so it can only ever make the
    server more private.
    """
    cron = _resolve_cron(app, "privacy.sync_cron", DEFAULT_CRONS["privacy.sync_cron"])

    async def fire() -> None:
        await _queue_and_drain(app, "privacy.sync")

    scheduler.add_job(fire, CronTrigger.from_crontab(cron), id=PRIVACY_SYNC_JOB_ID, replace_existing=True)


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

    scheduler.add_job(fire, CronTrigger.from_crontab(cron), id=SYNC_CHECK_JOB_ID, replace_existing=True)


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
    scheduler.add_job(drain, "interval", seconds=60, id="jobs.drain", max_instances=1, replace_existing=True)
    scheduler.add_job(sweep, "interval", minutes=5, id="jobs.sweep", max_instances=1, replace_existing=True)


def build_scheduler(app) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    groups = schedule_groups(app)
    _register(scheduler, app, groups)
    _register_watch_sync(scheduler, app)
    _register_user_sync(scheduler, app)
    _register_backup(scheduler, app)
    _register_privacy_sync(scheduler, app)
    _register_sync_check(scheduler, app)
    _register_jobs_worker(scheduler, app)
    logger.info(
        "scheduled {} row cron group(s) + watch-sync + user-sync + backup + privacy-sync{} + job worker",
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
    _register_watch_sync(scheduler, app)
    _register_user_sync(scheduler, app)
    _register_backup(scheduler, app)
    _register_privacy_sync(scheduler, app)
    _register_sync_check(scheduler, app)
    logger.info(
        "rebuilt schedule: {} row cron group(s) + watch-sync + user-sync + backup + privacy-sync{}",
        len(groups),
        " + sync-check" if scheduler.get_job(SYNC_CHECK_JOB_ID) else "",
    )
