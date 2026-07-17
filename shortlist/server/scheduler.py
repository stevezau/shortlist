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
_WATCH_SYNC_JOB_ID = "watch-sync"
_WATCH_SYNC_CRON = "17 4 * * *"  # 04:17 local daily — a quiet hour, offset off the top of the hour


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
        await app.state.run_service.start_run(trigger="schedule", dry_run=False, collection_ids=collection_ids)

    return fire


def _register(scheduler: AsyncIOScheduler, app, groups: dict[str, list[int]]) -> None:
    for cron, ids in groups.items():
        scheduler.add_job(
            _make_job(app, cron, ids), CronTrigger.from_crontab(cron), id=_job_id(cron), replace_existing=True
        )


def _register_watch_sync(scheduler: AsyncIOScheduler, app) -> None:
    """The daily watch-status reconcile — one fixed job, unaffected by row schedules."""

    async def fire() -> None:
        await app.state.run_service.sync_watched()

    scheduler.add_job(fire, CronTrigger.from_crontab(_WATCH_SYNC_CRON), id=_WATCH_SYNC_JOB_ID, replace_existing=True)


def build_scheduler(app) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    groups = schedule_groups(app)
    _register(scheduler, app, groups)
    _register_watch_sync(scheduler, app)
    logger.info("scheduled {} row cron group(s) + daily watch-sync", len(groups))
    return scheduler


def rebuild_schedule(app) -> None:
    """Re-derive every per-row cron job from the DB. Call after any row's schedule changes (create,
    edit, enable/disable, delete) so the live scheduler matches the rows exactly."""
    scheduler = app.state.scheduler
    groups = schedule_groups(app)
    wanted = {_job_id(cron) for cron in groups}
    for job in scheduler.get_jobs():
        if job.id.startswith(_JOB_PREFIX) and job.id not in wanted:
            job.remove()  # a cron that no longer has any row
    _register(scheduler, app, groups)
    logger.info("rebuilt schedule: {} row cron group(s)", len(groups))
