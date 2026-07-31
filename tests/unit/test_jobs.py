"""The durable job queue: claim, retry, recover, and stay out of a run's way.

Every maintenance action used to be a fire-and-forget executor call — no record, no retry, nowhere
an operator would see it fail. A disable cleanup lost to a Plex outage was never retried by anything,
because no run revisits a disabled user, so those rows stayed on Plex for ever.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from shortlist.engine.delivery import row_marker
from shortlist.engine.models import EngineConfig, RowSpec
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
        assert set(jobs.KINDS) == {"sync.check", "privacy.sync", "sync.users", "sync.history", "backup.take"}

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
        assert readers == {"sync.history", "backup.take"}
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

        from shortlist.server.api import users as users_api

        assert jobs.BY_KIND["sync.users"].writes_plex is True
        # The reason, asserted rather than trusted: if the rename ever moves out of this handler the
        # flag can be revisited, and this points at where to look.
        source = inspect.getsource(users_api.sync_users_from_state)
        assert "_rename_after_nickname" in source

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
        ctx = SimpleNamespace(plex=plex, config=SimpleNamespace(label_prefix="shortlist", dry_run=False))
        state = SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run: ctx))

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
            if merge_fails:
                raise RuntimeError("plex.tv 503")
            return None

        import shortlist.engine.pipeline as pipeline_mod

        self._patched = (pipeline_mod, pipeline_mod.run)
        pipeline_mod.run = fake_engine_run
        return SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run: ctx))

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
        return SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run: ctx))

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

        monkeypatch.setattr(pipeline_mod, "run", lambda ctx, users: None)

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
        ctx = SimpleNamespace(plex=plex, config=EngineConfig(), write_lock=None)
        return SimpleNamespace(sessions=sessions, run_service=SimpleNamespace(build_context=lambda dry_run: ctx))

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
