"""Per-row scheduler: each enabled row is grouped by its own cron; rows sharing a cron fire together,
and a blank/disabled/invalid cron never fires. There is no global schedule."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
        [("user-sync", "sync.users"), ("watch-sync", "sync.history"), ("db-backup", "backup.take")],
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
        if kind == "backup.take":
            # The payload the SUT controls, not just that something was queued: the keep limit comes
            # from settings and a dropped `max_keep` would silently prune to the built-in default.
            assert set(queued[0][1]) == {"label", "max_keep"}

    @pytest.mark.parametrize("kind", ["sync.users", "sync.history", "backup.take"])
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
