"""APScheduler wiring — the runs table is the durable queue; the scheduler only enqueues."""

from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from rowarr.server.settings_store import SettingsStore

DEFAULT_CRON = "30 3 * * *"


def _make_nightly(app):
    """The one nightly job body, so build_scheduler and reschedule enqueue identically."""

    async def nightly() -> None:
        logger.info("scheduled run firing")
        await app.state.run_service.start_run(trigger="schedule", dry_run=False)

    return nightly


def build_scheduler(app) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    with app.state.sessions() as session:
        cron = SettingsStore(session).get("schedule.cron")
    try:
        trigger = CronTrigger.from_crontab(cron)
    except ValueError:
        # A bad persisted value must never crash-loop the container out of its own fix.
        logger.error("invalid schedule.cron {!r} — falling back to default {!r}", cron, DEFAULT_CRON)
        trigger = CronTrigger.from_crontab(DEFAULT_CRON)
    scheduler.add_job(_make_nightly(app), trigger, id="nightly-run", replace_existing=True)
    logger.info("scheduled nightly run: cron '{}'", cron)
    return scheduler


def reschedule(app, cron: str) -> None:
    """Apply a new cron expression immediately (Settings → Schedules)."""
    app.state.scheduler.add_job(
        _make_nightly(app), CronTrigger.from_crontab(cron), id="nightly-run", replace_existing=True
    )
    logger.info("rescheduled nightly run: cron '{}'", cron)
