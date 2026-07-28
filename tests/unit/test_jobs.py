"""The durable job queue: claim, retry, recover, and stay out of a run's way.

Every maintenance action used to be a fire-and-forget executor call — no record, no retry, nowhere
an operator would see it fail. A disable cleanup lost to a Plex outage was never retried by anything,
because no run revisits a disabled user, so those rows stayed on Plex for ever.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from shortlist.server.db.models import Event, Job
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services import jobs


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    return make_session_factory(make_engine(tmp_path))


@pytest.fixture
def state(sessions):
    """Minimal app.state: the queue only needs sessions and (optionally) a run_service."""
    return SimpleNamespace(sessions=sessions, run_service=None)


def drain(state) -> int:
    """`run_pending` is async; these tests are sync, so drive one full drain per call."""
    return asyncio.run(jobs.run_pending(state))


@pytest.fixture(autouse=True)
def _isolate_handlers():
    """Each test registers its own handlers without leaking into the next."""
    original = dict(jobs._HANDLERS)
    yield
    jobs._HANDLERS.clear()
    jobs._HANDLERS.update(original)


def _job(sessions, job_id: int) -> Job:
    with sessions() as session:
        return session.get(Job, job_id)


class TestEnqueue:
    def test_an_unknown_kind_is_refused_at_enqueue_time(self, sessions):
        """Better to fail at the call site than to queue something nothing can ever run."""
        with pytest.raises(ValueError, match="unknown job kind"):
            jobs.enqueue(sessions, "does.not.exist")

    def test_a_queued_job_runs_and_records_its_result(self, state, sessions):
        jobs.handler("t.ok")(lambda st, payload: {"detail": f"did {payload['what']}"})
        job_id = jobs.enqueue(sessions, "t.ok", {"what": "the thing"})

        assert drain(state) == 1

        job = _job(sessions, job_id)
        assert job.status == "done"
        assert job.detail == "did the thing"
        assert job.finished_at is not None


class TestRetry:
    def test_a_failure_goes_back_to_queued_while_attempts_remain(self, state, sessions):
        jobs.handler("t.flaky")(lambda st, payload: (_ for _ in ()).throw(RuntimeError("Plex is down")))
        job_id = jobs.enqueue(sessions, "t.flaky")

        drain(state)

        job = _job(sessions, job_id)
        assert job.status == "queued"  # will be retried
        assert job.attempts == 1
        assert "Plex is down" in job.error

    def test_it_gives_up_after_max_attempts_and_raises_a_notification(self, state, sessions):
        """A job that silently stops retrying is the old bug in a new hat — the operator must be told."""
        jobs.handler("t.doomed")(lambda st, payload: (_ for _ in ()).throw(RuntimeError("nope")))
        job_id = jobs.enqueue(sessions, "t.doomed", max_attempts=1)

        drain(state)

        assert _job(sessions, job_id).status == "failed"
        with sessions() as session:
            assert session.query(Event).filter_by(scope="job.failed").count() == 1

    def test_a_failed_job_backs_off_instead_of_hammering_a_sick_server(self, state, sessions):
        jobs.handler("t.flaky")(lambda st, payload: (_ for _ in ()).throw(RuntimeError("boom")))
        jobs.enqueue(sessions, "t.flaky")

        assert drain(state) == 1
        assert drain(state) == 0  # still inside the backoff window


class TestRecovery:
    def test_a_job_abandoned_by_a_dead_process_is_requeued(self, sessions):
        """The whole point of the table: work must survive the process that started it."""
        jobs.handler("t.ok")(lambda st, payload: {})
        job_id = jobs.enqueue(sessions, "t.ok")
        with sessions() as session:  # simulate a process killed mid-job
            job = session.get(Job, job_id)
            job.status = "running"
            job.started_at = datetime.now(UTC) - jobs.STALE_AFTER - timedelta(minutes=1)
            session.commit()

        assert jobs.recover_stale(sessions) == 1
        assert _job(sessions, job_id).status == "queued"

    def test_boot_requeues_immediately_without_waiting_out_the_age_test(self, sessions):
        """At startup nothing is running in THIS process, so a `running` row is definitionally
        abandoned. Making a restart wait out STALE_AFTER is 30 minutes of doing nothing."""
        jobs.handler("t.ok")(lambda st, payload: {})
        job_id = jobs.enqueue(sessions, "t.ok")
        with sessions() as session:
            job = session.get(Job, job_id)
            job.status = "running"
            job.started_at = datetime.now(UTC)  # started a moment ago, then the process died
            session.commit()

        assert jobs.recover_stale(sessions, boot=True) == 1
        assert _job(sessions, job_id).status == "queued"

    def test_a_job_still_genuinely_running_is_left_alone(self, sessions):
        """Requeuing a merely-slow job would run it twice."""
        jobs.handler("t.ok")(lambda st, payload: {})
        job_id = jobs.enqueue(sessions, "t.ok")
        with sessions() as session:
            job = session.get(Job, job_id)
            job.status = "running"
            job.started_at = datetime.now(UTC)
            session.commit()

        assert jobs.recover_stale(sessions) == 0
        assert _job(sessions, job_id).status == "running"


class TestPlexContention:
    def test_no_job_starts_while_a_run_is_writing_to_plex(self, sessions):
        """Runs and jobs are separate systems sharing one Plex and one throttled plex.tv. Interleaving
        their writes risks a job merging a share filter from a roster a run is mid-way through changing."""
        jobs.handler("t.ok")(lambda st, payload: {})
        jobs.enqueue(sessions, "t.ok")
        busy = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(is_running=lambda: True))

        assert drain(busy) == 0

    def test_a_cancelled_run_still_counts_as_busy(self, sessions):
        """Cancellation is cooperative: the engine stops taking new users, then falls through to the
        privacy merge and promote for everyone already delivered. Treating "cancel requested" as
        "finished" opens the queue inside the exact merge->promote window rule 1 protects."""
        from shortlist.server.services.run_service import RunService

        jobs.handler("t.ok")(lambda st, payload: {})
        jobs.enqueue(sessions, "t.ok")
        service = RunService.__new__(RunService)
        cancel = __import__("threading").Event()
        cancel.set()  # cancel requested — but the run is still merging filters and promoting
        service._cancels = {7: cancel}
        cancelling = SimpleNamespace(sessions=sessions, run_service=service)

        assert drain(cancelling) == 0

    def test_the_job_runs_once_the_run_finishes(self, sessions):
        jobs.handler("t.ok")(lambda st, payload: {})
        jobs.enqueue(sessions, "t.ok")
        idle = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(is_running=lambda: False))

        assert drain(idle) == 1


class TestHandlers:
    """The registered job kinds — what a real server actually queues.

    All three are removal-only writes to Plex, which is what makes them safe to replay after a crash
    and why they must be durable: each used to be fire-and-forget, so a Plex outage at the moment of
    the edit lost the work with nothing left to revisit it.
    """

    def test_every_registered_kind_is_reachable(self):
        for kind in ("sync.check", "privacy.sync", "user.cleanup", "user.hide", "row.reconcile"):
            assert kind in jobs._HANDLERS, kind

    def test_only_safe_kinds_are_triggerable_from_the_ui(self):
        """`user.cleanup`, `user.hide` and `row.reconcile` all take a target and DELETE or hide that
        target's rows. A generic "run a job" button must never be able to aim them."""
        assert set(jobs.KINDS) == {"sync.check", "privacy.sync"}
        for destructive in ("user.cleanup", "user.hide", "row.reconcile"):
            assert destructive not in jobs.KINDS, destructive

    def test_hiding_a_paused_user_takes_every_row_off_every_surface(self, sessions):
        """Pause keeps the collection and its label — so everyone else's exclude still matches, and
        unpausing is a re-promote rather than a full LLM rebuild."""
        collection = SimpleNamespace(title="✨ Movies Picked for You")
        plex = SimpleNamespace(
            sections=lambda: ["movies"],
            find_owned_collections=lambda section, label: [collection] if label == "shortlist_sarah" else [],
            demote_all=lambda c: True,
        )
        ctx = SimpleNamespace(plex=plex, config=SimpleNamespace(label_prefix="shortlist"))
        state = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run: ctx))

        result = jobs._HANDLERS["user.hide"](state, {"slug": "sarah"})

        assert result["hidden"] == ["✨ Movies Picked for You"]
        with sessions() as session:
            assert session.query(Event).filter_by(scope="user.pause.hide").count() == 1

    def test_hiding_is_idempotent_when_the_rows_are_already_down(self, sessions):
        """Converge re-runs nightly over every collection; a no-op must cost reads, not writes."""
        collection = SimpleNamespace(title="✨ Movies Picked for You")
        plex = SimpleNamespace(
            sections=lambda: ["movies"],
            find_owned_collections=lambda section, label: [collection],
            demote_all=lambda c: False,  # already claims nothing
        )
        ctx = SimpleNamespace(plex=plex, config=SimpleNamespace(label_prefix="shortlist"))
        state = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run: ctx))

        assert jobs._HANDLERS["user.hide"](state, {"slug": "sarah"})["hidden"] == []
