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
