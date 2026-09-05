"""Run service — the server's run orchestrator, and nothing else.

Executes runs in a worker thread (the engine is sync) and emits SSE progress. A `runs` row is
inserted BEFORE execution so a container restart can see and abort orphaned runs. Cancellation,
the one-run-at-a-time lock and the Plex writer lock live here; everything a run *needs* is a
collaborator this module owns and delegates to:

* ``context_builder.ContextBuilder`` — clients, engine config, user profiles.
* ``run_log.RunLogBuffer`` — the live narration tail and its batched durable writes.
* ``watch_sync.WatchSync`` — the watched-set cache top-up and the nightly read-only sweep.
* ``run_persistence`` — run/run_user/pick/event rows, the approval inbox, and retention.

The thin methods below those section banners exist because `RunService` is the public surface:
`app.state.run_service` is how the API layer, the job queue and the scheduler reach all of this.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.context import EngineContext
from shortlist.engine.pipeline import run as engine_run
from shortlist.server.db.models import Collection, Run
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.services import jobs, run_persistence
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.run_log import RunLogBuffer
from shortlist.server.services.run_persistence import HIT_WINDOW_DAYS  # noqa: F401  (re-export)
from shortlist.server.services.sse import EventBus
from shortlist.server.services.watch_sync import WatchSync

# HIT_WINDOW_DAYS moved to `run_persistence` with the hit-rate reconcile that owns it, and is
# re-exported above because `services/report_service.py` imports it from here.


class RunService:
    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus, config_dir: Path, secret_box):
        self._sessions = session_factory
        self._bus = bus
        self._config_dir = config_dir
        self._secrets = secret_box
        self._ctx = ContextBuilder(session_factory, secret_box, bus)
        self._lock = asyncio.Lock()  # one run at a time; nightly + manual runs must not overlap
        # SQLite is single-writer, but the engine finishes users on a thread POOL — so the live
        # per-user persist (below) must serialize its commits across those worker threads.
        self._persist_lock = threading.Lock()
        # run_id -> cancel flag for the one in-flight run, so the /cancel endpoint can ask the engine
        # to stop. The engine checks it before each user (cooperative), so an in-flight user finishes.
        self._cancels: dict[int, threading.Event] = {}
        self._tasks: set[asyncio.Task] = set()  # strong refs so in-flight runs aren't GC'd
        self._log = RunLogBuffer(session_factory)
        self._watch = WatchSync(session_factory, bus)
        # `app.state`, assigned by `main.create_app` right after this object exists. Only used to
        # drain the job queue when a run ends; None in tests that build a RunService directly.
        self.state = None

    # -- run narration (delegated to RunLogBuffer) ---------------------------------------

    def _new_run_log(self, run_id: int) -> Callable[[dict], None]:
        return self._log.start(run_id)

    def flush_run_log(self, run_id: int) -> None:
        self._log.flush(run_id)

    def run_log(self, run_id: int, after_seq: int | None = None) -> list[dict]:
        return self._log.lines(run_id, after_seq)

    # -- context assembly (delegated to ContextBuilder) ----------------------------------

    def build_context(
        self,
        *,
        dry_run: bool,
        loop: asyncio.AbstractEventLoop | None = None,
        run_id: int | None = None,
        log_sink: Callable[[dict], None] | None = None,
        collection_ids: list[int] | None = None,
        plex_only: bool = False,
    ) -> EngineContext:
        """The engine context for any Plex-touching path.

        ``plex_only`` builds just the PMS + plex.tv + history source (see
        ``ContextBuilder.build_plex_only``) — for the handlers that only walk collections under a
        label and would otherwise be coupled to TMDB, the LLM provider and the poster studio.
        """
        # Safe-mode chokepoint: EVERY Plex-touching path (runs AND the manual row
        # delete/rename/poster-reset/disable-user reconciles) gets its context here, so forcing
        # dry-run on the built context makes SHORTLIST_DRY_RUN cover all of them. Callers read the
        # effective value back off `ctx.config.dry_run`.
        dry_run = force_dry_run() or dry_run
        if plex_only:
            return self._ctx.build_plex_only(dry_run=dry_run)
        return self._ctx.build(
            dry_run=dry_run, loop=loop, run_id=run_id, log_sink=log_sink, collection_ids=collection_ids
        )

    def build_requests_context(self):
        """Requests config + TMDB client for the approval inbox's manual send — no Plex/LLM I/O."""
        return self._ctx.build_requests_only()

    def enabled_profiles(self, session: Session, user_ids: list[int] | None = None):
        return self._ctx.enabled_profiles(session, user_ids)

    def user_history(self, user_id: int, *, limit: int = 25) -> list[dict] | None:
        return self._ctx.user_history(user_id, limit=limit)

    def user_watched(
        self,
        user_id: int,
        *,
        q: str = "",
        media_type: str = "",
        library: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> dict | None:
        return self._ctx.user_watched(user_id, q=q, media_type=media_type, library=library, limit=limit, offset=offset)

    # -- watch-cache orchestration (delegated to WatchSync) -------------------------------

    def refresh_watched(self, ctx, profile, *, force_full: bool = False, sweep_dead: bool = False) -> list:
        return self._watch.refresh_watched(ctx, profile, force_full=force_full, sweep_dead=sweep_dead)

    def _has_a_row_in_scope(self, ctx, profile) -> bool:
        return self._watch.has_a_row_in_scope(ctx, profile)

    def sync_watched_background(self) -> None:
        """Fire sync_watched as a tracked background task (the reference is kept so it isn't GC'd) —
        for the dashboard's manual 'Sync now'."""
        task = asyncio.create_task(self.sync_watched())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def sync_watched(self) -> None:
        """The nightly read-only watch sweep. The four collaborators are resolved HERE, per call, not
        captured when `WatchSync` was built — `build_context` in particular is replaced wholesale by
        tests and by nothing else owning it."""
        await self._watch.sync_watched(
            build_context=self.build_context,
            enabled_profiles=self.enabled_profiles,
            reconcile_watched=self._reconcile_watched,
            run_lock=self._lock,
        )

    # -- execution -----------------------------------------------------------------------

    async def start_run(
        self,
        *,
        trigger: str,
        dry_run: bool,
        user_ids: list[int] | None = None,
        collection_ids: list[int] | None = None,
    ) -> int:
        """Insert the runs row and launch execution as a background task; returns run id.

        ``collection_ids`` scopes the run to specific rows (a per-row scheduled run builds only its
        own rows); ``None`` builds every enabled row. The leak-safe privacy sync always covers every
        account regardless, so rows not built this run stay hidden.
        """
        if force_dry_run() and not dry_run:
            logger.warning("SHORTLIST_DRY_RUN is set — forcing this {} run to dry-run (no Plex writes)", trigger)
            dry_run = True
        with self._sessions() as session:
            # The rows this run will build, recorded the moment it is QUEUED rather than when it
            # starts. A queued run is a real thing an operator sits watching — it had no scope
            # recorded yet, so its page had nothing to show and said so. `collection_ids` is the
            # scope a "run selected rows" press chose; without it, every enabled row.
            wanted = session.query(Collection).filter(Collection.enabled)
            if collection_ids:
                wanted = wanted.filter(Collection.id.in_(collection_ids))
            run = Run(
                trigger=trigger,
                dry_run=dry_run,
                status="queued",
                stats={
                    # Who it will build for, recorded here for the same reason as the rows below: a
                    # QUEUED run is exactly when someone is watching the page, and without this its
                    # rows opened onto "0 succeeded" and an empty list.
                    "expected_users": [
                        {"slug": p.slug, "username": p.username, "display_name": p.nickname or p.username}
                        for p in self.enabled_profiles(session, user_ids)
                    ],
                    "expected_rows": [
                        {"slug": row.slug, "title": row.name_template or row.name, "build": row.build}
                        for row in wanted.order_by(Collection.sort_order, Collection.id).all()
                    ],
                },
            )
            session.add(run)
            session.commit()
            run_id = run.id
        self._cancels[run_id] = threading.Event()  # armed here so /cancel works the instant it's queued
        task = asyncio.create_task(self._execute(run_id, dry_run, user_ids, collection_ids))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _execute(
        self, run_id: int, dry_run: bool, user_ids: list[int] | None, collection_ids: list[int] | None = None
    ) -> None:
        loop = asyncio.get_running_loop()
        try:
            await self._run_locked(run_id, dry_run, user_ids, collection_ids, loop)
        finally:
            # A run holds the Plex writer lock, so `_plex_busy` parks every writer job behind it.
            # Nothing used to tell the queue when that ended, leaving jobs idle until the worker's
            # next 60s tick — measured at 29s of doing nothing on a real server. Draining here is a
            # latency fix only; the tick stays as the backstop.
            #
            # In a `finally`, so a run that ERRORED or was CANCELLED drains too: it released the
            # lock just the same, and an aborted run is exactly when a `privacy.sync` is most likely
            # to be queued and most important — that is the leak direction.
            #
            # After `_run_locked` returns, so the lock is released and `_cancels` is empty: draining
            # while `is_running()` is still true would re-park every writer and achieve nothing.
            await self._drain_jobs_after_run(run_id)

    async def _run_locked(
        self,
        run_id: int,
        dry_run: bool,
        user_ids: list[int] | None,
        collection_ids: list[int] | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        async with self._lock:
            cancel = self._cancels.get(run_id)
            # Cancelled while it sat in the QUEUE, behind another run holding this lock. The flag is
            # only ever read between users, which this run has not reached — so pressing Cancel on a
            # queued run did nothing visible until the run ahead of it finished, which on a real
            # server is half an hour of "Stopping…". Nothing has been built yet, so there is nothing
            # to unwind: mark it aborted and give the lock straight back.
            if cancel is not None and cancel.is_set():
                try:
                    with self._sessions() as session:
                        run = session.get(Run, run_id)
                        if run is not None:
                            run.status = "aborted"
                            run.finished_at = datetime.now(UTC)
                            session.commit()
                    logger.info("run {} was cancelled before it started — nothing was built", run_id)
                    self._bus.publish("run.progress", {"run_id": run_id, "status": "aborted"})
                    self._bus.publish("run.finished", {"run_id": run_id, "status": "aborted"})
                finally:
                    # The SAME cleanup the main path's `finally` does, and it is not optional here.
                    # `is_running()` is `bool(self._cancels)`, and the job worker reads it to decide
                    # whether to claim WRITER jobs — so returning without dropping the Event left this
                    # run in flight for the life of the process, and `privacy.sync`, `user.disable`,
                    # `user.remove` and the roster syncs were never claimed again. Silently: the queue
                    # looks backed up rather than broken, and on a Watchtower host a departed user's
                    # `label!=` excludes simply stop being pruned until someone restarts the container.
                    self._cancels.pop(run_id, None)
                    self.flush_run_log(run_id)
                return
            self._bus.publish("run.progress", {"run_id": run_id, "status": "running"})
            try:
                # Inside the try so a failure here (e.g. reading users) still marks the run errored
                # AND runs the finally that frees the cancel Event — never leaves a run stuck "running".
                self._mark_started(run_id)
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    profiles = self.enabled_profiles(session, user_ids)
                    run.stats = {
                        **(run.stats or {}),
                        "expected_users": [
                            {"slug": p.slug, "username": p.username, "display_name": p.nickname or p.username}
                            for p in profiles
                        ],
                    }
                    session.commit()
                ctx = self.build_context(
                    dry_run=dry_run,
                    loop=loop,
                    run_id=run_id,
                    log_sink=self._new_run_log(run_id),
                    collection_ids=collection_ids,
                )
                # Which rows this run will build, recorded UP FRONT — the row twin of
                # `expected_users` above. Without it the page cannot know a run's SCOPE until the
                # first person finishes, because scope only exists in `rows_considered`, which is
                # written per user as each completes. A running run therefore had nothing to draw
                # and said so, which is no way to watch a run that takes half an hour.
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    run.stats = {
                        **(run.stats or {}),
                        "expected_rows": [
                            {
                                "slug": spec.slug,
                                "title": spec.name_template,
                                "build": "shared" if spec.shared else "per_person",
                            }
                            for spec in (*ctx.config.per_person_rows(), *ctx.config.shared_rows())
                            if ctx.config.should_build(spec)
                        ],
                    }
                    session.commit()
                # Cooperative cancel: the engine checks this before each user and skips the rest. An
                # in-flight user still finishes (per-user transactional), and the privacy merge +
                # promote still run for who was delivered, so a cancel leaves a consistent server.
                if cancel is not None:
                    ctx.cancelled = cancel.is_set
                # "Run now" for one person hands the engine a subset of the roster. Tell it so, or it
                # reads that subset as the whole server and builds/judges shared rows against it.
                ctx.config.users_scoped = user_ids is not None
                # Persist each user's results the moment they finish, so the run page fills in person by
                # person instead of staying empty until the whole run ends (the end-of-run persist below
                # is the backstop + reconciler).
                ctx.on_user_done = lambda profile, user_report: self._persist_user_live(
                    run_id, profile, user_report, dry_run
                )
                # Fill each person's history from the cache BEFORE the engine runs. The run used to
                # do its own complete per-user read — the same read the nightly sync had already
                # done hours earlier — which was half the total cost of a night.
                await loop.run_in_executor(None, self._watch.prefill_history, ctx, profiles, run_id)  # scoped inside
                # What is in each person's rows RIGHT NOW, before the engine rebuilds them. This is
                # the shelf they were actually looking at during the window they were watching in,
                # and it stops existing the moment the run persists tonight's picks — so it has to be
                # read here, not at the reconcile below. See `reconcile_watched`.
                live_picks = await loop.run_in_executor(None, self._live_pick_ids)
                # The ONE-WRITER lock, held for the whole engine run (plex-safety rule 3 + design
                # doc §2 principle 6). Jobs deferring to runs via `_plex_busy` is only half of it:
                # without this, a writer job already mid-flight kept merging share filters straight
                # through the start of a run, and the second of two read-modify-write merges drops
                # the first's `label!=shortlist_<slug>` excludes with nothing left to catch it.
                async with jobs.plex_writer_lock():
                    report = await loop.run_in_executor(None, engine_run, ctx, profiles)
                aborted = cancel is not None and cancel.is_set()
                self._persist_report(run_id, report, status="aborted" if aborted else None)
                # The engine filled each profile's history in place, so this is the one moment we hold
                # both "what we recommended" and "what they have since watched". A dry run is a
                # preview and mutates nothing, matching the rest of persistence.
                if not dry_run:
                    # Guarded, and the guard is the point. Every row is already built and delivered
                    # on Plex by the time this runs; crediting is bookkeeping over our own database.
                    # An exception here used to land in the `except` below and mark a completely
                    # successful run as ERROR — the same shape as the retention prune, which was
                    # moved out of the persist transaction for exactly this reason. Whatever this
                    # pass misses, the nightly sync reaches from the same records.
                    try:
                        self._reconcile_watched(profiles, live_picks)
                    except Exception as e:
                        logger.warning(
                            "run {}: crediting watches failed ({}) — the run itself is unaffected",
                            run_id,
                            type(e).__name__,
                        )
                status = "aborted" if aborted else ("ok" if report.ok else "error")
            except Exception as e:
                logger.exception("run {} failed", run_id)
                self._mark_run_error(run_id, {"error": f"{type(e).__name__}: {e}"})
                self._bus.publish(
                    "run.finished", {"run_id": run_id, "status": "error", "error": f"{type(e).__name__}: {e}"}
                )
                return
            finally:
                self._cancels.pop(run_id, None)
                # In the `finally` so a crashed run still leaves its narration behind — that is
                # exactly the run whose log someone will want to read.
                self.flush_run_log(run_id)
            # Carry the reason on failure so the UI (e.g. the wizard's first run) can show it inline
            # rather than only pointing at the Runs page.
            self._bus.publish(
                "run.finished",
                {"run_id": run_id, "status": status, "error": None if report.ok else report.error},
            )

    async def _drain_jobs_after_run(self, run_id: int) -> None:
        """Start whatever this run was blocking, now that it is not. Never raises.

        The queue is durable and the worker re-ticks regardless, so a failure here costs latency and
        nothing else — it must never turn a finished run into a failed one.
        """
        if self.state is None:
            return
        try:
            await jobs.drain_now(self.state, f"run {run_id} finished")
        except Exception as e:  # defensive: drain_now already swallows, this is the belt
            logger.warning("run {}: could not drain the job queue ({})", run_id, type(e).__name__)

    def is_running(self) -> bool:
        """Is an engine run executing in THIS process right now?

        Read by the job queue: runs and jobs are separate systems but share one Plex and one
        throttled plex.tv, and interleaving their writes risks a job merging a share filter from a
        roster snapshot a run is halfway through changing. `_cancels` holds exactly the runs this
        process is executing — an entry is added the moment a run is QUEUED (`start_run`, so /cancel
        works before execution begins) and removed when it finishes — so it is the authoritative
        in-flight set, unlike the DB status (which a crashed process leaves stale until the boot
        reap). Counting a queued run as running is deliberately conservative: it is about to take the
        writer lock, and letting a job in first only to have it lose the race achieves nothing.

        A CANCELLED run still counts. Cancellation is cooperative: the engine stops taking new users
        but then falls through to the privacy merge and promote for everyone already delivered.
        Treating "cancel requested" as "finished" would open the job queue precisely inside the
        merge→promote window that rule 1 exists to protect, letting a second share-filter writer
        clobber an exclude the run had just added.
        """
        return bool(self._cancels)

    def _mark_started(self, run_id: int) -> None:
        """Mark a run as executing, and stamp WHEN — the moment the engine actually began.

        Its own method rather than two lines inside `_run_locked` because the two must not drift:
        `started_at` is stamped at INSERT (when the run was asked for), so a run that waited behind
        another for nine minutes and was then cancelled reported nine minutes of work it had never
        done. Anything that sets the status to running owes the timestamp too, and a named method is
        what makes that visible — this status write has already been moved once, for the
        cancel-while-queued fix, and a stamp left behind would read as "never ran" on every run.
        """
        with self._sessions() as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            run.status = "running"
            run.began_at = datetime.now(UTC)
            session.commit()

    def cancel_run(self, run_id: int) -> bool:
        """Ask the in-flight run to stop. Returns True if a running run was signalled, False if that
        run isn't currently executing here (already finished, never ran, or a stale id).

        Cooperative: the engine stops before the next user, so a user already being built finishes and
        the privacy merge + promote still run for everyone delivered — the server stays consistent.
        """
        event = self._cancels.get(run_id)
        if event is None:
            return False
        if event.is_set():
            return True  # already stopping — pressing again is a no-op, and a no-op is not an error
        event.set()
        # Recorded on the RUN, not just in memory and an SSE event, so any client can see it. The
        # button used to read "Stopping..." off local mutation state alone: a page refresh forgot,
        # offered a live-looking Cancel, and every press after that 409'd with "this run isn't
        # currently running" — the opposite of the truth, on a run that was very much running.
        # Best-effort: the Event above IS the cancellation, and it has already taken effect. This row
        # is a convenience so a reloaded page knows. SQLite is single-writer and the run's own thread
        # is writing users and log lines, so a busy timeout here must not surface as a failed cancel —
        # which would be this endpoint lying about what it did, the very thing it was fixed to stop.
        try:
            with self._sessions() as session:
                run = session.get(Run, run_id)
                if run is not None:
                    # A QUEUED run is finished here and now. It has not started, holds nothing and has
                    # written nothing, so there is nothing to unwind and nothing to be careful about.
                    #
                    # It used to be marked aborted only once it ACQUIRED the Plex writer lock — which
                    # is exactly what it is waiting for. Queue two runs, cancel both, and the second
                    # sat on "Stopping…" until the FIRST one finished, because the code meant to stop
                    # it could not run until the thing it was queued behind got out of the way. The
                    # flag is still set below, so if it is already mid-start it bails there too.
                    if run.status == "queued":
                        run.status = "aborted"
                        run.finished_at = datetime.now(UTC)
                        run.stats = {**(run.stats or {}), "cancel_requested": True}
                        session.commit()
                        logger.info("run {} cancelled while queued — it never started", run_id)
                        self._bus.publish("run.progress", {"run_id": run_id, "status": "aborted"})
                        self._bus.publish("run.finished", {"run_id": run_id, "status": "aborted"})
                        return True
                    run.stats = {**(run.stats or {}), "cancel_requested": True}
                    session.commit()
        except Exception as e:
            logger.warning("run {}: cancel accepted but not recorded ({})", run_id, type(e).__name__)
        self._bus.publish("run.progress", {"run_id": run_id, "status": "cancelling"})
        logger.info("run {} cancel requested", run_id)
        return True

    def _mark_run_error(self, run_id: int, stats: dict) -> None:
        """Force a run to a finished error state with the given stats (a build failure)."""
        with self._sessions() as session:
            run = session.get(Run, run_id)
            run.status = "error"
            run.finished_at = datetime.now(UTC)
            run.stats = stats
            session.commit()

    # -- persistence + audit (delegated to run_persistence) -------------------------------

    def _reconcile_watched(
        self, profiles, live_picks: dict[int, set[int]] | None = None, *, complete_read: bool = False
    ) -> None:
        run_persistence.reconcile_watched(self._sessions, profiles, live_picks, complete_read=complete_read)

    def _live_pick_ids(self) -> dict[int, set[int]]:
        """What is in everyone's rows right now — read BEFORE a run rebuilds them (see
        `reconcile_watched`, which cannot ask this question after the fact)."""
        with self._sessions() as session:
            return run_persistence.live_pick_ids(session)

    def _persist_user_live(self, run_id: int, profile, user_report, dry_run: bool) -> None:
        run_persistence.persist_user_live(self._sessions, self._persist_lock, run_id, profile, user_report, dry_run)

    def _persist_report(self, run_id: int, report, *, status: str | None = None, error: str | None = None) -> None:
        run_persistence.persist_report(self._sessions, run_id, report, status=status, error=error)

    # The retention prunes are NOT aliased here: everything that runs them — the `maintenance.prune`
    # handler and the tests — calls `run_persistence` directly. The inbox write still is, because
    # `tests/unit/test_request_queue.py` drives it through the class.
    _persist_request_queue = staticmethod(run_persistence.persist_request_queue)
