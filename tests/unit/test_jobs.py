"""The durable job queue: claim, retry, recover, and stay out of a run's way.

Every maintenance action used to be a fire-and-forget executor call — no record, no retry, nowhere
an operator would see it fail. A disable cleanup lost to a Plex outage was never retried by anything,
because no run revisits a disabled user, so those rows stayed on Plex for ever.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.delivery import row_marker
from shortlist.engine.models import EngineConfig, RowSpec
from shortlist.server.db.models import Event, Job
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services import jobs
from shortlist.server.settings_store import SettingsStore


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


@pytest.fixture
def sessions_state_with_cap(sessions):
    """An app.state whose `jobs.max_parallel_readonly` is pinned to 1."""
    from shortlist.server.settings_store import SettingsStore

    with sessions() as session:
        SettingsStore(session).set("jobs.max_parallel_readonly", 1)
    return SimpleNamespace(sessions=sessions, run_service=None)


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


class TestReaderWriterConcurrency:
    """Read-only jobs run together; anything that writes to Plex does not.

    Share-filter writes are read-modify-write MERGES (plex-safety rule 3). Two running at once both
    read the same "before" and the second silently drops the first's excludes — a row stays visible
    to someone it should be hidden from, with nothing to catch it since the Privacy Check was
    removed. That is what the writer lock exists for, so it is asserted directly rather than inferred
    from a count.
    """

    @staticmethod
    def _tracker():
        """A handler factory recording overlap: max concurrent, and the order they ran."""
        state = {"live": 0, "peak": 0, "order": []}

        def make(name: str, delay: float = 0.05):
            async def handler(st, payload):
                state["live"] += 1
                state["peak"] = max(state["peak"], state["live"])
                state["order"].append(name)
                await asyncio.sleep(delay)
                state["live"] -= 1
                return {}

            return handler

        return state, make

    def test_read_only_jobs_run_at_the_same_time(self, sessions, state):
        tracked, make = self._tracker()
        jobs._HANDLERS["sync.history"] = make("history")
        jobs._HANDLERS["backup.take"] = make("backup")
        jobs.enqueue(sessions, "sync.history")
        jobs.enqueue(sessions, "backup.take")

        assert drain(state) == 2
        assert tracked["peak"] == 2, "read-only jobs should not be queueing behind each other"

    def test_plex_writers_never_overlap(self, sessions, state):
        tracked, make = self._tracker()
        jobs._HANDLERS["privacy.sync"] = make("privacy")
        jobs._HANDLERS["sync.check"] = make("check")
        jobs.enqueue(sessions, "privacy.sync")
        jobs.enqueue(sessions, "sync.check")

        assert drain(state) == 2
        assert tracked["peak"] == 1, "two share-filter merges ran at once — one of them lost its excludes"

    def test_a_reader_and_a_writer_may_overlap(self, sessions, state):
        # Only writers contend. Backing up the database while filters merge is harmless.
        tracked, make = self._tracker()
        jobs._HANDLERS["privacy.sync"] = make("privacy")
        jobs._HANDLERS["backup.take"] = make("backup")
        jobs.enqueue(sessions, "privacy.sync")
        jobs.enqueue(sessions, "backup.take")

        assert drain(state) == 2
        assert tracked["peak"] == 2

    def test_a_run_blocks_writers_but_not_readers(self, sessions):
        """The whole queue used to stop for the length of a run, so a server that ran for an hour did
        no maintenance at all in that time."""
        tracked, make = self._tracker()
        jobs._HANDLERS["privacy.sync"] = make("privacy")
        jobs._HANDLERS["backup.take"] = make("backup")
        jobs.enqueue(sessions, "privacy.sync")
        jobs.enqueue(sessions, "backup.take")
        busy = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(is_running=lambda: True))

        assert drain(busy) == 1
        assert tracked["order"] == ["backup"], "the writer ran while a run held Plex"
        # The writer is untouched and still queued — not failed, not counted as an attempt.
        with sessions() as session:
            writer = session.query(Job).filter_by(kind="privacy.sync").one()
            assert writer.status == "queued" and writer.attempts == 0

    def test_history_sync_waits_for_a_run_even_though_it_only_reads(self, sessions):
        """The run refreshes history itself and both saturate the same PMS endpoints, so running
        them together is pure waste."""
        tracked, make = self._tracker()
        jobs._HANDLERS["sync.history"] = make("history")
        jobs._HANDLERS["backup.take"] = make("backup")
        jobs.enqueue(sessions, "sync.history")
        jobs.enqueue(sessions, "backup.take")
        busy = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(is_running=lambda: True))

        assert drain(busy) == 1
        assert tracked["order"] == ["backup"]

    def test_a_writer_stands_down_while_a_run_holds_the_lock(self, sessions):
        """The mirror of `test_plex_writers_never_overlap`, and the half that was missing.

        Jobs deferring to runs via `_plex_busy` used to be one-directional: it stopped a job STARTING
        during a run, but a writer already mid-flight kept merging share filters straight through the
        start of one. Runs now hold the same lock, and a writer that finds a run in progress stands
        down rather than queueing behind it — a run holds the lock for its whole duration, and
        `_drain` awaits its tasks while holding the drain lock, so parking would freeze the queue.
        """

        async def scenario():
            ran: list[str] = []
            jobs._HANDLERS["privacy.sync"] = lambda st, payload: ran.append("privacy") or {}
            jobs.enqueue(sessions, "privacy.sync")
            claimed = jobs._claim(sessions)
            assert claimed is not None
            job_id, kind = claimed
            busy = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(is_running=lambda: True))
            async with jobs.plex_writer_lock():  # a run, holding it
                await asyncio.wait_for(jobs._run_writer(busy, sessions, job_id, kind), timeout=2)
            return ran

        assert asyncio.run(scenario()) == [], "a writer merged share filters while a run held the lock"
        with sessions() as session:
            job = session.query(Job).filter_by(kind="privacy.sync").one()
            # Back on the queue with its attempt returned: losing the race is not a failure.
            assert job.status == "queued" and job.attempts == 0

    def test_a_writer_gives_up_on_the_lock_rather_than_parking_for_ever(self, sessions, state, monkeypatch):
        """Bounds the late race: a run can take the lock between a writer's `_plex_busy` check and its
        acquire. Without a cap the writer waits for the run's full duration, inside a `finally` the
        drain lock is held across — freezing every other job, readers included."""
        monkeypatch.setattr(jobs, "WRITER_LOCK_WAIT_S", 0.2)

        async def scenario():
            ran: list[str] = []
            jobs._HANDLERS["privacy.sync"] = lambda st, payload: ran.append("privacy") or {}
            jobs.enqueue(sessions, "privacy.sync")
            claimed = jobs._claim(sessions)
            assert claimed is not None
            job_id, kind = claimed
            # Lock held, but nothing reports a run — so only the timeout can rescue this.
            async with jobs.plex_writer_lock():
                await asyncio.wait_for(jobs._run_writer(state, sessions, job_id, kind), timeout=5)
            return ran

        assert asyncio.run(scenario()) == []
        with sessions() as session:
            job = session.query(Job).filter_by(kind="privacy.sync").one()
            assert job.status == "queued" and job.attempts == 0

    def test_writers_still_run_serially_within_one_drain(self, sessions, state):
        """ "Disable everyone" queues one `user.cleanup` per person and drains them inline, so they
        must all run — serially. A writer that refused to wait for another JOB would leave all but the
        first requeued, and the UI would report one person cleaned out of five."""
        tracked, make = self._tracker()
        jobs._HANDLERS["user.cleanup"] = make("cleanup")
        jobs._HANDLERS["privacy.sync"] = make("privacy")
        jobs.enqueue(sessions, "user.cleanup", {"slug": "sarah"})
        jobs.enqueue(sessions, "privacy.sync")

        assert drain(state) == 2
        assert sorted(tracked["order"]) == ["cleanup", "privacy"], "a writer was left requeued"
        assert tracked["peak"] == 1, "and they still never overlapped"

    def test_a_reader_still_runs_while_a_run_holds_the_writer_lock(self, sessions, state):
        """The point of the lock being writer-only: a run must stop share-filter merges, not the
        database backup. Refusing everything for a run's duration is what this change removed."""

        async def scenario():
            done: list[str] = []
            jobs._HANDLERS["backup.take"] = lambda st, payload: done.append("backup") or {}
            jobs.enqueue(sessions, "backup.take")
            async with jobs.plex_writer_lock():
                await asyncio.wait_for(jobs.run_pending(state), timeout=5)
            return done

        assert asyncio.run(scenario()) == ["backup"]

    def test_a_writer_that_loses_the_race_keeps_its_attempts(self, sessions):
        """A run starting between the claim and the lock is not a failure. Burning one of three
        attempts for it would retire a healthy job after three unlucky nights."""
        started = {"n": 0}

        def racing_is_running():
            # Idle at claim time, busy by the time the writer takes the lock.
            started["n"] += 1
            return started["n"] > 1

        jobs._HANDLERS["privacy.sync"] = lambda st, payload: {}
        jobs.enqueue(sessions, "privacy.sync")
        racing = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(is_running=racing_is_running))

        drain(racing)

        with sessions() as session:
            job = session.query(Job).filter_by(kind="privacy.sync").one()
            assert job.status == "queued"
            assert job.attempts == 0, "a lost race must not count against the retry budget"

    def test_the_reader_pool_is_capped(self, sessions, sessions_state_with_cap):
        """`jobs.max_parallel_readonly` is the dial for a PMS that objects to the concurrency."""
        tracked, make = self._tracker()
        jobs._HANDLERS["sync.history"] = make("history")
        jobs._HANDLERS["backup.take"] = make("backup")
        jobs.enqueue(sessions, "sync.history")
        jobs.enqueue(sessions, "backup.take")

        assert drain(sessions_state_with_cap) == 2
        assert tracked["peak"] == 1, "the cap was ignored"


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
        """`user.cleanup`, `user.hide`, `user.restore` and `row.reconcile` all take a target and
        DELETE or hide that target's rows. A generic "run a job" button must never be able to aim
        them — every one of them is queued by the mutation handler that knows the target."""
        for targeted in ("user.cleanup", "user.hide", "user.restore", "row.reconcile"):
            assert targeted not in jobs.KINDS, targeted
            assert not jobs.BY_KIND[targeted].manual, targeted
        # The manual kinds are all converge-to-desired-state passes that take no target.
        assert set(jobs.KINDS) == {
            "sync.check",
            "privacy.sync",
            "sync.users",
            "sync.history",
            "backup.take",
            "maintenance.prune",
            # Takes no target either: it converges EVERY row onto the surfaces its own day schedule
            # asks for today, so aiming it at anything is meaningless (issue #102).
            "rows.visibility",
        }

    def test_the_catalog_describes_every_registered_handler(self):
        """The Jobs page renders straight from CATALOG, so a kind missing from it is a job the
        operator cannot see ran at all — and a CATALOG entry with no handler is a button that 500s."""
        assert set(jobs.BY_KIND) == set(jobs._HANDLERS)
        for entry in jobs.CATALOG:
            assert entry.label and entry.description, entry.kind
            # A kind no button can start has to say what DOES start it, or its card reads as broken.
            assert entry.manual or entry.trigger, entry.kind

    def test_scheduled_kinds_point_at_a_real_scheduler_job_id(self):
        """`schedule_job_id` is how a card finds its next run. The ids are string literals here
        (importing scheduler would be circular), so this is what catches them drifting apart."""
        from shortlist.server import scheduler

        ids = {
            scheduler.WATCH_SYNC_JOB_ID,
            scheduler.USER_SYNC_JOB_ID,
            scheduler.BACKUP_JOB_ID,
            scheduler.PRIVACY_SYNC_JOB_ID,
            scheduler.SYNC_CHECK_JOB_ID,
            scheduler.MAINTENANCE_PRUNE_JOB_ID,
            scheduler.ROW_VISIBILITY_JOB_ID,
        }
        scheduled = {e.schedule_job_id for e in jobs.CATALOG if e.schedule_job_id}
        assert scheduled == ids

    def test_every_scheduled_kind_names_the_setting_that_edits_its_cron(self):
        """The Schedule view edits crons by settings key. A kind with a timer but no key would
        render a schedule nobody can change."""
        from shortlist.server.settings_store import DEFAULTS

        for entry in jobs.CATALOG:
            if not entry.schedule_job_id:
                continue
            assert entry.schedule_setting, f"{entry.kind} is scheduled but names no settings key"
            assert entry.schedule_setting in DEFAULTS, entry.schedule_setting

    def test_every_kind_declares_whether_it_writes_to_plex(self):
        """The flag that keeps share-filter merges correct.

        A read-modify-write merge running twice at once loses one of the two sets of excludes — the
        bug class §12 of jobs-and-runs-design.md tracks. Adding a kind without thinking about this
        must fail here rather than defaulting quietly.
        """
        readers = {e.kind for e in jobs.CATALOG if not e.writes_plex}
        # Pinned by name, not counted: if a kind changes side, that is a decision someone has to
        # make here, in a diff, rather than a number quietly moving.
        # `watch.reconcile` is a reader of Plex and a writer of our OWN database only: it credits
        # picks from playback already recorded locally and never opens a Plex client.
        assert readers == {"sync.history", "backup.take", "maintenance.prune", "watch.reconcile"}
        writers = {e.kind for e in jobs.CATALOG if e.writes_plex}
        assert "privacy.sync" in writers and "sync.check" in writers
        assert {"user.cleanup", "user.hide", "user.restore", "row.reconcile"} <= writers

    def test_sync_users_is_a_writer_because_it_renames_collections(self):
        """It reads a roster — and then renames Shortlist collections on the PMS in the same handler.

        Classed read-only, that rename could land mid-run. The run matches collections by rendered
        TITLE, so a rename during converge can make a live collection look orphaned (converge deletes
        orphans) or make delivery build a second collection beside the renamed one.
        """
        import inspect

        from shortlist.server.services import user_sync

        assert jobs.BY_KIND["sync.users"].writes_plex is True
        # The reason, asserted rather than trusted: if the rename ever moves out of this handler the
        # flag can be revisited, and this points at where to look.
        source = inspect.getsource(user_sync.sync_users_from_state)
        assert "rename_after_nickname" in source

    def test_hiding_a_paused_user_takes_every_row_off_every_surface(self, sessions):
        """Pause keeps the collection and its label — so everyone else's exclude still matches, and
        unpausing is a re-promote rather than a full LLM rebuild."""
        collection = SimpleNamespace(title="✨ Movies Picked for You")
        plex = SimpleNamespace(
            sections=lambda: ["movies"],
            find_owned_collections=lambda section, label: [collection] if label == "shortlist_sarah" else [],
            demote_all=lambda c: True,
        )
        ctx = SimpleNamespace(plex=plex, config=SimpleNamespace(label_prefix="shortlist", dry_run=False))
        state = SimpleNamespace(
            sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run, plex_only=False: ctx)
        )

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
        ctx = SimpleNamespace(plex=plex, config=SimpleNamespace(label_prefix="shortlist", dry_run=False))
        state = SimpleNamespace(
            sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run, plex_only=False: ctx)
        )

        assert jobs._HANDLERS["user.hide"](state, {"slug": "sarah"})["hidden"] == []


class TestRestoreAfterUnpause:
    """Un-pausing is the mirror of pausing: the collections were only DEMOTED, so putting them back is
    a re-promote, not a rebuild.

    Leaving it to "the next run" — which is what used to happen — was wrong twice over: a row whose
    schedule is blank has no next run at all, and neither does one while `paused_all` is on. An
    un-paused person could stay invisible indefinitely.
    """

    ROW = RowSpec(
        slug="picked",
        name_template="✨ {library_name} Picked for You",
        size=10,
        # Deliberately NOT the defaults: an assertion against `both/both` would pass with the row's
        # placement ignored entirely, which is what the no-spec fallback does.
        placement="off",  # the OWNER's own copy claims nothing
        placement_friends="library",  # everyone else's is Recommended-only, never Home
        pin_top=True,
    )

    def _state(self, sessions, *, promoted: list, merged: list, rows=(), merge_fails=False):
        """A fake Plex + a stub `engine_run` that records the share-filter merge (or fails)."""
        config = EngineConfig(rows=list(rows), rows_defined=bool(rows))
        collection = SimpleNamespace(title="✨ Movies Picked for You" + row_marker(555000100), ratingKey=42)
        plex = SimpleNamespace(
            sections=lambda: [SimpleNamespace(title="Movies", key=1, type="movie")],
            find_owned_collections=lambda section, label: [collection] if label == "shortlist_sarah" else [],
            promote=lambda c, **kw: promoted.append((c.title, kw)),
        )
        ctx = SimpleNamespace(plex=plex, config=config, write_lock=None)

        def fake_engine_run(_ctx, users):
            merged.append(users)
            # Shaped like the REAL return. `pipeline.run` never raises on a privacy failure — it
            # reports one on the returned report — and this stub used to raise, which is the same
            # false premise that let `user.restore` promote a row nobody's filter was hiding.
            return SimpleNamespace(
                error="could not read the plex.tv user list: RuntimeError: plex.tv 503" if merge_fails else None,
                promotion_blockers=[],
                swept_rows={},
                converged=0,
            )

        import shortlist.engine.pipeline as pipeline_mod

        self._patched = (pipeline_mod, pipeline_mod.run)
        pipeline_mod.run = fake_engine_run
        return SimpleNamespace(
            sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run, plex_only=False: ctx)
        )

    @pytest.fixture(autouse=True)
    def _restore_engine_run(self):
        self._patched = None
        yield
        if self._patched:
            module, original = self._patched
            module.run = original

    def _add_user(self, sessions, **overrides):
        from shortlist.server.db.models import User

        with sessions() as session:
            session.add(
                User(
                    plex_account_id=555000100,
                    username="sarah",
                    slug="sarah",
                    user_type=overrides.pop("user_type", "shared"),
                    **{"enabled": True, "prefs": {}, **overrides},
                )
            )
            session.commit()

    def test_the_share_filters_are_merged_before_anything_is_promoted(self, sessions):
        """plex-safety rule 1, outside a run. This used to be two queued jobs relying on the queue
        being FIFO — but `_claim` steps over a job whose retry backoff has not elapsed, so a filter
        pass that failed against a 503 plex.tv was skipped and the promotion behind it landed anyway."""
        promoted: list = []
        merged: list = []
        self._add_user(sessions)

        jobs._HANDLERS["user.restore"](self._state(sessions, promoted=promoted, merged=merged), {"slug": "sarah"})

        assert merged == [[]], "engine_run(ctx, []) merges every filter and builds nothing"
        assert promoted, "and only then is anything promoted"

    def test_a_failed_filter_merge_promotes_nothing_at_all(self, sessions):
        """The whole point of doing the merge in this handler: if it cannot be proven private, the row
        stays down and the JOB fails, so the queue retries the pair together."""
        promoted: list = []
        merged: list = []
        self._add_user(sessions)
        state = self._state(sessions, promoted=promoted, merged=merged, merge_fails=True)

        with pytest.raises(RuntimeError, match="503"):
            jobs._HANDLERS["user.restore"](state, {"slug": "sarah"})

        assert promoted == []

    def test_one_account_refusing_its_filter_stops_the_promotion_for_everyone(self, sessions):
        """The likelier real shape, and the one the old stub could not produce.

        A privacy failure is reported on the RETURNED report, not raised — `_privacy_sync_phase`
        appends to `promotion_blockers` and `run()` returns normally. This handler discarded that
        return, so a plex.tv 503 for ONE account still promoted the row onto shared Home with nothing
        hiding it from the others. That is the leak direction, on the one job whose purpose is making
        something more visible.
        """
        promoted: list = []
        merged: list = []
        self._add_user(sessions)
        state = self._state(sessions, promoted=promoted, merged=merged)
        import shortlist.engine.pipeline as pipeline_mod

        pipeline_mod.run = lambda ctx, users: SimpleNamespace(
            error=None,
            promotion_blockers=["dave (plex account 300): plex.tv 503"],
            swept_rows={},
            converged=0,
        )

        with pytest.raises(RuntimeError, match="dave"):
            jobs._HANDLERS["user.restore"](state, {"slug": "sarah"})

        assert promoted == [], "nothing may be promoted while any account's excludes are unwritten"

    def test_privacy_sync_does_not_report_success_when_no_filter_was_written(self, sessions):
        """It read only `swept_rows`/`converged` and returned a result dict, so `_finish` marked the
        job `done` — "Share filters merged for every account" — and retired an owed HIDE from the
        durable queue. Nothing retried it, and the Jobs page said it had worked."""
        state = self._state(sessions, promoted=[], merged=[])
        import shortlist.engine.pipeline as pipeline_mod

        pipeline_mod.run = lambda ctx, users: SimpleNamespace(
            error="could not read the plex.tv user list: RuntimeError: plex.tv 503",
            promotion_blockers=[],
            swept_rows={},
            converged=0,
        )

        with pytest.raises(RuntimeError, match=re.escape("plex.tv 503")):
            jobs._HANDLERS["privacy.sync"](state, {"reason": "someone left a shared row"})

    @pytest.mark.parametrize(
        ("user_type", "expected"),
        [
            # The row says: owner sees it nowhere, friends see it on the Recommended shelf only.
            ("owner", {"shared": False, "home": False, "recommended": False, "pin_top": True}),
            ("shared", {"shared": False, "home": False, "recommended": True, "pin_top": True}),
            # MANAGED goes with SHARED, never the owner — Plex's own docs: promotedToSharedHome
            # "applies to all shared users, INCLUDING managed users".
            ("managed", {"shared": False, "home": False, "recommended": True, "pin_top": True}),
        ],
    )
    def test_it_promotes_onto_the_surfaces_the_row_actually_asks_for(self, sessions, user_type, expected):
        """Asserted against a REAL RowSpec with a non-default placement. With `rows=[]` the title lookup
        misses and every collection takes `_promote_one`'s no-spec fallback, so an assertion there would
        pass with the row's placement wrong in every field."""
        promoted: list = []
        merged: list = []
        self._add_user(sessions, user_type=user_type)

        jobs._HANDLERS["user.restore"](
            self._state(sessions, promoted=promoted, merged=merged, rows=[self.ROW]), {"slug": "sarah"}
        )

        assert [kwargs for _title, kwargs in promoted] == [expected]

    def test_a_users_own_row_name_override_still_finds_their_collection(self, sessions):
        """`resolve_row_template` puts a user's `row_name_tpl` ABOVE the global template, so a profile
        built without it renders the wrong title here, matches nothing, and drops the row onto the
        fallback's surfaces — which for a `placement=off` row means putting it somewhere the operator
        switched off.

        The DEFAULT row, deliberately: it is the only one with a blank `name_template`, and so the only
        one where the per-user override is consulted at all."""
        promoted: list = []
        merged: list = []
        self._add_user(sessions, prefs={"row_name_tpl": "🌟 My Own Picks"})
        default_row = replace(self.ROW, name_template="")
        state = self._state(sessions, promoted=promoted, merged=merged, rows=[default_row])
        # Their collection carries the title THEIR template renders to, not the global one.
        ctx = state.run_service.build_context(dry_run=False)
        collection = ctx.plex.find_owned_collections(None, "shortlist_sarah")[0]
        collection.title = "🌟 My Own Picks" + row_marker(555000100)

        jobs._HANDLERS["user.restore"](state, {"slug": "sarah"})

        assert [kwargs for _t, kwargs in promoted] == [
            {"shared": False, "home": False, "recommended": True, "pin_top": True}
        ]

    def test_a_top_seed_row_is_placed_from_the_ledger_with_no_run_history_at_all(self, sessions):
        """The last gap the ledger closes. A `{top_seed}` title is different every run, so nothing can
        re-render it — and the run breakdown that used to answer "which row is this?" is scoped to one
        run and erased by `DELETE /api/runs`. The ratingKey is the only handle left, and without it the
        row takes `_promote_one`'s no-spec branch: onto its audience's Home, regardless of a placement
        that may say `off`.

        NO run history is set up here, deliberately."""
        from shortlist.server.db.models import Delivery, User

        promoted: list = []
        merged: list = []
        self._add_user(sessions)
        row = replace(self.ROW, name_template="Because you watched {top_seed}")
        state = self._state(sessions, promoted=promoted, merged=merged, rows=[row])
        ctx = state.run_service.build_context(dry_run=False)
        ctx.plex.find_owned_collections(None, "shortlist_sarah")[0].title = "Because you watched Dune" + row_marker(
            555000100
        )
        with sessions() as session:
            user_id = session.query(User).filter_by(slug="sarah").one().id
            assert user_id
            session.add(
                Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=42, title="whatever")
            )
            session.commit()

        jobs._HANDLERS["user.restore"](state, {"slug": "sarah"})

        # The ROW's placement — off for the owner, Recommended-only for friends, pinned — not the
        # fallback's "show it on their Home".
        assert [kwargs for _t, kwargs in promoted] == [
            {"shared": False, "home": False, "recommended": True, "pin_top": True}
        ]

    def test_the_ledger_wins_over_a_stale_recorded_title(self, sessions):
        """Both sources can disagree — a title recorded before a rename, against a ratingKey that
        cannot go stale. Identity has to win, or a renamed row is placed by whatever it used to be."""
        from shortlist.server.db.models import Delivery, Run, RunUser, User

        promoted: list = []
        merged: list = []
        self._add_user(sessions)
        state = self._state(sessions, promoted=promoted, merged=merged, rows=[self.ROW])
        ctx = state.run_service.build_context(dry_run=False)
        ctx.plex.find_owned_collections(None, "shortlist_sarah")[0].title = "Renamed Since" + row_marker(555000100)
        with sessions() as session:
            user = session.query(User).filter_by(slug="sarah").one()
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            # The breakdown names a row that no longer exists in the config…
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user.id,
                    status="ok",
                    breakdown=[{"row_slug": "deleted_row", "row_title": "Renamed Since"}],
                )
            )
            # …while the ledger names the real one.
            session.add(
                Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=42, title="x")
            )
            session.commit()

        jobs._HANDLERS["user.restore"](state, {"slug": "sarah"})

        assert [kwargs for _t, kwargs in promoted] == [
            {"shared": False, "home": False, "recommended": True, "pin_top": True}
        ]

    def test_a_top_seed_row_is_placed_from_what_the_last_run_delivered(self, sessions):
        """A `{top_seed}` title is different every run, so it cannot be re-rendered from the template.
        The last run's breakdown is the only record of which row a collection belongs to — without it
        the row takes the no-spec fallback and lands on a surface its placement switched off."""
        from shortlist.server.db.models import Run, RunUser, User

        promoted: list = []
        merged: list = []
        self._add_user(sessions)
        row = replace(self.ROW, name_template="Because you watched {top_seed}")
        state = self._state(sessions, promoted=promoted, merged=merged, rows=[row])
        ctx = state.run_service.build_context(dry_run=False)
        ctx.plex.find_owned_collections(None, "shortlist_sarah")[0].title = "Because you watched Dune" + row_marker(
            555000100
        )
        with sessions() as session:
            user = session.query(User).filter_by(slug="sarah").one()
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user.id,
                    status="ok",
                    breakdown=[{"row_slug": "picked", "row_title": "Because you watched Dune"}],
                )
            )
            session.commit()

        jobs._HANDLERS["user.restore"](state, {"slug": "sarah"})

        assert [kwargs for _t, kwargs in promoted] == [
            {"shared": False, "home": False, "recommended": True, "pin_top": True}
        ]

    def test_a_replayed_job_does_nothing_once_the_user_is_paused_again(self, sessions):
        """Jobs are replayed after a crash with no way to know how far they got. This is the one
        handler that makes rows MORE visible, so a stale replay must not un-hide someone the owner
        has since paused."""
        promoted: list = []
        merged: list = []
        self._add_user(sessions, prefs={"paused": True})

        result = jobs._HANDLERS["user.restore"](
            self._state(sessions, promoted=promoted, merged=merged), {"slug": "sarah"}
        )

        assert promoted == [] and merged == []
        assert "no longer an un-paused user" in result["detail"]

    def test_a_replayed_job_does_nothing_once_the_user_is_disabled(self, sessions):
        promoted: list = []
        merged: list = []
        self._add_user(sessions, enabled=False)

        jobs._HANDLERS["user.restore"](self._state(sessions, promoted=promoted, merged=merged), {"slug": "sarah"})

        assert promoted == []

    def test_a_user_deleted_since_the_job_was_queued_is_not_an_error(self, sessions):
        """A failed handler is retried three times and then raises a notification. "The user is gone"
        is a correct outcome, not a failure to escalate."""
        promoted: list = []
        merged: list = []

        result = jobs._HANDLERS["user.restore"](
            self._state(sessions, promoted=promoted, merged=merged), {"slug": "ghost"}
        )

        assert promoted == [] and result["restored"] == []


class TestSafeMode:
    """`SHORTLIST_DRY_RUN=1` must make EVERY Plex write a preview (plex-safety rule 8).

    `build_context` ORs the env flag into `ctx.config.dry_run`, so a handler passing `dry_run=False`
    still gets a dry-run context — but neither `PlexClient.promote` nor `demote_all` has a dry-run
    branch of its own, so the guard has to be at the call site. `_promote_phase` had one and
    `promote_user_rows` did not, which stayed invisible until `user.restore` became a second caller:
    un-pausing someone previewed the hiding and performed the showing.
    """

    def _ctx(self, *, wrote: list):
        collection = SimpleNamespace(title="✨ Movies Picked for You" + row_marker(555000100), ratingKey=42)
        plex = SimpleNamespace(
            sections=lambda: [SimpleNamespace(title="Movies", key=1, type="movie")],
            find_owned_collections=lambda section, label: [collection] if label == "shortlist_sarah" else [],
            promote=lambda c, **kw: wrote.append(("promote", kw)),
            demote_all=lambda c, **kw: wrote.append(("demote", kw)) or True,
            claims_any_surface=lambda c: True,
        )
        return SimpleNamespace(plex=plex, config=EngineConfig(dry_run=True), write_lock=None)

    def _state(self, sessions, ctx):
        return SimpleNamespace(
            sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run, plex_only=False: ctx)
        )

    def _add_sarah(self, sessions, **overrides):
        from shortlist.server.db.models import User

        with sessions() as session:
            session.add(
                User(
                    plex_account_id=555000100,
                    username="sarah",
                    slug="sarah",
                    user_type="shared",
                    **{"enabled": True, "prefs": {}, **overrides},
                )
            )
            session.commit()

    def test_restoring_an_un_paused_user_promotes_nothing(self, sessions, monkeypatch):
        wrote: list = []
        self._add_sarah(sessions)
        import shortlist.engine.pipeline as pipeline_mod

        # A clean report — the real shape. `run()` returns one; it does not return None.
        monkeypatch.setattr(
            pipeline_mod,
            "run",
            lambda ctx, users: SimpleNamespace(error=None, promotion_blockers=[], swept_rows={}, converged=0),
        )

        result = jobs._HANDLERS["user.restore"](self._state(sessions, self._ctx(wrote=wrote)), {"slug": "sarah"})

        assert wrote == []
        assert result["restored"] == []

    def test_hiding_a_paused_user_writes_nothing_but_still_reports_the_plan(self, sessions):
        """A preview has to say what WOULD change, or an operator cannot tell "nothing to do" from
        "safe mode swallowed it"."""
        wrote: list = []
        self._add_sarah(sessions)

        result = jobs._HANDLERS["user.hide"](self._state(sessions, self._ctx(wrote=wrote)), {"slug": "sarah"})

        assert wrote == []
        assert len(result["hidden"]) == 1 and result["dry_run"] is True
        assert result["detail"].startswith("Would hide")


class TestCleanupForgetsTheLedger:
    """Disabling someone removes their WHOLE label from Plex in one go — which the per-row
    `_forget_deliveries` never sees, because it is scoped to a row.

    Left alone, the ledger keeps pointing at ratingKeys that no longer exist. Not a correctness
    problem (a removal still has to find the collection under one of OUR labels first, so a stale key
    cannot reach anything) but it grows for ever and makes the audit lie. Found by running a disable
    against a real PMS, not by these tests — which is why this one exists.
    """

    def _state(self, sessions, *, removed: list[str]):
        plex = SimpleNamespace(
            sections=lambda: [SimpleNamespace(title="Movies", key=1, type="movie")],
            find_owned_collections=lambda section, label: (
                [SimpleNamespace(title=t, ratingKey=i) for i, t in enumerate(removed, start=900)]
                if label == "shortlist_sarah"
                else []
            ),
            delete_owned_collection=lambda c, prefix: None,
        )

        def build_context(dry_run, plex_only=False):
            # The stub reproduces the chokepoint, because that is where safe mode is applied: the
            # handler no longer calls `force_dry_run()` itself, so a context that ignored the flag
            # would make this test pass for a handler that had stopped honouring it.
            from shortlist.server.safe_mode import force_dry_run

            return SimpleNamespace(plex=plex, config=EngineConfig(dry_run=force_dry_run() or dry_run), write_lock=None)

        return SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=build_context))

    def _ledger(self, sessions) -> list[tuple]:
        from shortlist.server.db.models import Delivery

        with sessions() as session:
            return [(d.user_slug, d.library_key) for d in session.query(Delivery)]

    def _seed(self, sessions):
        from shortlist.server.db.models import Delivery

        with sessions() as session:
            session.add(Delivery(collection_slug="picked", user_slug="sarah", library_key="1", rating_key=900))
            session.add(Delivery(collection_slug="picked", user_slug="mike", library_key="1", rating_key=901))
            session.commit()

    def test_it_forgets_that_users_rows_and_nobody_elses(self, sessions):
        self._seed(sessions)

        jobs._HANDLERS["user.cleanup"](self._state(sessions, removed=["✨ Picked for You"]), {"slug": "sarah"})

        assert self._ledger(sessions) == [("mike", "1")]

    def test_a_dry_run_leaves_the_ledger_alone(self, sessions, monkeypatch):
        """A preview removed nothing, so the ledger must still be able to address what it previewed."""
        import shortlist.server.safe_mode as safe_mode

        monkeypatch.setattr(safe_mode, "force_dry_run", lambda: True)
        self._seed(sessions)

        jobs._HANDLERS["user.cleanup"](self._state(sessions, removed=["✨ Picked for You"]), {"slug": "sarah"})

        assert sorted(self._ledger(sessions)) == [("mike", "1"), ("sarah", "1")]


class TestStaleSweepDoesNotDuplicate:
    """The periodic sweep exists to reclaim work a DEAD process abandoned. It must not touch work
    this process is still holding."""

    def _job(self, sessions, **kw):
        from shortlist.server.db.models import Job

        with sessions() as session:
            job = Job(kind="user.cleanup", status="running", payload={}, **kw)
            session.add(job)
            session.commit()
            return job.id

    def test_a_job_waiting_on_the_writer_lock_is_not_requeued(self, sessions):
        """`_claim` leaves `started_at` unset until `_execute` stamps it, so a writer waiting up to
        WRITER_LOCK_WAIT_S for the Plex lock sits at running/None. Requeuing that is not recovery,
        it is duplication — the original coroutine is alive and about to run it, and the requeued
        copy gets picked up by the next drain, so one job executes twice against Plex.
        """
        from shortlist.server.db.models import Job
        from shortlist.server.services.jobs import recover_stale

        job_id = self._job(sessions, started_at=None, attempts=1)

        assert recover_stale(sessions, boot=False) == 0
        with sessions() as session:
            assert session.get(Job, job_id).status == "running"

    def test_boot_DOES_reclaim_one_because_no_coroutine_survived_the_restart(self, sessions):
        """Same state, opposite meaning: after a restart nothing is holding it."""
        from shortlist.server.db.models import Job
        from shortlist.server.services.jobs import recover_stale

        job_id = self._job(sessions, started_at=None, attempts=1)

        assert recover_stale(sessions, boot=True) == 1
        with sessions() as session:
            assert session.get(Job, job_id).status == "queued"

    def test_a_genuinely_stuck_job_is_still_reclaimed(self, sessions):
        """The sweep must keep doing its actual job for work that started and then died."""
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import Job
        from shortlist.server.services.jobs import STALE_AFTER, recover_stale

        job_id = self._job(sessions, started_at=datetime.now(UTC) - STALE_AFTER - timedelta(minutes=1))

        assert recover_stale(sessions, boot=False) == 1
        with sessions() as session:
            assert session.get(Job, job_id).status == "queued"


class TestTheAuditRecordsTheEffectiveDryRun:
    """Under `SHORTLIST_DRY_RUN`, an audit row must never describe a preview as a real write.

    Safe mode is applied by `build_context`, BELOW the handler that asked for a live run — so the
    value a handler asks for and the value that governs the writes are two different things. Three
    handlers audited the one they asked for (`row.reconcile` hardcoded `False` outright), which meant
    that under safe mode the events feed recorded somebody's row as DELETED when nothing had been
    touched. That feed is the whole of rule 10's "what changed on whose server at 03:31".

    The chokepoint here is the REAL one: only the client stack below it is stubbed.
    """

    @pytest.fixture
    def state(self, sessions, monkeypatch):
        from pathlib import Path

        from shortlist.server.services import run_service as run_service_mod
        from shortlist.server.services.run_service import RunService
        from shortlist.server.services.sse import EventBus

        monkeypatch.setattr(run_service_mod, "force_dry_run", lambda: True)  # SHORTLIST_DRY_RUN=1
        service = RunService(sessions, EventBus(), Path("/nonexistent"), None)
        plex = SimpleNamespace(
            sections=lambda: [SimpleNamespace(title="Movies", key=1, type="movie")],
            find_owned_collections=lambda section, label: [],
            claims_any_surface=lambda c: False,
            demote_all=lambda c: False,
        )
        monkeypatch.setattr(
            service._ctx,
            "build_plex_only",
            lambda *, dry_run: SimpleNamespace(plex=plex, config=EngineConfig(dry_run=dry_run)),
        )
        return SimpleNamespace(sessions=sessions, run_service=service, secrets=None)

    def _audited(self, sessions, scope: str) -> list[bool]:
        with sessions() as session:
            return [e.message.get("dry_run") for e in session.query(Event).filter_by(scope=scope)]

    def test_removing_a_disabled_persons_rows(self, state, sessions):
        result = jobs._HANDLERS["user.cleanup"](state, {"slug": "sarah", "dry_run": False})

        assert result["dry_run"] is True
        assert self._audited(sessions, "user.disable.cleanup") == [True]

    def test_hiding_a_paused_persons_rows(self, state, sessions):
        result = jobs._HANDLERS["user.hide"](state, {"slug": "sarah", "dry_run": False})

        assert result["dry_run"] is True
        assert self._audited(sessions, "user.pause.hide") == [True]

    def test_reconciling_a_changed_row(self, state, sessions):
        """The one that hardcoded `"dry_run": False` into its Event."""
        result = jobs._HANDLERS["row.reconcile"](state, {"slug": "picked", "build": "per_person"})

        assert result["dry_run"] is True
        assert self._audited(sessions, "row.reconcile") == [True]
        assert result["detail"].startswith("Would remove")

    def test_no_audit_row_claims_a_timestamp_of_its_own(self, state, sessions):
        """`Event.ts` is the column the API sorts and filters on. A second copy in the JSON only ever
        drifts from it, and only some writers ever emitted one — so "when did this happen" was
        answered from a different field depending on who wrote the row."""
        jobs._HANDLERS["user.hide"](state, {"slug": "sarah"})

        with sessions() as session:
            event = session.query(Event).filter_by(scope="user.pause.hide").one()
            assert "at" not in event.message
            assert event.ts is not None


class TestRetentionPruning:
    """Retention runs as its own job, in its own transaction.

    It used to run INSIDE the run-persist transaction: a bulk delete that failed there rolled back
    the persist with it, discarding the record of a run that had already written to Plex.
    """

    def _seed_old_run(self, sessions) -> int:
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import Event as EventRow
        from shortlist.server.db.models import Run

        old = datetime.now(UTC) - timedelta(days=400)
        with sessions() as session:
            run = Run(trigger="scheduled", status="ok", started_at=old)
            session.add(run)
            session.add(EventRow(scope="run.user", level="info", message={}, ts=old))
            session.commit()
            return run.id

    def test_it_deletes_past_the_retention_window_and_says_what_it_did(self, sessions):
        from shortlist.server.db.models import Run
        from shortlist.server.settings_store import SettingsStore

        run_id = self._seed_old_run(sessions)
        with sessions() as session:
            SettingsStore(session).set("runs.retention", 1)
            SettingsStore(session).set("events.retention", 1)

        result = jobs._HANDLERS["maintenance.prune"](SimpleNamespace(sessions=sessions), {})

        assert result["runs"] == 1 and result["events"] == 1
        assert "Pruned 1 run(s)" in result["detail"]
        with sessions() as session:
            assert session.get(Run, run_id) is None

    def test_watch_history_ages_out_on_the_same_cutoff(self, sessions):
        """The two new tables are not tied to a run, so the run prune cannot reach them — and without
        their own sweep they are the only tables here that grow for ever (Plex's own log holds 101,604
        rows over six years on a real server, and we ingest ~100 a day from it).

        An event older than the oldest retained run can never be attributed to anything anyway: the
        delivery it would have been judged against is gone."""
        from shortlist.server.db.models import WatchEvent, WatchSession
        from shortlist.server.settings_store import SettingsStore

        old = datetime.now(UTC) - timedelta(days=400)
        recent = datetime.now(UTC) - timedelta(days=1)
        self._seed_old_run(sessions)
        with sessions() as session:
            for when, key in ((old, "old"), (recent, "new")):
                session.add(
                    WatchEvent(
                        plex_account_id=99,
                        rating_key=1,
                        media_type="movie",
                        viewed_at=when,
                        source="history",
                        history_key=key,
                    )
                )
                session.add(
                    WatchSession(
                        plex_account_id=99,
                        session_key="1",
                        rating_key=1,
                        media_type="movie",
                        started_at=when,
                        last_seen_at=when,
                        max_offset_ms=1,
                        duration_ms=2,
                    )
                )
            SettingsStore(session).set("runs.retention", 1)
            session.commit()

        jobs._HANDLERS["maintenance.prune"](SimpleNamespace(sessions=sessions), {})

        with sessions() as session:
            assert [e.history_key for e in session.query(WatchEvent).all()] == ["new"]
            assert session.query(WatchSession).count() == 1, "the 400-day-old session went with it"

    def test_a_null_retention_row_does_not_raise(self, sessions):
        """`int(store.get("runs.retention"))` had no `or 0` guard — and it raised inside the run's
        persist transaction, where the cost of a TypeError was the whole run's record."""
        from shortlist.server.settings_store import SettingsStore

        self._seed_old_run(sessions)
        with sessions() as session:
            SettingsStore(session).set("runs.retention", None)

        result = jobs._HANDLERS["maintenance.prune"](SimpleNamespace(sessions=sessions), {})

        assert result["runs"] == 0  # unreadable retention means keep everything, not crash


class TestSyncCheckPreviewsWhatItWouldDelete:
    """The sync check's preview has to NAME the collections a real pass would destroy.

    Deleting a collection is the one irreversible thing Shortlist does to a Plex server, and the UI's
    "this will delete N collections … this cannot be undone" callout renders off the `orphans` list
    this handler returns. `may_delete=confirmed and not dry_run` withheld delete authority from the
    preview, so converge filed every orphan under `converged` instead and `orphans` came back empty
    on every preview ever run — the warning could not render, and Fix deleted unannounced.

    A dry run holding that authority cannot delete anything: `ctx.config.dry_run` is True whenever
    `dry_run` is, and converge checks that flag before every delete
    (`test_pipeline.py::test_dry_run_reports_the_deletion_without_making_it`).
    """

    def _state(self, *, forced_dry_run: bool = False, sections: list | None = None):
        # A REAL EngineConfig and a plex client, because the handler now also places rows on the
        # Recommended shelf and reads both. A namespace carrying only `dry_run` was a fake easier
        # than the thing it stood for, and it hid the shelf pass from every test here.
        import threading

        built: list = []

        def build_context(dry_run: bool, plex_only: bool = False):
            plex = MagicMock()
            plex.sections.return_value = list(sections or [])
            plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["row"], "verified": True}
            ctx = SimpleNamespace(
                config=EngineConfig(
                    dry_run=dry_run or forced_dry_run,
                    rows=[RowSpec(slug="picked", name_template="Picked for You", size=10)],
                    rows_defined=True,
                ),
                plex=plex,
                delivery_sections=[],
                delivered_keys={},
                write_lock=threading.Lock(),
            )
            built.append(ctx)
            return ctx

        # `sessions` because the handler audits the shelf pass through `write_audit`, which opens one.
        # A namespace without it is a fake thinner than the real `app.state` and would only ever prove
        # the audit is unreachable.
        return SimpleNamespace(
            run_service=SimpleNamespace(build_context=build_context), contexts=built, sessions=MagicMock()
        )

    def _converge_spy(self, monkeypatch) -> list[bool]:
        """Record the `may_delete` each call is given, and report one orphan when it is allowed."""
        from shortlist.engine import pipeline

        seen: list[bool] = []

        def fake_converge(ctx, promoted, report, *, may_delete=None):
            seen.append(may_delete)
            if may_delete:
                report.orphans_removed = ["shortlist_ghost"]
            else:
                report.converged = ["shortlist_ghost"]

        monkeypatch.setattr(pipeline, "_converge_phase", fake_converge)
        return seen

    def test_a_preview_lists_the_orphans_it_would_remove(self, monkeypatch):
        seen = self._converge_spy(monkeypatch)
        state = self._state(sections=[MagicMock(type="movie", key="1", title="Movies")])

        result = jobs._HANDLERS["sync.check"](state, {"dry_run": True})

        assert seen == [True], "a preview must be allowed to REPORT what a real pass would delete"
        assert result["orphans"] == ["shortlist_ghost"]
        # Worded as a warning about the future, not a record of a deletion that happened.
        assert "1 orphaned collection(s) to remove" in result["detail"]
        # "Press Check now and it tells you what it would change without touching anything" — the
        # shelf pass is inside that promise too, so it must be asked for as a DRY RUN and worded so.
        ctx = state.contexts[0]
        assert ctx.plex.order_owned_hubs.call_args.kwargs["dry_run"] is True
        assert "would reposition rows on the shelf in Movies" in result["detail"]

    def test_it_also_puts_the_rows_back_in_place_on_the_shelf(self, monkeypatch):
        """ "Check and fix rows on Plex" has to fix a row stranded at the bottom of the Recommended shelf.

        That is the literal complaint this button is pressed for, and it did nothing about it: the
        handler only converged. On SFLIX it was pressed against a shelf holding 14 rows at the bottom
        and reported success without issuing a single move (2026-08-12).
        """
        self._converge_spy(monkeypatch)
        movies = MagicMock(type="movie", key="1", title="Movies")
        state = self._state(sections=[movies])

        result = jobs._HANDLERS["sync.check"](state, {"confirmed": True})

        ctx = state.contexts[0]
        # The library rows live in was named without reading a single item inside it...
        assert [s.title for s in ctx.delivery_sections] == ["Movies"]
        ctx.plex.build_library_index.assert_not_called()
        # ...and the shelf placement really was asked for, not just reported.
        ctx.plex.order_owned_hubs.assert_called_once()
        assert ctx.plex.order_owned_hubs.call_args.kwargs["dry_run"] is False
        assert "repositioned rows on the shelf in Movies" in result["detail"]

    def test_the_shelf_pass_is_audited_even_though_no_run_is_persisted(self, monkeypatch):
        """`run_persistence` only audits a PERSISTED run, and this handler persists none.

        So without its own audit the `verified: False` record — the one thing that makes a shelf Plex
        refused to reorder visible to anyone — exists only on the path that never needed it
        (plex-safety rule 10).
        """
        self._converge_spy(monkeypatch)
        audits: list[tuple] = []
        monkeypatch.setattr(jobs, "write_audit", lambda st, scope, level, **f: audits.append((scope, level, f)))
        state = self._state(sections=[MagicMock(type="movie", key="1", title="Movies")])

        jobs._HANDLERS["sync.check"](state, {"confirmed": True})

        shelf = [a for a in audits if a[0] == "shelf.order"]
        assert len(shelf) == 1
        _, level, fields = shelf[0]
        assert level == "info" and fields["verified"] is True and fields["library"] == "Movies"

    def test_an_unverified_shelf_pass_is_audited_as_a_warning(self, monkeypatch):
        """A shelf we asked for and did not get has to reach the operator, not just the log."""
        self._converge_spy(monkeypatch)
        audits: list[tuple] = []
        monkeypatch.setattr(jobs, "write_audit", lambda st, scope, level, **f: audits.append((scope, level, f)))
        state = self._state(sections=[MagicMock(type="movie", key="1", title="Movies")])

        jobs._HANDLERS["sync.check"](state, {"confirmed": True})
        # Re-run with the client reporting the shelf as unconfirmed.
        state.contexts.clear()
        audits.clear()
        original = state.run_service.build_context

        def unverified_context(dry_run: bool, plex_only: bool = False):
            ctx = original(dry_run, plex_only)
            ctx.plex.order_owned_hubs.return_value = {"skipped": False, "moved": ["row"], "verified": False}
            return ctx

        state.run_service.build_context = unverified_context
        jobs._HANDLERS["sync.check"](state, {"confirmed": True})

        shelf = [a for a in audits if a[0] == "shelf.order"]
        assert [a[1] for a in shelf] == ["warning"]
        assert shelf[0][2]["verified"] is False

    def test_a_placement_we_could_not_apply_is_audited_under_its_own_scope(self, monkeypatch):
        """Issue #106's second half. A configured placement we cannot honour was a container-log
        warning and nothing else, so the Rows page went on showing a setting that had silently done
        nothing since the night it was saved.

        Its OWN scope, not `shelf.order`: `_shelf_contention` counts repeated MOVES within a bounded
        event budget, and one stale anchor re-reported on every privacy sync has nothing to tell it.
        And no `verified` — nothing was asked of Plex, so that question has no answer here.
        """
        self._converge_spy(monkeypatch)
        audits: list[tuple] = []
        monkeypatch.setattr(jobs, "write_audit", lambda st, scope, level, **f: audits.append((scope, level, f)))
        state = self._state(sections=[MagicMock(type="movie", key="1", title="Movies")])
        original = state.run_service.build_context

        def unplaceable_context(dry_run: bool, plex_only: bool = False):
            ctx = original(dry_run, plex_only)
            ctx.plex.order_owned_hubs.return_value = {
                "anchor": "Archive 2019",
                "moved": [],
                "skipped": True,
                "reason": "anchor not on the shelf",
            }
            return ctx

        state.run_service.build_context = unplaceable_context
        result = jobs._HANDLERS["sync.check"](state, {"confirmed": True})

        assert [a[0] for a in audits if a[0].startswith("shelf.")] == ["shelf.unplaced"]
        _, level, fields = next(a for a in audits if a[0] == "shelf.unplaced")
        assert level == "warning"
        assert fields["reason"] == "anchor not on the shelf"
        assert fields["verified"] is None  # never fabricated: we asked Plex for nothing
        # And the operator's line says so instead of claiming a reposition.
        assert "could NOT place rows in Movies" in result["detail"]
        assert "repositioned rows on the shelf" not in result["detail"]

    def test_a_dry_run_never_files_an_unplaceable_row_as_a_warning(self, monkeypatch):
        """A dry run asked Plex for nothing, so it is a preview either way — the rule
        `run_persistence._emit_hub_ordering_events` already states for `verified`.

        Driven through `_audit_hub_orderings` directly: `sync.check` without `confirmed` previews
        DELETES but still writes, so it is not a dry run and cannot exercise this branch.
        """
        from types import SimpleNamespace

        audits: list[tuple] = []
        monkeypatch.setattr(jobs, "write_audit", lambda st, scope, level, **f: audits.append((scope, level, f)))
        report = SimpleNamespace(
            hub_orderings=[
                {"library": "Movies", "placed": False, "moved": [], "reason": "anchor not on the shelf"},
                {"library": "TV", "moved": ["row"], "verified": False},
            ]
        )

        jobs._audit_hub_orderings(None, report, dry_run=True)

        assert [(a[0], a[1]) for a in audits] == [("shelf.unplaced", "info"), ("shelf.order", "info")]

    def test_the_unattended_nightly_pass_still_has_no_delete_authority(self, monkeypatch):
        """The scheduled pass sends neither flag. It must demote and report, never destroy — upgrading
        must not turn on a job that silently deletes from somebody's Plex server."""
        seen = self._converge_spy(monkeypatch)

        result = jobs._HANDLERS["sync.check"](self._state(), {})

        assert seen == [False]
        assert result["orphans"] == []
        assert result["fixed"] == ["shortlist_ghost"]  # taken off the shelves, left in place

    def test_pressing_fix_authorises_the_deletion(self, monkeypatch):
        seen = self._converge_spy(monkeypatch)

        result = jobs._HANDLERS["sync.check"](self._state(), {"confirmed": True})

        assert seen == [True]
        assert "removed 1 orphaned collection(s)" in result["detail"]

    def test_safe_mode_downgrades_a_confirmed_fix_back_to_a_preview(self, monkeypatch):
        """SHORTLIST_DRY_RUN forces the context dry, and the handler's `dry_run` picks that up — so
        Fix reports rather than removes, and the detail must not claim a deletion that never happened."""
        seen = self._converge_spy(monkeypatch)
        state = self._state(forced_dry_run=True, sections=[MagicMock(type="movie", key="1", title="Movies")])

        result = jobs._HANDLERS["sync.check"](state, {"confirmed": True})

        assert seen == [True]
        assert "1 orphaned collection(s) to remove" in result["detail"]
        assert "removed" not in result["detail"]
        # Safe mode has to reach the shelf pass too — it is a Plex write like any other here.
        assert state.contexts[0].plex.order_owned_hubs.call_args.kwargs["dry_run"] is True
        assert "would reposition" in result["detail"]


class TestWatchReconcileTellsTheDashboard:
    """Crediting the watch is only half of it. Without the SSE the owner watches something, the credit
    lands in the database seconds later, and the page in front of them still says nothing until they
    reload — which reads as the feature not working.

    Driven against REAL data, not a stubbed `reconcile_from_events`. Stubbing it made the second test
    assert a return value the real function did not produce: it counted "users who have a credit",
    recomputed from the whole event log, so one person pressing stop reported all 47 users and every
    dashboard on the server refetched for nothing.
    """

    def _world(self, sessions):
        """One user, one live row, one pick, one session that reached 30%."""
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import Collection, Delivery, PickRow, Run, User, WatchSession

        now = datetime.now(UTC)
        with sessions() as s:
            # Ids assigned by the DATABASE, not pinned: this fixture's schema is migrated, so a
            # seeded default row already owns `collections.id = 1`.
            user = User(plex_account_id=99, username="alex", slug="alex")
            row = Collection(slug="mine", name="Mine", enabled=True)
            run = Run(trigger="schedule", status="ok", started_at=now - timedelta(days=2))
            s.add_all([user, row, run])
            s.flush()
            s.add(Delivery(collection_slug="mine", user_slug="alex", library_key="1", rating_key=7))
            s.add(
                PickRow(
                    run_id=run.id,
                    user_id=user.id,
                    collection_slug="mine",
                    section_key="1",
                    library="Movies",
                    tmdb_id=550,
                    media_type="movie",
                    rating_key=9001,
                    rank=1,
                    title="Fight Club",
                    created_at=now - timedelta(days=1),
                )
            )
            s.add(
                WatchSession(
                    plex_account_id=99,
                    session_key="1",
                    rating_key=9001,
                    media_type="movie",
                    started_at=now - timedelta(hours=2),
                    last_seen_at=now - timedelta(hours=1),
                    ended_at=now - timedelta(hours=1),
                    max_offset_ms=1_800_000,
                    duration_ms=6_000_000,
                    end_reason="stopped",
                )
            )
            s.commit()

    def _state(self, sessions):
        published: list[tuple[str, dict]] = []
        bus = SimpleNamespace(publish=lambda event, data: published.append((event, data)))
        return SimpleNamespace(sessions=sessions, run_service=None, bus=bus), published

    def test_it_credits_and_publishes_the_event_the_report_listens_for(self, sessions):
        """`sync.finished` / `kind="watched"` is what `useSyncWatched` invalidates the report on."""
        from shortlist.server.db.models import PickRow

        self._world(sessions)
        state, published = self._state(sessions)

        result = jobs._HANDLERS["watch.reconcile"](state, {})

        assert result == {"users_credited": 1}
        assert published == [("sync.finished", {"kind": "credited", "ok": True, "count": 1})], (
            "its own kind — sent as 'watched' it made the Jobs page announce a sync that never ran"
        )
        with sessions() as s:
            pick = s.query(PickRow).filter_by(tmdb_id=550).one()
            assert pick.watched_at is not None and pick.max_percent == 30

    def test_a_second_pass_over_the_same_data_says_nothing(self, sessions):
        """A session ends every time anyone stops anything, and this recomputes from the whole event
        log. Announcing a refresh that moved no number has every dashboard on the server refetch for
        nothing — and it is what the previous version of this test could not see, because it stubbed
        the function whose return value was wrong."""
        self._world(sessions)
        state, published = self._state(sessions)

        jobs._HANDLERS["watch.reconcile"](state, {})
        published.clear()
        second = jobs._HANDLERS["watch.reconcile"](state, {})

        assert second == {"users_credited": 0}
        assert published == []


class TestScheduledRowVisibility:
    """The midnight `rows.visibility` tick (issue #102).

    Rows build at 03:30, so a run is far too late to turn a row over: a Monday row would sit on
    people's Home until 03:30 Tuesday, and a weekly-rebuilding row for days. This job is what makes a
    day schedule mean anything, so its ordering, its no-op case and its refusal to promote anything it
    cannot identify are all load-bearing.
    """

    ON = RowSpec(slug="picked", name_template="✨ Picked for You", size=10, placement="both", placement_friends="both")
    OFF = RowSpec(slug="gems", name_template="✨ Hidden Gems", size=10, placement="off", placement_friends="off")
    #: A row whose title cannot be re-rendered without picks, so it matches no spec by title.
    SEEDED = RowSpec(
        slug="seeded",
        name_template="✨ Because you watched {top_seed}",
        size=10,
        placement="off",
        placement_friends="off",
    )
    SHARED = RowSpec(
        slug="crowd",
        name_template="✨ Popular Here",
        size=10,
        shared=True,
        placement="off",
        placement_friends="off",
    )

    ACCOUNT = 555000100
    #: 2026-08-31 is a Monday. Frozen so "shown on Monday" is a fact in these tests, not a coin toss.
    MONDAY = datetime(2026, 8, 31, 12, 0)
    TUESDAY = 2

    @pytest.fixture(autouse=True)
    def _freeze_today(self, monkeypatch):
        import shortlist.server.services.context_builder as cb

        monkeypatch.setattr(cb, "local_now", lambda: self.MONDAY)

    def _state(
        self,
        sessions,
        *,
        calls: list,
        rows,
        merge_fails=False,
        collections=None,
        paused_all=False,
        dry_run=False,
    ):
        """A fake Plex plus a stub `engine_run`, both recording into ONE ordered list.

        One list, not two: the claim this suite has to be able to make is that the share-filter merge
        happened BEFORE any promote, and two separate lists can only show that both occurred.
        """
        marker = row_marker(self.ACCOUNT)
        owned = collections if collections is not None else [("✨ Picked for You" + marker, 42, "shortlist_sarah")]
        objects = [SimpleNamespace(title=title, ratingKey=key) for title, key, _ in owned]
        by_label: dict[str, list] = {}
        for obj, (_, _, label) in zip(objects, owned, strict=True):
            by_label.setdefault(label, []).append(obj)

        config = EngineConfig(rows=list(rows), rows_defined=True, dry_run=dry_run)
        plex = SimpleNamespace(
            sections=lambda: [SimpleNamespace(title="Movies", key=1, type="movie")],
            find_owned_collections=lambda section, label: by_label.get(label, []),
            promote=lambda c, **kw: calls.append(("promote", c.title, kw)),
            demote_all=lambda c, **kw: calls.append(("demote", c.title, kw)) or True,
        )
        ctx = SimpleNamespace(plex=plex, config=config, write_lock=None)

        def fake_engine_run(_ctx, users):
            calls.append(("merge", users))
            return SimpleNamespace(
                error="could not read the plex.tv user list: RuntimeError: plex.tv 503" if merge_fails else None,
                promotion_blockers=[],
                swept_rows={},
                converged=0,
            )

        import shortlist.engine.pipeline as pipeline_mod

        self._patched = (pipeline_mod, pipeline_mod.run)
        pipeline_mod.run = fake_engine_run

        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.secrets import SecretBox
        from shortlist.server.services.sse import EventBus

        secrets = SecretBox(Path(tempfile.mkdtemp()))
        builder = ContextBuilder(sessions, secrets, EventBus())
        if paused_all:
            with sessions() as session:
                SettingsStore(session, secrets).set("paused_all", True)
                session.commit()

        run_service = SimpleNamespace(
            build_context=lambda dry_run, plex_only=False: ctx,
            enabled_profiles=lambda session, user_ids=None: builder.enabled_profiles(session, user_ids),
        )
        return SimpleNamespace(sessions=sessions, run_service=run_service, secrets=secrets)

    @pytest.fixture(autouse=True)
    def _restore_engine_run(self):
        self._patched = None
        yield
        if self._patched:
            module, original = self._patched
            module.run = original

    def _seed(self, sessions, *, rows: dict, paused=False, user_type="shared", deliveries=()):
        from shortlist.server.db.models import Collection, Delivery, User

        with sessions() as session:
            session.query(Collection).delete()
            session.add(
                User(
                    plex_account_id=self.ACCOUNT,
                    username="sarah",
                    slug="sarah",
                    user_type=user_type,
                    enabled=True,
                    prefs={"paused": True} if paused else {},
                )
            )
            for slug, show_days in rows.items():
                session.add(
                    Collection(
                        slug=slug,
                        name=slug,
                        build="shared" if slug == "crowd" else "per_person",
                        enabled=True,
                        show_days=show_days,
                    )
                )
            for slug, key in deliveries:
                session.add(
                    Delivery(collection_slug=slug, user_slug="sarah", library_key="1", rating_key=key, title="x")
                )
            session.commit()

    def _promotes(self, calls):
        return [c for c in calls if c[0] == "promote"]

    # ---- the cheap night ------------------------------------------------------------------

    def test_a_server_that_schedules_nothing_touches_nothing_at_all(self, sessions):
        """The gate that keeps this free for everybody who does not use the feature — which is every
        server until somebody picks days. It has to sit before the Plex client is even built, or the
        "costs nothing" claim in the docs and the job description is false on every night."""
        calls: list = []
        self._seed(sessions, rows={"picked": [], "gems": []})
        built: list = []
        state = self._state(sessions, calls=calls, rows=[self.ON, self.OFF])
        state.run_service.build_context = lambda dry_run, plex_only=False: (
            built.append(1)
            or (_ for _ in ()).throw(AssertionError("build_context must not be reached on a night with no work"))
        )

        result = jobs._HANDLERS["rows.visibility"](state, {})

        assert result["changed"] == []
        assert built == [], "the Plex/plex.tv/TMDB clients must not be constructed for a no-op tick"
        assert calls == []

    def test_a_row_with_no_schedule_never_makes_work(self, sessions):
        """Every row carries `show_days=[]` straight after migration 0088, and a server where nobody
        has scheduled anything must do nothing at midnight — not build a Plex client, not merge a
        filter, nothing."""
        calls: list = []
        self._seed(sessions, rows={"picked": []})
        state = self._state(sessions, calls=calls, rows=[self.ON])
        state.run_service.build_context = lambda dry_run, plex_only=False: (_ for _ in ()).throw(
            AssertionError("an unscheduled row must not converge anything")
        )

        assert jobs._HANDLERS["rows.visibility"](state, {})["changed"] == []

    # ---- the write path -------------------------------------------------------------------

    def test_a_row_that_turned_off_is_taken_off_every_surface(self, sessions):
        """Asserted on the OFF row's OWN collection and its exact flags. An earlier version of this
        test only had a collection for the ON row, so it passed on that row's promotion and would
        have gone green with the off row left up."""
        calls: list = []
        marker = row_marker(self.ACCOUNT)
        # `picked` shows on Monday and already did; `gems` shows on Tuesday and was up yesterday, so
        # today is its transition to hidden.
        self._seed(
            sessions,
            rows={"picked": [1], "gems": [2]},
            deliveries=(("picked", 42), ("gems", 43)),
        )
        # `gems` is scheduled off today: its spec placement is already `off`.
        state = self._state(
            sessions,
            calls=calls,
            rows=[self.ON, self.OFF],
            collections=[
                ("✨ Picked for You" + marker, 42, "shortlist_sarah"),
                ("✨ Hidden Gems" + marker, 43, "shortlist_sarah"),
            ],
        )
        jobs._HANDLERS["rows.visibility"](state, {})

        off = next(c for c in self._promotes(calls) if "Hidden Gems" in c[1])
        assert off[2]["home"] is False
        assert off[2]["shared"] is False
        assert off[2]["recommended"] is False
        on = next(c for c in self._promotes(calls) if "Picked for You" in c[1])
        assert on[2]["shared"] is True, "the row that IS on today must still be promoted"

    def test_a_row_whose_collection_matches_no_spec_is_left_alone(self, sessions):
        """The HIGH finding. A `{top_seed}` title cannot be re-rendered without picks, so if the
        delivery ledger has no key for it the collection matches nothing — and promotion's no-spec
        fallback PROMOTES, which would put a row scheduled OFF onto Home. Under-showing is the safe
        direction for this job, unlike a run."""
        calls: list = []
        marker = row_marker(self.ACCOUNT)
        self._seed(sessions, rows={"seeded": [2]}, deliveries=())  # no ledger key
        state = self._state(
            sessions,
            calls=calls,
            rows=[self.SEEDED],
            collections=[("✨ Because you watched Heat" + marker, 77, "shortlist_sarah")],
        )

        jobs._HANDLERS["rows.visibility"](state, {})

        assert self._promotes(calls) == [], "an unidentifiable collection must not be promoted onto Home"

    def test_the_share_filters_are_merged_before_anything_is_promoted(self, sessions):
        """plex-safety rule 1, asserted as ORDER rather than as "both happened"."""
        calls: list = []
        self._seed(sessions, rows={"picked": [1]}, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.ON])

        jobs._HANDLERS["rows.visibility"](state, {})

        kinds = [c[0] for c in calls]
        assert "merge" in kinds and "promote" in kinds
        assert kinds.index("merge") < kinds.index("promote"), "a row must never appear before its excludes"

    def test_a_failed_filter_merge_promotes_nothing_at_all(self, sessions):
        calls: list = []
        self._seed(sessions, rows={"picked": [1]}, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.ON], merge_fails=True)

        with pytest.raises(RuntimeError, match="503"):
            jobs._HANDLERS["rows.visibility"](state, {})

        assert self._promotes(calls) == []

    # ---- the exclusions -------------------------------------------------------------------

    def test_pause_all_stops_it_like_every_other_scheduled_task(self, sessions):
        """The Danger Zone kill switch. `enabled_profiles` returns [] when `paused_all` is set, and
        this job must go through it rather than hand-rolling the roster — it is the first scheduled
        task that writes to people's shelves, so a kill switch it ignores is the whole point."""
        calls: list = []
        self._seed(sessions, rows={"picked": [1]}, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.ON], paused_all=True)

        jobs._HANDLERS["rows.visibility"](state, {})

        assert self._promotes(calls) == [], "pause all must stop the midnight tick too"

    def test_a_paused_persons_rows_are_not_put_back_by_a_schedule(self, sessions):
        """Two independent reasons a row is hidden, and only one of them is lifting."""
        calls: list = []
        self._seed(sessions, rows={"picked": [1]}, paused=True, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.ON])

        jobs._HANDLERS["rows.visibility"](state, {})

        assert self._promotes(calls) == []

    def test_a_restricted_managed_account_is_skipped(self, sessions):
        """Plex refuses a label filter for an account with a parental profile, so it can never have a
        private row. `enabled_profiles` drops it; a hand-rolled roster did not."""
        from shortlist.server.db.models import Collection, User

        calls: list = []
        self._seed(sessions, rows={"picked": [1]}, deliveries=(("picked", 42),))
        with sessions() as session:
            session.query(User).filter_by(slug="sarah").update(
                {"user_type": "managed", "restricted": True, "restriction_profile": "little_kid"}
            )
            session.query(Collection).count()
            session.commit()
        state = self._state(sessions, calls=calls, rows=[self.ON])

        jobs._HANDLERS["rows.visibility"](state, {})

        assert self._promotes(calls) == []

    # ---- shared rows, and the preview -----------------------------------------------------

    def test_a_shared_row_is_converged_too(self, sessions):
        """A shared row is ONE public collection under its own label, so it goes through a different
        promote path than a per-person row — and nothing exercised it."""
        calls: list = []
        self._seed(sessions, rows={"crowd": [2]})
        state = self._state(
            sessions,
            calls=calls,
            rows=[self.SHARED],
            collections=[("✨ Popular Here", 99, self.SHARED.label)],
        )

        jobs._HANDLERS["rows.visibility"](state, {})

        promoted = self._promotes(calls)
        assert promoted, "the shared row's public collection was never converged"
        assert promoted[0][2]["home"] is False and promoted[0][2]["shared"] is False

    def test_a_dry_run_writes_nothing_and_still_records_what_it_would_do(self, sessions):
        """Rule 8 for the preview, rule 10 for the audit — a dry run that leaves no event is a
        visibility change nobody can account for."""
        calls: list = []
        self._seed(sessions, rows={"picked": [1]}, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.ON])

        result = jobs._HANDLERS["rows.visibility"](state, {"dry_run": True})

        assert calls == [], "a dry run must not merge filters or promote anything"
        assert result["dry_run"] is True
        assert result["changed"] == ["picked"]
        with sessions() as session:
            audited = [e.message.get("dry_run") for e in session.query(Event).filter_by(scope="rows.visibility")]
        assert audited == [True]

    def test_every_scheduled_row_is_converged_on_the_same_pass(self, sessions):
        """One pass settles every scheduled row, each with its OWN placement — the case that only
        exists once a server has more than one schedule."""
        calls: list = []
        marker = row_marker(self.ACCOUNT)
        self._seed(sessions, rows={"picked": [1], "gems": [2]}, deliveries=(("picked", 42), ("gems", 43)))
        state = self._state(
            sessions,
            calls=calls,
            rows=[self.ON, self.OFF],
            collections=[
                ("✨ Picked for You" + marker, 42, "shortlist_sarah"),
                ("✨ Hidden Gems" + marker, 43, "shortlist_sarah"),
            ],
        )

        result = jobs._HANDLERS["rows.visibility"](state, {})

        assert result["changed"] == ["gems", "picked"]
        on = next(c for c in self._promotes(calls) if "Picked for You" in c[1])[2]
        off = next(c for c in self._promotes(calls) if "Hidden Gems" in c[1])[2]
        assert on["shared"] is True, "the row on today"
        assert (off["shared"], off["home"], off["recommended"]) == (False, False, False), "the row off today"

    def test_pause_all_leaves_the_work_owed_rather_than_recording_it_as_done(self, sessions):
        """Found by live testing. The pass must stop BEFORE the filter merge and record nothing, so
        lifting the pause simply recomputes and applies it. An earlier version cached the answer here
        and left the row visible on its off day for good."""
        calls: list = []
        self._seed(sessions, rows={"picked": [2]}, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.OFF], paused_all=True)

        result = jobs._HANDLERS["rows.visibility"](state, {})

        assert calls == [], "pause all must stop the merge too, not just the promote"
        assert "paused" in result["detail"].lower()

    def test_the_owners_own_row_uses_the_owner_flags_not_the_friends_ones(self, sessions):
        """`_promote_one` branches three ways on user type and only the `shared` cell was covered.

        Asserted on a row that is ON today, because that is the only state where the branches differ:
        an off row is all-False whoever it belongs to, so an off row cannot tell them apart.
        """
        calls: list = []
        marker = row_marker(self.ACCOUNT)
        self._seed(sessions, rows={"picked": [1]}, user_type="owner", deliveries=(("picked", 42),))
        state = self._state(
            sessions, calls=calls, rows=[self.ON], collections=[("✨ Picked for You" + marker, 42, "shortlist_sarah")]
        )

        jobs._HANDLERS["rows.visibility"](state, {})

        flags = next(c for c in self._promotes(calls) if "Picked for You" in c[1])[2]
        assert flags["home"] is True, "the owner's own row belongs on the OWNER's Home"
        assert flags["shared"] is False, "and never on Friends' Home"

    def test_an_unrestricted_managed_user_goes_through_the_friends_flags(self, sessions):
        """Plex's own docs are explicit that Shared Users' Home covers managed users too, so a managed
        account without a parental profile is treated like a friend here, not like the owner. Routing
        it through the owner flag would hide its row from the person it belongs to."""
        calls: list = []
        marker = row_marker(self.ACCOUNT)
        self._seed(sessions, rows={"picked": [1]}, user_type="managed", deliveries=(("picked", 42),))
        state = self._state(
            sessions, calls=calls, rows=[self.ON], collections=[("✨ Picked for You" + marker, 42, "shortlist_sarah")]
        )

        jobs._HANDLERS["rows.visibility"](state, {})

        flags = next(c for c in self._promotes(calls) if "Picked for You" in c[1])[2]
        assert flags["shared"] is True
        assert flags["home"] is False, "a managed user's row must never land on the OWNER's Home"

    def test_a_collection_two_rows_claim_is_left_alone_rather_than_arbitrated(self, sessions):
        """The midnight job is where guessing wrong is worst: arbitrating an ambiguous ledger key
        hands the collection the OTHER row's placement, which can show a row scheduled off.

        Dropping it sends it to the title map; with no title stamped either, `skip_unmatched` leaves
        it exactly as it is — the conservative end of the branch.
        """
        calls: list = []
        marker = row_marker(self.ACCOUNT)
        self._seed(
            sessions,
            rows={"picked": [1], "gems": [2]},
            # Both rows name the SAME collection: a stale entry that survived an out-of-band delete,
            # plus Plex handing the freed rowid to a new collection.
            deliveries=(("picked", 42), ("gems", 42)),
        )
        state = self._state(
            sessions,
            calls=calls,
            rows=[self.ON, self.OFF],
            collections=[("✨ Because you watched Heat" + marker, 42, "shortlist_sarah")],
        )

        jobs._HANDLERS["rows.visibility"](state, {})

        assert self._promotes(calls) == [], "an ambiguous key must not decide a row's surfaces"

    def test_clearing_the_last_schedule_on_the_server_still_puts_that_row_back(self, sessions):
        """The gate asks "does any row narrow its days" — and after a clear, none does. Without the
        row named in the payload the pass would skip, and the row it was meant to restore would stay
        hidden until 03:30. This is the ONLY path that covers it.
        """
        calls: list = []
        marker = row_marker(self.ACCOUNT)
        self._seed(sessions, rows={"picked": []}, deliveries=(("picked", 42),))
        state = self._state(
            sessions, calls=calls, rows=[self.ON], collections=[("✨ Picked for You" + marker, 42, "shortlist_sarah")]
        )

        result = jobs._HANDLERS["rows.visibility"](state, {"row": "picked"})

        assert self._promotes(calls), "the row whose schedule was just cleared was never put back"
        assert result["collections"] == 1

    def test_without_the_row_in_the_payload_an_unscheduled_server_does_nothing(self, sessions):
        """The other half of the pair: the midnight cron passes no row, so a server that schedules
        nothing must still cost one query."""
        calls: list = []
        self._seed(sessions, rows={"picked": []}, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.ON])

        assert jobs._HANDLERS["rows.visibility"](state, {})["changed"] == []
        assert calls == []

    def test_the_shelf_order_is_left_to_the_nightly_run(self, sessions):
        """`engine_run` would otherwise run its ordering phase here EVERY night, writing the
        `shelf.order` events that the "something else is reordering your shelf" notification counts
        (3 in a day trips it) — and ordering before promoting, so a row shown today moves again at
        03:30. Position is the run's job; this pass only decides visibility."""
        calls: list = []
        seen: list = []
        self._seed(sessions, rows={"picked": [1]}, deliveries=(("picked", 42),))
        state = self._state(sessions, calls=calls, rows=[self.ON])
        inner = state.run_service.build_context
        state.run_service.build_context = lambda dry_run, plex_only=False: (
            seen.append(inner(dry_run=dry_run)) or seen[-1]
        )

        jobs._HANDLERS["rows.visibility"](state, {})

        assert seen[0].config.manage_shelf_order is False
