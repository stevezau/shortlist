"""Per-row scheduler: each enabled row is grouped by its own cron; rows sharing a cron fire together,
and a blank/disabled/invalid cron never fires. There is no global schedule."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shortlist.server.db.models import Collection, Event
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.scheduler import schedule_groups


@pytest.fixture
def app(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    # The migration seeds the default 'picked' row with a cron; clear it so each test owns the set.
    with factory() as session:
        session.query(Collection).delete()
        session.commit()
    yield SimpleNamespace(state=SimpleNamespace(sessions=factory))
    engine.dispose()


def _add(factory, slug: str, schedule: str, *, enabled: bool = True) -> None:
    with factory() as session:
        session.add(Collection(slug=slug, name=slug, schedule=schedule, enabled=enabled))
        session.commit()


class TestScheduleGroups:
    def test_rows_sharing_a_cron_group_and_blank_schedules_never_fire(self, app):
        factory = app.state.sessions
        _add(factory, "a", "30 3 * * *")
        _add(factory, "b", "30 3 * * *")  # same cron as a -> one job fires both
        _add(factory, "c", "0 6 * * *")  # its own cron -> its own job
        _add(factory, "d", "")  # no schedule -> never fires

        groups = schedule_groups(app)

        assert set(groups) == {"30 3 * * *", "0 6 * * *"}  # 'd' contributes no job
        assert len(groups["30 3 * * *"]) == 2  # a + b run together
        assert len(groups["0 6 * * *"]) == 1

    def test_disabled_and_invalid_crons_are_skipped_not_crashed(self, app):
        factory = app.state.sessions
        _add(factory, "off", "30 3 * * *", enabled=False)  # disabled -> excluded
        _add(factory, "bad", "not a valid cron")  # invalid -> skipped, never raises
        _add(factory, "good", "0 4 * * *")

        groups = schedule_groups(app)

        assert set(groups) == {"0 4 * * *"}


class TestSchedulerLateness:
    @pytest.mark.parametrize("rebuild", [False, True], ids=["initial", "rebuilt"])
    @pytest.mark.parametrize("late_seconds", [1.116242, 29, 30])
    def test_row_runs_when_no_more_than_thirty_seconds_late(
        self, app: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, rebuild: bool, late_seconds: float
    ) -> None:
        from apscheduler.events import EVENT_JOB_EXECUTED
        from apscheduler.executors import base

        from shortlist.server.scheduler import build_scheduler, rebuild_schedule

        cron = "0 2 * * *"
        _add(app.state.sessions, "a", cron)
        _add(app.state.sessions, "b", cron)
        _add(app.state.sessions, "other", "0 6 * * *")
        start_run = AsyncMock()
        app.state.run_service = SimpleNamespace(start_run=start_run)
        now = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        monkeypatch.setattr(base, "datetime", SimpleNamespace(now=lambda tz: now))

        async def execute() -> None:
            scheduler = build_scheduler(app)
            app.state.scheduler = scheduler
            # Starting paused applies APScheduler's defaults without firing unrelated jobs.
            scheduler.start(paused=True)
            try:
                if rebuild:
                    _add(app.state.sessions, "added", cron)
                    rebuild_schedule(app)
                job = scheduler.get_job(f"row-schedule::{cron}")
                assert job is not None
                events = await base.run_coroutine_job(
                    job, "default", [now - timedelta(seconds=late_seconds)], "test.scheduler"
                )

                assert [(event.job_id, event.code) for event in events] == [(job.id, EVENT_JOB_EXECUTED)]
                start_run.assert_awaited_once_with(
                    trigger="schedule", dry_run=False, collection_ids=schedule_groups(app)[cron]
                )
                # Row runs have no grace at all (owner decision 2026-10-02); every timer keeps 30 seconds.
                for scheduled in scheduler.get_jobs():
                    expected = None if scheduled.id.startswith("row-schedule::") else 30
                    assert scheduled.misfire_grace_time == expected, scheduled.id
            finally:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)

        asyncio.run(execute())

    @staticmethod
    def _missed_events(app: SimpleNamespace) -> list[Event]:
        with app.state.sessions() as session:
            return session.query(Event).filter(Event.scope == "schedule.missed").all()

    @staticmethod
    async def _submit(scheduler, job, run_times: list[datetime]) -> None:
        """The executor's own submit path, so APScheduler applies its lateness check and dispatches the event."""
        executor = scheduler._lookup_executor("default")
        executor.submit_job(job, run_times)
        for _ in range(100):
            if not executor._pending_futures:
                break
            await asyncio.sleep(0)
        assert not executor._pending_futures

    def test_a_row_run_still_starts_when_it_is_minutes_late(
        self, app: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Owner decision 2026-10-02: a row run is never skipped for being late."""
        from apscheduler.executors import base

        from shortlist.server.scheduler import build_scheduler
        from tests.conftest import freeze_clock

        cron = "0 2 * * *"
        _add(app.state.sessions, "a", cron)
        start_run = AsyncMock()
        app.state.run_service = SimpleNamespace(start_run=start_run)
        due = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        freeze_clock(monkeypatch, base, due + timedelta(minutes=10))

        async def execute() -> None:
            scheduler = build_scheduler(app)
            scheduler.start(paused=True)
            try:
                await self._submit(scheduler, scheduler.get_job(f"row-schedule::{cron}"), [due])
            finally:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)

        asyncio.run(execute())

        start_run.assert_awaited_once_with(trigger="schedule", dry_run=False, collection_ids=schedule_groups(app)[cron])
        assert self._missed_events(app) == []

    def test_a_non_row_job_later_than_the_grace_is_still_skipped_and_recorded(
        self, app: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apscheduler.executors import base

        from shortlist.server import scheduler as scheduler_module
        from tests.conftest import freeze_clock

        queue = AsyncMock()
        monkeypatch.setattr(scheduler_module, "_queue_and_drain", queue)
        due = datetime(2026, 1, 1, 4, 17, tzinfo=UTC)
        now = due + timedelta(seconds=31)
        freeze_clock(monkeypatch, base, now)
        freeze_clock(monkeypatch, scheduler_module, now)

        async def execute() -> None:
            scheduler = scheduler_module.build_scheduler(app)
            scheduler.start(paused=True)
            try:
                job = scheduler.get_job(scheduler_module.WATCH_SYNC_JOB_ID)
                assert job.misfire_grace_time == 30
                await self._submit(scheduler, job, [due])
            finally:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)

        asyncio.run(execute())

        queue.assert_not_awaited()
        assert [(e.level, e.message) for e in self._missed_events(app)] == [
            (
                "warning",
                {
                    "job": "watch-sync",
                    "name": "Sync watch history",
                    "scheduled_for": "2026-01-01T04:17:00+00:00",
                    "late_by_s": 31,
                },
            )
        ]

    def test_the_missed_job_audit_is_written_off_the_event_loop(
        self, app: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A miss happens when the loop is already congested; a blocking SQLite commit there would stall it further."""
        from apscheduler.executors import base

        from shortlist.server import scheduler as scheduler_module
        from shortlist.server.services import audit
        from tests.conftest import freeze_clock

        monkeypatch.setattr(scheduler_module, "_queue_and_drain", AsyncMock())
        due = datetime(2026, 1, 1, 4, 17, tzinfo=UTC)
        now = due + timedelta(seconds=31)
        freeze_clock(monkeypatch, base, now)
        freeze_clock(monkeypatch, scheduler_module, now)
        real_write_audit = audit.write_audit
        written_on_loop: list[bool] = []

        def spy(*args, **kwargs) -> None:
            try:
                asyncio.get_running_loop()
                written_on_loop.append(True)
            except RuntimeError:
                written_on_loop.append(False)
            real_write_audit(*args, **kwargs)

        monkeypatch.setattr(audit, "write_audit", spy)

        async def execute() -> None:
            scheduler = scheduler_module.build_scheduler(app)
            scheduler.start(paused=True)
            try:
                await self._submit(scheduler, scheduler.get_job(scheduler_module.WATCH_SYNC_JOB_ID), [due])
                deadline = asyncio.get_running_loop().time() + 5
                while not self._missed_events(app) and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
            finally:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)

        asyncio.run(execute())

        assert written_on_loop == [False]
        assert [(e.level, e.message) for e in self._missed_events(app)] == [
            (
                "warning",
                {
                    "job": "watch-sync",
                    "name": "Sync watch history",
                    "scheduled_for": "2026-01-01T04:17:00+00:00",
                    "late_by_s": 31,
                },
            )
        ]

    def test_several_missed_row_fire_times_start_one_run(
        self, app: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An event loop blocked across several fire times catches up with ONE run, for the newest of them."""
        from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_MISSED, JobExecutionEvent
        from apscheduler.executors import base as executor_base
        from apscheduler.schedulers import base as scheduler_base

        from shortlist.server.scheduler import build_scheduler
        from tests.conftest import freeze_clock

        cron = "0 2 * * *"
        _add(app.state.sessions, "a", cron)
        start_run = AsyncMock()
        app.state.run_service = SimpleNamespace(start_run=start_run)
        outcomes: list[tuple[int, datetime]] = []

        def record(event: JobExecutionEvent) -> None:
            outcomes.append((event.code, event.scheduled_run_time))

        async def execute() -> datetime:
            scheduler = build_scheduler(app)
            scheduler.add_listener(record, EVENT_JOB_EXECUTED | EVENT_JOB_MISSED)
            scheduler.start(paused=True)
            try:
                job = scheduler.get_job(f"row-schedule::{cron}")
                first = job.trigger.get_next_fire_time(None, datetime(2026, 1, 1, tzinfo=UTC))
                second = job.trigger.get_next_fire_time(first, first)
                third = job.trigger.get_next_fire_time(second, second)
                job.modify(next_run_time=first)
                now = third + timedelta(minutes=10)
                freeze_clock(monkeypatch, scheduler_base, now)
                freeze_clock(monkeypatch, executor_base, now)
                # The real wakeup path: `_process_jobs` finds all three fire times due at once.
                scheduler.resume()
                executor = scheduler._lookup_executor("default")
                for _ in range(100):
                    await asyncio.sleep(0)
                    if outcomes and not executor._pending_futures:
                        break
                return third
            finally:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)

        newest = asyncio.run(execute())

        assert outcomes == [(EVENT_JOB_EXECUTED, newest)]
        start_run.assert_awaited_once_with(trigger="schedule", dry_run=False, collection_ids=schedule_groups(app)[cron])

    def test_row_jobs_keep_the_never_skip_rule_after_a_schedule_rebuild(
        self, app: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from apscheduler.executors import base

        from shortlist.server.scheduler import build_scheduler, rebuild_schedule
        from tests.conftest import freeze_clock

        cron = "0 2 * * *"
        _add(app.state.sessions, "a", cron)
        start_run = AsyncMock()
        app.state.run_service = SimpleNamespace(start_run=start_run)
        due = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)
        freeze_clock(monkeypatch, base, due + timedelta(minutes=10))

        async def execute() -> None:
            scheduler = build_scheduler(app)
            app.state.scheduler = scheduler
            scheduler.start(paused=True)
            try:
                # One cron re-registered with a new member, and one registered for the first time.
                _add(app.state.sessions, "b", cron)
                _add(app.state.sessions, "c", "30 5 * * 1")
                rebuild_schedule(app)
                grace = {job.id: job.misfire_grace_time for job in scheduler.get_jobs()}
                rows = {job_id: g for job_id, g in grace.items() if job_id.startswith("row-schedule::")}
                assert rows == {f"row-schedule::{cron}": None, "row-schedule::30 5 * * 1": None}
                assert {g for job_id, g in grace.items() if job_id not in rows} == {30}
                await self._submit(scheduler, scheduler.get_job(f"row-schedule::{cron}"), [due])
            finally:
                scheduler.shutdown(wait=False)
                await asyncio.sleep(0)

        asyncio.run(execute())

        start_run.assert_awaited_once_with(trigger="schedule", dry_run=False, collection_ids=schedule_groups(app)[cron])
        assert len(schedule_groups(app)[cron]) == 2
        assert self._missed_events(app) == []

    def test_a_missed_timer_job_is_named_from_the_jobs_catalogue(self, app: SimpleNamespace) -> None:
        from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

        from shortlist.server.scheduler import build_scheduler

        scheduler = build_scheduler(app)
        due = datetime(2026, 1, 1, 4, 17, tzinfo=UTC)

        scheduler._dispatch_event(JobExecutionEvent(EVENT_JOB_MISSED, "watch-sync", "default", due))

        [event] = self._missed_events(app)
        assert event.message["job"] == "watch-sync"
        assert event.message["name"] == "Sync watch history"
        assert event.message["scheduled_for"] == "2026-01-01T04:17:00+00:00"

    @pytest.mark.parametrize("job_id", ["jobs.drain", "jobs.sweep"])
    def test_a_drain_tick_missed_or_skipped_writes_nothing(self, app: SimpleNamespace, job_id: str) -> None:
        """The worker ticks every minute; `jobs.drain` hit max_instances 27 times in 8 days on a live server."""
        from apscheduler.events import (
            EVENT_JOB_MAX_INSTANCES,
            EVENT_JOB_MISSED,
            JobExecutionEvent,
            JobSubmissionEvent,
        )

        from shortlist.server.scheduler import build_scheduler

        _add(app.state.sessions, "a", "0 2 * * *")
        scheduler = build_scheduler(app)
        due = datetime(2026, 1, 1, 2, 0, tzinfo=UTC)

        scheduler._dispatch_event(JobExecutionEvent(EVENT_JOB_MISSED, job_id, "default", due))
        # A row run that could not start because the last one is still going is not a missed schedule.
        for skipped in (job_id, "row-schedule::0 2 * * *"):
            scheduler._dispatch_event(JobSubmissionEvent(EVENT_JOB_MAX_INSTANCES, skipped, "default", [due]))

        assert self._missed_events(app) == []

    def test_the_listener_is_registered_once_after_a_schedule_rebuild(self, app: SimpleNamespace) -> None:
        from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent

        from shortlist.server.scheduler import build_scheduler, rebuild_schedule

        cron = "0 2 * * *"
        _add(app.state.sessions, "a", cron)
        app.state.scheduler = build_scheduler(app)
        rebuild_schedule(app)
        rebuild_schedule(app)

        app.state.scheduler._dispatch_event(
            JobExecutionEvent(EVENT_JOB_MISSED, f"row-schedule::{cron}", "default", datetime(2026, 1, 1, 2, tzinfo=UTC))
        )

        assert len(self._missed_events(app)) == 1

    def test_a_failed_audit_write_does_not_raise_out_of_the_listener(self, app: SimpleNamespace) -> None:
        """APScheduler swallows listener errors itself, so the listener is called directly to prove its own guard."""
        from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
        from loguru import logger

        from shortlist.server.scheduler import build_scheduler

        scheduler = build_scheduler(app)
        [(listener, mask)] = scheduler._listeners
        assert mask == EVENT_JOB_MISSED

        def locked():
            raise RuntimeError("database is locked")

        app.state.sessions = locked
        lines: list[str] = []
        sink = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            listener(JobExecutionEvent(EVENT_JOB_MISSED, "db-backup", "default", datetime(2026, 1, 1, 3, tzinfo=UTC)))
        finally:
            logger.remove(sink)

        assert any("db-backup" in line for line in lines), lines


class TestBuildScope:
    """A per-row scheduled run rebuilds ONLY its rows (`build_only`), but the config still exposes
    EVERY row to privacy classification, the share-filter sync, the sweep, and shelf promotion — so
    an out-of-scope SHARED row is never misclassified and over-hidden (the leak-safe guarantee)."""

    def _cfg(self, build_only):
        from shortlist.engine.models import EngineConfig, RowSpec

        personal = RowSpec(slug="picked", name_template="", size=10)
        shared = RowSpec(slug="popular", name_template="Popular", size=10, shared=True)
        cfg = EngineConfig(rows=[personal, shared], rows_defined=True, build_only=build_only)
        return cfg, personal, shared

    def test_scope_limits_should_build_but_not_the_row_view(self):
        cfg, personal, shared = self._cfg(frozenset({"picked"}))
        assert cfg.should_build(personal) is True
        assert cfg.should_build(shared) is False  # out of scope -> not rebuilt this run
        # ...yet BOTH stay visible to the classification/promotion helpers (they iterate the full lists),
        # so the out-of-scope shared row is never dropped from the "what's shared" set and over-hidden.
        assert cfg.per_person_rows() == [personal]
        assert cfg.shared_rows() == [shared]

    def test_a_full_run_builds_every_row(self):
        cfg, personal, shared = self._cfg(None)
        assert cfg.should_build(personal) is True
        assert cfg.should_build(shared) is True


class TestScheduledWorkIsDurable:
    """The three scheduled tasks that keep a server correct BETWEEN runs — roster reconcile, watch
    history, backups — were bare coroutines whose only failure path was `logger.exception`.

    That made them the least observable code in the app and the most consequential when broken: the
    roster sync is what notices a new account and writes the filters that stop them seeing everyone
    else's rows, and the backup is the one thing nobody checks until they need it.
    """

    @pytest.fixture(autouse=True)
    def _app_state(self, tmp_path: Path):
        """A real `app.state` — sessions, run_service, config_dir — so the handlers run for real."""
        from starlette.testclient import TestClient

        from shortlist.server.main import create_app

        application = create_app(config_dir=tmp_path / "live")
        with TestClient(application):
            self._state = application.state
            yield

    def _fire(self, app, job_id: str):
        """Run the scheduler job registered under `job_id`, synchronously."""
        import asyncio

        from shortlist.server.scheduler import build_scheduler

        job = build_scheduler(app).get_job(job_id)
        assert job is not None, f"no scheduled job {job_id!r}"
        asyncio.run(job.func())

    @pytest.mark.parametrize(
        ("job_id", "kind"),
        [
            ("user-sync", "sync.users"),
            ("watch-sync", "sync.history"),
            ("db-backup", "backup.take"),
            ("privacy-sync", "privacy.sync"),
            ("maintenance-prune", "maintenance.prune"),
        ],
    )
    def test_each_scheduled_task_lands_on_the_queue(self, app, job_id, kind, monkeypatch):
        from shortlist.server.services import jobs

        queued: list[tuple[str, dict]] = []
        monkeypatch.setattr(jobs, "enqueue", lambda sessions, k, payload=None, **kw: queued.append((k, payload or {})))

        async def no_drain(state, reason):
            return None

        monkeypatch.setattr(jobs, "drain_now", no_drain)

        self._fire(app, job_id)

        assert [k for k, _ in queued] == [kind]
        if kind == "privacy.sync":
            # Only a scheduled pass may count as quiet and stay out of Recent.
            assert queued[0][1] == {"scheduled": True}
        if kind == "backup.take":
            # The payload the SUT controls, not just that something was queued: the keep limit comes
            # from settings and a dropped `max_keep` would silently prune to the built-in default.
            assert set(queued[0][1]) == {"label", "max_keep"}

    def test_a_scheduled_privacy_sync_is_not_queued_behind_one_still_waiting(self, app, monkeypatch):
        """Every 30 minutes, and writer jobs wait out a run: a two-hour run used to leave four passes queued,
        run back to back afterwards, each redoing the merge the run itself had just done."""
        from shortlist.server.db.models import Job
        from shortlist.server.services import jobs

        async def no_drain(state, reason):
            return None

        monkeypatch.setattr(jobs, "drain_now", no_drain)

        self._fire(app, "privacy-sync")
        self._fire(app, "privacy-sync")

        with app.state.sessions() as session:
            assert session.query(Job).filter_by(kind="privacy.sync").count() == 1

    def test_a_scheduled_privacy_sync_is_not_queued_beside_one_waiting_to_retry(self, app, monkeypatch):
        """A pass that failed (plex.tv down) goes back to `queued` with its `started_at` kept while it backs
        off, and it re-reads everything when it retries. Counting only never-started jobs queued a fresh
        pass beside it on every tick of an outage: more plex.tv reads while it is failing, more failure cards."""
        from datetime import UTC, datetime

        from shortlist.server.db.models import Job
        from shortlist.server.services import jobs

        async def no_drain(state, reason):
            return None

        monkeypatch.setattr(jobs, "drain_now", no_drain)
        with app.state.sessions() as session:
            session.add(
                Job(
                    kind="privacy.sync",
                    status="queued",
                    attempts=1,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    error="RuntimeError: could not read the plex.tv user list",
                )
            )
            session.commit()

        self._fire(app, "privacy-sync")

        with app.state.sessions() as session:
            assert session.query(Job).filter_by(kind="privacy.sync").count() == 1

    def test_a_scheduled_privacy_sync_is_queued_once_the_last_one_finished(self, app, monkeypatch):
        from datetime import UTC, datetime

        from shortlist.server.db.models import Job
        from shortlist.server.services import jobs

        async def no_drain(state, reason):
            return None

        monkeypatch.setattr(jobs, "drain_now", no_drain)
        with app.state.sessions() as session:
            for status in ("done", "failed"):
                session.add(Job(kind="privacy.sync", status=status, attempts=1, started_at=datetime.now(UTC)))
            session.commit()

        self._fire(app, "privacy-sync")

        with app.state.sessions() as session:
            assert session.query(Job).filter_by(kind="privacy.sync", status="queued").count() == 1

    @pytest.mark.parametrize("kind", ["sync.users", "sync.history", "backup.take", "maintenance.prune"])
    def test_each_handler_actually_runs(self, kind):
        """Mocking `enqueue`/`drain_now` proves the scheduler CALLS the queue and nothing more — it
        passes whether the handler exists, is registered, or raises on its first line.

        That is not hypothetical: `sync.users` shipped reading `state.app`, an attribute nothing ever
        sets, and burned its three attempts every night behind a green suite. This is the probe that
        catches it — enqueue for real, drain for real, and look at what came back.
        """
        import asyncio

        from shortlist.server.db.models import Job
        from shortlist.server.services import jobs

        state = self._state
        job_id = jobs.enqueue(state.sessions, kind, {"label": "test"})

        asyncio.run(jobs.run_pending(state))

        with state.sessions() as session:
            error = session.get(Job, job_id).error or ""
        # Plex is not connected in this fixture, so a RuntimeError saying so is a fine outcome — the
        # handler ran and failed for an environmental reason the queue will retry. An AttributeError or
        # TypeError is not: that is the handler reaching for something that does not exist.
        assert "AttributeError" not in error, error
        assert "TypeError" not in error, error

    def test_a_failure_to_queue_never_kills_the_scheduler(self, app, monkeypatch):
        """A scheduler job that raises stops firing. Whatever goes wrong here, the next tick must
        still come round."""
        from shortlist.server.services import jobs

        monkeypatch.setattr(jobs, "enqueue", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db locked")))

        self._fire(app, "user-sync")  # must not raise


class TestSyncUsersOnAnUnlinkedServer:
    """A nightly false alarm is how an owner learns to ignore the notification bell.

    Making `sync.users` a durable job turned "Plex is not connected yet" from a silent log line into
    three retries, a `failed` job and a bell notification — every night, on any install whose wizard
    is unfinished or whose token was revoked. `sync.history` already treats the same condition as a
    skip; the two must not disagree about what "not connected" means.
    """

    def test_it_skips_rather_than_failing(self, tmp_path: Path):
        import asyncio

        from starlette.testclient import TestClient

        from shortlist.server.db.models import Job
        from shortlist.server.main import create_app
        from shortlist.server.services import jobs

        application = create_app(config_dir=tmp_path / "unlinked")
        with TestClient(application):
            state = application.state
            job_id = jobs.enqueue(state.sessions, "sync.users", {})

            asyncio.run(jobs.run_pending(state))

            with state.sessions() as session:
                job = session.get(Job, job_id)
                status, detail, error = job.status, job.detail, job.error
                raised = session.query(Event).filter_by(scope="job.failed").count()

        assert status == "done", f"a not-connected server must not fail the job ({error})"
        assert "not connected" in detail.lower()
        assert raised == 0, "and must not ring the bell"


class TestPrivacySyncSchedule:
    """The scheduled share-filter re-merge (every 30 minutes by default).

    It exists because the automatic Privacy Check + write gate was removed on 2026-07-16 at the
    owner's request: nothing verifies hiding after the fact any more, so leak-safe write ORDERING is
    the only remaining guarantee. This pass is the cheapest safety net against drift, and it only
    ever makes the server more private.
    """

    def test_it_is_registered_with_a_trigger_by_default(self, app):
        """Asserted against the LIVE scheduler, not the catalogue: `build_scheduler` naming the
        function is not the same claim as APScheduler holding a trigger for it."""
        from shortlist.server.scheduler import PRIVACY_SYNC_JOB_ID, build_scheduler

        job = build_scheduler(app).get_job(PRIVACY_SYNC_JOB_ID)

        # `next_run_time` only exists once the scheduler is STARTED, so registration + a trigger is
        # the whole claim available here — matching how the other schedule tests assert.
        assert job is not None, "the privacy sync is not scheduled"
        assert job.trigger is not None

    def test_the_retention_prune_has_a_timer_of_its_own(self, app):
        """The prune is queued after every run — which prunes nothing on a server that has stopped
        running. A row with a blank cron never fires, and neither does anything while `paused_all`
        is set, so without a schedule of its own `runs`, `events` and the expired cache rows grow
        forever in the same file the nightly backup copies whole and keeps ten of."""
        from shortlist.server.scheduler import MAINTENANCE_PRUNE_JOB_ID, build_scheduler

        job = build_scheduler(app).get_job(MAINTENANCE_PRUNE_JOB_ID)

        assert job is not None, "the retention prune has no schedule — it only runs if runs do"
        assert job.trigger is not None

    def test_the_prune_runs_after_every_other_schedule(self, app):
        """Order matters: it trims runs and events, so a pass that fired before the night's syncs
        and drift check would be trimming a database still being appended to."""
        from shortlist.server.scheduler import DEFAULT_CRONS

        def minutes(cron: str) -> int:
            minute, hour = cron.split()[:2]
            return int(hour) * 60 + int(minute)

        def daily(cron: str) -> bool:
            minute, hour = cron.split()[:2]
            return minute.isdigit() and hour.isdigit()

        # A schedule that repeats through the day (the privacy sync, every 30 minutes) has no "after".
        others = {
            key: minutes(cron) for key, cron in DEFAULT_CRONS.items() if key != "maintenance.prune_cron" and daily(cron)
        }
        assert minutes(DEFAULT_CRONS["maintenance.prune_cron"]) > max(others.values()), others

    def test_the_privacy_sync_runs_every_30_minutes_out_of_the_box(self, app):
        """It also reads the plex.tv account list, so it is what hides everyone's rows from an account
        newly shared with the server. Once a day left such an account able to browse every row in the
        Collections tab for up to 24 hours; a clean pass takes ~20s and writes nothing."""
        from shortlist.server.scheduler import DEFAULT_CRONS

        assert DEFAULT_CRONS["privacy.sync_cron"] == "*/30 * * * *"

    def test_the_drift_check_runs_nightly_out_of_the_box(self, app):
        """Drift is the failure nobody notices — a row left on the wrong shelf stays there until
        someone happens to look. Shipping the thing that repairs it switched off meant the repair
        never happened on the servers that needed it most."""
        from shortlist.server.scheduler import SYNC_CHECK_JOB_ID, build_scheduler

        assert build_scheduler(app).get_job(SYNC_CHECK_JOB_ID) is not None

    def test_setting_a_cron_moves_the_drift_check(self, app):
        from shortlist.server.scheduler import SYNC_CHECK_JOB_ID, build_scheduler
        from shortlist.server.settings_store import SettingsStore

        with app.state.sessions() as session:
            SettingsStore(session).set("sync.check_cron", "0 6 * * *")

        job = build_scheduler(app).get_job(SYNC_CHECK_JOB_ID)
        assert job is not None
        assert "hour='6'" in str(job.trigger)

    def test_clearing_the_drift_check_cron_really_turns_it_off(self, app):
        """The one schedule the UI offers to disable, so "cleared" must not mean "inherit".

        For every other key a blank value means "use the built-in default" — if that applied here the
        "Turn this schedule off" button would write "" and the next resolve would put the job straight
        back, so the switch would silently do nothing.
        """
        from shortlist.server.scheduler import SYNC_CHECK_JOB_ID, build_scheduler
        from shortlist.server.settings_store import SettingsStore

        with app.state.sessions() as session:
            SettingsStore(session).set("sync.check_cron", "")

        assert build_scheduler(app).get_job(SYNC_CHECK_JOB_ID) is None

    def test_a_blank_cron_still_means_inherit_for_every_other_schedule(self, app):
        """The off-switch semantics are scoped to the drift check. Applying them everywhere would turn
        a blank `backup.cron` — which has always meant "use the default" — into "never back up"."""
        from shortlist.server.scheduler import BACKUP_JOB_ID, build_scheduler
        from shortlist.server.settings_store import SettingsStore

        with app.state.sessions() as session:
            SettingsStore(session).set("backup.cron", "")

        assert build_scheduler(app).get_job(BACKUP_JOB_ID) is not None

    def test_a_bad_cron_falls_back_instead_of_crash_looping_the_container(self, app):
        """A typo in a settings box must never stop the app booting."""
        from shortlist.server.scheduler import PRIVACY_SYNC_JOB_ID, build_scheduler
        from shortlist.server.settings_store import SettingsStore

        with app.state.sessions() as session:
            SettingsStore(session).set("privacy.sync_cron", "not a cron")

        job = build_scheduler(app).get_job(PRIVACY_SYNC_JOB_ID)
        assert job is not None and job.trigger is not None


class TestThereIsOneSourceOfTruthForEveryCronDefault:
    """`DEFAULT_CRONS` owns each expression; `settings_store.DEFAULTS` derives blank placeholders
    from it. The second copy that used to sit beside the settings defaults is what let the drift
    check be documented as off-by-default for months while it ran nightly at 05:45."""

    def test_the_settings_defaults_carry_no_cron_literal_of_their_own(self):
        from shortlist.server.scheduler import DEFAULT_CRONS
        from shortlist.server.settings_store import DEFAULTS

        assert {key: DEFAULTS[key] for key in DEFAULT_CRONS} == dict.fromkeys(DEFAULT_CRONS, "")

    def test_every_schedulable_key_is_writable_through_the_settings_api(self):
        """`PUT /api/settings` builds its allowlist from `DEFAULTS`, so a cron the scheduler honours
        but the settings dict has never heard of is a schedule the owner cannot change."""
        from shortlist.server.api.settings import KNOWN_KEYS
        from shortlist.server.scheduler import DEFAULT_CRONS

        assert set(DEFAULT_CRONS) <= KNOWN_KEYS

    def test_an_unset_cron_resolves_to_the_one_declared_default(self, app):
        """The claim a comment cannot make: what each schedule ACTUALLY runs on out of the box."""
        from shortlist.server.scheduler import DEFAULT_CRONS, effective_cron

        assert {key: effective_cron(app, key) for key in DEFAULT_CRONS} == DEFAULT_CRONS


class TestCronResolverEdges:
    """The two ways a settings row can be wrong, both of which run at BOOT."""

    def test_a_malformed_settings_row_does_not_crash_the_container(self, app):
        """`build_scheduler` runs during startup, so anything that raises here is a crash loop — the
        one outcome the resolver's own docstring promises can never happen."""
        from shortlist.server.db.models import Setting
        from shortlist.server.scheduler import build_scheduler

        with app.state.sessions() as session:
            session.add(Setting(key="backup.cron", value={"wrong": "shape"}))
            session.commit()

        assert build_scheduler(app) is not None

    def test_a_typo_leaves_an_off_able_plex_writer_OFF(self, app):
        """The off-able schedules are the ones that WRITE to Plex. Falling back to the built-in time
        on a typo would schedule an unattended write the owner never asked for."""
        from shortlist.server.scheduler import SYNC_CHECK_JOB_ID, build_scheduler
        from shortlist.server.settings_store import SettingsStore

        with app.state.sessions() as session:
            SettingsStore(session).set("sync.check_cron", "not a cron")

        assert build_scheduler(app).get_job(SYNC_CHECK_JOB_ID) is None

    def test_a_typo_on_a_normal_schedule_still_falls_back_to_its_default(self, app):
        """Only the off-able keys change direction — a broken backup cron must still back up."""
        from shortlist.server.scheduler import BACKUP_JOB_ID, build_scheduler
        from shortlist.server.settings_store import SettingsStore

        with app.state.sessions() as session:
            SettingsStore(session).set("backup.cron", "not a cron")

        assert build_scheduler(app).get_job(BACKUP_JOB_ID) is not None


class TestRowVisibilitySchedule:
    """The midnight tick behind "When it appears" (issue #102).

    Rows build at 03:30. If a run were the only thing that turned a row over, a Monday row would sit
    on people's Home until 03:30 Tuesday — and a row rebuilding weekly would be days late. So this
    schedule is not a convenience; without it a day schedule does not mean what the screen says.
    """

    def test_it_is_registered_with_a_trigger_by_default(self, app):
        from shortlist.server.scheduler import ROW_VISIBILITY_JOB_ID, build_scheduler

        job = build_scheduler(app).get_job(ROW_VISIBILITY_JOB_ID)

        assert job is not None, "nothing would ever apply a row's day schedule"
        assert job.trigger is not None

    def test_it_fires_at_midnight(self, app):
        """Not 03:30 with the runs, and not an arbitrary quiet minute: a day schedule that turned over
        at 04:17 would show a Monday row for four hours of Tuesday."""
        from shortlist.server.scheduler import DEFAULT_CRONS

        assert DEFAULT_CRONS["rows.visibility_cron"] == "0 0 * * *"


class TestCrontabWeekdays:
    """Cron counts weekdays from 0 = Sunday; APScheduler 3's `from_crontab` counts from 0 = Monday, so
    every numeric weekday fired a day late (issue #123: `0 4 * * 1,4` ran Tuesday and Friday)."""

    @pytest.mark.parametrize(
        ("cron", "expected"),
        [
            ("0 4 * * 1,4", ["Mon", "Thu", "Mon", "Thu"]),
            ("0 4 * * 0", ["Sun", "Sun", "Sun", "Sun"]),
            ("0 4 * * 7", ["Sun", "Sun", "Sun", "Sun"]),
            ("0 4 * * 1-5", ["Mon", "Tue", "Wed", "Thu"]),
            ("0 4 * * 0-2", ["Mon", "Tue", "Sun", "Mon"]),
            ("0 4 * * 5-7", ["Fri", "Sat", "Sun", "Fri"]),
            ("0 4 * * 0,6", ["Sat", "Sun", "Sat", "Sun"]),
            ("0 4 * * */2", ["Tue", "Thu", "Sat", "Sun"]),
            ("0 4 * * mon,thu", ["Mon", "Thu", "Mon", "Thu"]),
            ("0 4 * * *", ["Mon", "Tue", "Wed", "Thu"]),
            # A named range may END on Sunday. `sun` is 0, so read literally `sat-sun` is 6-0 and was
            # refused, silently stopping a schedule APScheduler's own names had always accepted.
            ("0 4 * * sat-sun", ["Sat", "Sun", "Sat", "Sun"]),
            ("0 4 * * fri-sun", ["Fri", "Sat", "Sun", "Fri"]),
            ("0 4 * * mon-sun", ["Mon", "Tue", "Wed", "Thu"]),
            ("0 4 * * SAT-SUN", ["Sat", "Sun", "Sat", "Sun"]),
            ("0 4 * * sun-sat", ["Mon", "Tue", "Wed", "Thu"]),
        ],
    )
    def test_weekdays_fire_on_the_day_cron_means_when_numbered_from_sunday(self, cron, expected):
        from datetime import UTC, datetime, timedelta

        from shortlist.server.scheduler import crontab_trigger

        trigger = crontab_trigger(cron, timezone=UTC)
        now, previous, fired = datetime(2026, 9, 6, 12, tzinfo=UTC), None, []  # a Sunday, after 04:00
        for _ in expected:
            previous = trigger.get_next_fire_time(previous, now)
            fired.append(previous.strftime("%a"))
            now = previous + timedelta(seconds=1)
        assert fired == expected

    @pytest.mark.parametrize(
        "cron",
        ["0 4 * * 8", "0 4 * * 5-2", "0 4 * *", "0 4 * * funday", "0 4 * * 1/", "0 4 * * */0", "0 4 * * sat-mon"],
    )
    def test_an_invalid_weekday_raises_value_error_when_parsed(self, cron):
        from shortlist.server.scheduler import crontab_trigger

        with pytest.raises(ValueError):
            crontab_trigger(cron)

    def test_nothing_calls_from_crontab_directly_when_the_wrapper_exists(self):
        root = Path(__file__).resolve().parents[2] / "shortlist"
        offenders = [str(p) for p in root.rglob("*.py") if "from_crontab(" in p.read_text()]
        assert offenders == []


class TestCrontabDayOfMonthAndWeekday:
    """Cron runs a job when the day of the month OR the weekday matches, if both are restricted.
    APScheduler requires both, so `0 4 1 * 1` ran only on a 1st that was a Monday (next: 2027-02-01),
    not every Monday and every 1st. A field that starts with `*` is not a restriction (Vixie cron's
    DOM_STAR/DOW_STAR), so `*/2` still combines with AND."""

    @staticmethod
    def _fires(cron: str, count: int) -> list[str]:
        from datetime import UTC, datetime, timedelta

        from shortlist.server.scheduler import crontab_trigger

        trigger = crontab_trigger(cron, timezone=UTC)
        now, previous, fired = datetime(2026, 9, 6, 12, tzinfo=UTC), None, []  # a Sunday, after 04:00
        for _ in range(count):
            previous = trigger.get_next_fire_time(previous, now)
            fired.append(previous.strftime("%a %m-%d"))
            now = previous + timedelta(seconds=1)
        return fired

    def test_both_restricted_fires_on_either(self):
        assert self._fires("0 4 1 * 1", 6) == [
            "Mon 09-07",
            "Mon 09-14",
            "Mon 09-21",
            "Mon 09-28",
            "Thu 10-01",
            "Mon 10-05",
        ]

    def test_a_list_of_days_or_a_weekday_range(self):
        assert self._fires("0 4 12,13 * sat-sun", 5) == [
            "Sat 09-12",
            "Sun 09-13",
            "Sat 09-19",
            "Sun 09-20",
            "Sat 09-26",
        ]

    def test_a_starred_day_of_month_still_means_both(self):
        """`*/2` is 1, 3, 5, … AND Monday — Mondays on odd dates only."""
        assert self._fires("0 4 */2 * 1", 3) == ["Mon 09-07", "Mon 09-21", "Mon 10-05"]

    def test_a_starred_weekday_still_means_both(self):
        """`*/2` weekdays are Sun, Tue, Thu, Sat; with the 1st, only a 1st that falls on one of them."""
        assert self._fires("0 4 1 * */2", 2) == ["Thu 10-01", "Sun 11-01"]

    def test_only_a_day_of_month_fires_on_that_day(self):
        assert self._fires("0 4 1 * *", 2) == ["Thu 10-01", "Sun 11-01"]

    def test_an_invalid_day_of_month_still_raises_value_error(self):
        from shortlist.server.scheduler import crontab_trigger

        with pytest.raises(ValueError):
            crontab_trigger("0 4 32 * 1")
