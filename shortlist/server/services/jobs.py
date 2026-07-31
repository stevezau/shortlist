"""Durable background jobs — the queue for maintenance work that must not be lost.

Before this, every maintenance action (disable cleanup, row reconciles, share-filter writes) was a
fire-and-forget executor call: no record, no retry, nowhere an operator would see it fail. If Plex
was down — or the container restarted mid-write — the work vanished silently. A user disabled during
an outage kept their rows on Plex for ever, because no later run revisits a disabled user.

**Why a table and not a library.** Every brokerless Python queue was evaluated (2026-07-28) and
rejected: Celery/RQ/arq/dramatiq/procrastinate all need Redis or Postgres, which is a deployment
regression for a single-container self-hosted app. Huey is the only brokerless contender and its own
documentation says it "does not guarantee at-least-once delivery... does not do acknowledgement of
completed tasks" — a task popped when the process dies is LOST, which is the exact failure this
exists to prevent. It also needs a second OS process. Meanwhile ``scheduler.py`` already states the
pattern this extends: *"the runs table is the durable queue."*

Runs deliberately do NOT live here. A run is a long, user-facing operation with its own page, live
progress, per-user results and a cancel button; a job is a short mechanical fix-up. What they share
is that neither may write to Plex while the other is (see ``_plex_busy``).

Every handler MUST be idempotent: a job interrupted mid-flight is requeued and replayed on the next
boot, and there is no way to know how far it got.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import text

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.delivery import row_marker
from shortlist.server.db.models import Event, Job
from shortlist.server.settings_store import SettingsStore

# A job still marked `running` this long after it started is presumed dead — its process is gone.
# Generous on purpose: a real sync check on a large server legitimately takes minutes, and requeuing
# a job that is merely slow means running it twice.
STALE_AFTER = timedelta(minutes=30)

# Backoff between attempts, indexed by attempt number. A Plex/plex.tv outage is usually minutes, so
# the tail is deliberately long rather than hammering a server that is already unhappy.
_BACKOFF_S = (30, 300, 900)

Handler = Callable[[object, dict], dict]  # (app.state, payload) -> result

_HANDLERS: dict[str, Handler] = {}


@dataclass(frozen=True)
class JobKind:
    """What a job kind IS, for the Jobs page — the catalogue an operator browses.

    Kept here rather than in the SPA so the labels, the descriptions and (critically) the `manual`
    allow-list have one definition. A kind the UI can name but the API refuses to run, or vice
    versa, is exactly the drift this avoids.
    """

    kind: str
    label: str
    description: str
    manual: bool  # may the UI trigger it from a button?
    # Does this kind WRITE to Plex or plex.tv? The load-bearing flag on this dataclass.
    #
    # Share-filter writes are read-modify-write MERGES (plex-safety rule 3): two of them running at
    # once both read the same "before" and the second silently drops the first's excludes — the bug
    # class §12 of jobs-and-runs-design.md exists to track. Writers therefore take an exclusive lock
    # that engine runs also hold; read-only kinds run concurrently up to `jobs.max_parallel_readonly`.
    #
    # Every kind must declare this deliberately. A test asserts the catalogue is exhaustive, so
    # adding a kind without thinking about it fails CI rather than silently defaulting to "safe".
    writes_plex: bool = True
    schedule_job_id: str | None = None  # APScheduler id, when this kind runs on a timer
    # The settings key holding this kind's cron, so the Schedule view can edit it without a
    # hard-coded map from kind to key. Blank when the kind has no schedule at all.
    schedule_setting: str = ""
    # Is the schedule opt-IN (blank cron = off)? True only for kinds that write to Plex unattended
    # — running those on a timer is a choice to make, not a default to inherit.
    schedule_optional: bool = False
    trigger: str = ""  # what causes it, for the kinds no button can start


# Every registered kind, in the order the Jobs page shows them. `manual` is a deliberate allow-list,
# NOT `_HANDLERS.keys()`: `user.cleanup` takes a slug and DELETES that person's rows, so it must never
# be runnable from a generic button. The four scheduled/maintenance kinds above it are all
# converge-to-desired-state passes that are safe to press at any time.
CATALOG: tuple[JobKind, ...] = (
    JobKind(
        kind="sync.users",
        label="Sync people from Plex",
        description=(
            "Re-read who has access to your server from plex.tv and Tautulli. This is what notices a "
            "new person (and writes the share filters that stop them seeing everyone else's rows) and "
            "what notices someone leaving."
        ),
        manual=True,
        # A WRITER, despite the name. The handler calls `_rename_after_nickname` inline, which renames
        # Shortlist collections on the PMS when someone's display name has drifted. Marking it
        # read-only let that rename land in the middle of a run — and the run matches collections by
        # rendered TITLE, so a rename mid-converge can make a live collection look orphaned (converge
        # deletes orphans) or make delivery build a second collection beside the renamed one.
        writes_plex=True,
        schedule_job_id="user-sync",
        schedule_setting="sync.users_cron",
    ),
    JobKind(
        kind="sync.history",
        label="Sync watch history",
        description=(
            "Re-read every enabled person's watched set. Drives every recommendation, and marks "
            "delivered picks as watched so the dashboard's hit rate stays current. Reads only — "
            "nothing on Plex changes."
        ),
        manual=True,
        writes_plex=False,  # "Reads only — nothing on Plex changes", as the description says
        schedule_job_id="watch-sync",
        schedule_setting="sync.watch_cron",
    ),
    JobKind(
        kind="sync.check",
        label="Sync check",
        description=(
            "Checks every row on Plex against what Shortlist intends, and fixes anything that "
            "drifted. Rows fall out of step when a run doesn't finish, when the container restarts "
            "mid-write, or when someone was paused or disabled while their row was already live — a "
            "run only updates the people in that run, so everyone else keeps whatever they last had."
            "\n\nRuns nightly at 05:45 by default — after the rows build and after the privacy pass, "
            "so it checks the state those actually left behind. Unlike Privacy sync, which only ever "
            "makes the server more private, this one WRITES corrections to Plex and changes what "
            "people see, so it is the one schedule you can switch off completely: clear the box and "
            "it stays off rather than falling back to the default."
        ),
        manual=True,
        schedule_job_id="sync-check",
        schedule_setting="sync.check_cron",
        schedule_optional=True,
    ),
    JobKind(
        kind="privacy.sync",
        label="Privacy sync",
        description=(
            "Merge every account's share filter so nobody sees anyone else's row. Builds, delivers "
            "and promotes nothing — it can only ever make the server more private."
        ),
        manual=True,
        schedule_job_id="privacy-sync",
        schedule_setting="privacy.sync_cron",
    ),
    JobKind(
        kind="backup.take",
        label="Back up the database",
        description="Copy the database to /config/backups and prune to the keep limit.",
        manual=True,
        writes_plex=False,  # local filesystem only
        schedule_job_id="db-backup",
        schedule_setting="backup.cron",
    ),
    JobKind(
        kind="user.cleanup",
        label="Remove a disabled person's rows",
        description="Deletes the collections belonging to someone you disabled.",
        manual=False,
        trigger="Queued when you disable someone.",
    ),
    JobKind(
        kind="user.hide",
        label="Hide a paused person's rows",
        description="Takes a paused person's rows off every surface, keeping the collections so unpausing is instant.",
        manual=False,
        trigger="Queued when you pause someone.",
    ),
    JobKind(
        kind="user.restore",
        label="Put an un-paused person's rows back",
        description="Re-promotes the rows that pausing hid.",
        manual=False,
        trigger="Queued when you un-pause someone.",
    ),
    JobKind(
        kind="row.reconcile",
        label="Tidy up after a row change",
        description="Brings Plex back in line after a row is renamed, retargeted, or has its audience narrowed.",
        manual=False,
        trigger="Queued when you change a row.",
    ),
)

BY_KIND: dict[str, JobKind] = {k.kind: k for k in CATALOG}

# Kinds the UI may trigger by hand.
KINDS = tuple(k.kind for k in CATALOG if k.manual)


def handler(kind: str) -> Callable[[Handler], Handler]:
    """Register the function that performs `kind`. It must be idempotent (see module docstring)."""

    def register(fn: Handler) -> Handler:
        _HANDLERS[kind] = fn
        return fn

    return register


def enqueue(sessions, kind: str, payload: dict | None = None, *, max_attempts: int = 3) -> int:
    """Queue a job and return its id. Cheap and synchronous — safe to call from a request handler."""
    if kind not in _HANDLERS:
        raise ValueError(f"unknown job kind {kind!r}; known: {sorted(_HANDLERS)}")
    with sessions() as session:
        job = Job(kind=kind, payload=payload or {}, max_attempts=max_attempts)
        session.add(job)
        session.commit()
        logger.debug("queued job {} ({})", job.id, kind)
        return job.id


async def drain_now(state, reason: str) -> None:
    """Run whatever is queued, right now, without letting a failure reach the request.

    The queue is the SAFETY NET, not a delay: an operator who just changed something should see it
    take effect, and only when that is impossible (Plex down, a run mid-write) does the work fall back
    to the worker's retry-with-backoff. Nothing is lost either way — the jobs stay queued.
    """
    try:
        await run_pending(state)
    except Exception as e:
        # Named loudly: until these succeed the excludes are stale, which is the leak direction.
        logger.warning(
            "{}: queued work could not run now ({}: {}) — retrying", reason, type(e).__name__, redact(str(e))
        )


async def queue_privacy_sync(state, reason: str) -> None:
    """Queue a share-filter pass because `reason` left a filter write owed, and drain it now.

    The one entry point for "something changed and somebody's `label!=` excludes are now wrong".
    `privacy.sync` is `engine_run(ctx, [])`: it merges every account's filter and creates, promotes
    and delivers nothing, so it can only ever make the server MORE private (plex-safety rule 1) —
    which is what makes it safe to fire straight from a mutation handler.
    """
    enqueue(state.sessions, "privacy.sync", {"reason": reason})
    await drain_now(state, reason)


def recover_stale(sessions, *, boot: bool = False) -> int:
    """Requeue jobs whose worker died — at boot, and periodically for a mid-flight process kill.

    This is the whole point of the table. Handlers are idempotent, so replaying is safe and losing
    the work is not.

    ``boot=True`` skips the age test. At startup NOTHING is running in this process, so every row
    still marked `running` is definitionally abandoned — making a container restart wait out
    ``STALE_AFTER`` before retrying a cleanup would be 30 minutes of doing nothing for no reason.
    The periodic sweep keeps the age test, because there a `running` row usually IS this process.
    """
    now = datetime.now(UTC)
    with sessions() as session:
        stale = session.query(Job).filter(Job.status == "running").all()
        requeued = 0
        for job in stale:
            started = job.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if not boot and started is not None and now - started < STALE_AFTER:
                continue  # genuinely still running in this process
            job.status = "queued"
            job.started_at = None
            requeued += 1
        if requeued:
            session.commit()
            logger.warning("requeued {} job(s) abandoned by a previous process", requeued)
        return requeued


def writes_plex(kind: str) -> bool:
    """Does this kind write to Plex or plex.tv? Unknown kinds are treated as writers — the safe
    default is "take the lock", never "assume it is harmless"."""
    entry = BY_KIND.get(kind)
    return True if entry is None else entry.writes_plex


def _claimable(kind: str, *, allow_writers: bool, allow_history: bool) -> bool:
    """May a job of this kind START right now?

    Filtered at CLAIM time rather than after, so a job we cannot run is never marked `running` and
    then handed back — which would burn an attempt and its backoff for doing nothing.
    """
    if writes_plex(kind):
        return allow_writers
    # `sync.history` is read-only but is deferred during a run anyway: the run refreshes history
    # itself and both saturate the same PMS endpoints, so running them together is pure waste.
    return allow_history or kind != "sync.history"


def _claim(sessions, *, allow_writers: bool = True, allow_history: bool = True) -> tuple[int, str] | None:
    """Take the oldest runnable queued job whose backoff has elapsed, atomically. Returns (id, kind).

    SQLite has no ``SELECT ... FOR UPDATE SKIP LOCKED``, so the select-then-update is wrapped in
    ``BEGIN IMMEDIATE``, which takes the write lock up front and makes the pair atomic against any
    other claimer — including the concurrent read-only handlers this now dispatches.
    """
    now = datetime.now(UTC)
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        try:
            for job in session.query(Job).filter(Job.status == "queued").order_by(Job.created_at, Job.id).all():
                if not _claimable(job.kind, allow_writers=allow_writers, allow_history=allow_history):
                    continue
                if job.attempts:
                    ready_at = job.finished_at or job.created_at
                    if ready_at is not None and ready_at.tzinfo is None:
                        ready_at = ready_at.replace(tzinfo=UTC)
                    wait = _BACKOFF_S[min(job.attempts - 1, len(_BACKOFF_S) - 1)]
                    if ready_at is not None and now - ready_at < timedelta(seconds=wait):
                        continue  # still backing off after a failure
                job.status = "running"
                # `started_at` is stamped when the job actually STARTS (see `_execute`), not here.
                # A writer can be claimed and then stand down without running, and dating it from the
                # claim made those age toward STALE_AFTER — so the 30-minute sweep requeued jobs that
                # had never begun, with a spurious "abandoned by a previous process" warning.
                job.attempts += 1
                kind = job.kind
                session.commit()
                return job.id, kind
            session.rollback()
            return None
        except Exception:
            session.rollback()
            raise


def _release(sessions, job_id: int) -> None:
    """Put a claimed job back on the queue without counting the attempt.

    Used when a run starts between claiming a writer and running it: the job did not fail, it simply
    lost the race for the writer lock, and burning one of its three attempts for that would retire a
    perfectly good job after three unlucky nights.
    """
    with sessions() as session:
        job = session.get(Job, job_id)
        if job is not None and job.status == "running":
            job.status = "queued"
            job.started_at = None
            job.attempts = max(0, job.attempts - 1)
            session.commit()


def _finish(sessions, job_id: int, *, result: dict | None = None, error: str | None = None) -> None:
    """Close a job out. A failure that has attempts left goes back to `queued`; one that does not
    becomes `failed` and raises an Event, so the notification bell surfaces it (rule 10)."""
    with sessions() as session:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.finished_at = datetime.now(UTC)
        if error is None:
            job.status = "done"
            job.result = result or {}
            job.detail = str((result or {}).get("detail", ""))[:512]
        elif job.attempts < job.max_attempts:
            job.status = "queued"  # retried after backoff
            job.error = error
            logger.warning(
                "job {} ({}) failed, attempt {}/{}: {}", job.id, job.kind, job.attempts, job.max_attempts, error
            )
        else:
            job.status = "failed"
            job.error = error
            logger.error("job {} ({}) gave up after {} attempts: {}", job.id, job.kind, job.attempts, error)
            session.add(
                Event(
                    scope="job.failed",
                    level="error",
                    message={"job_id": job.id, "kind": job.kind, "error": error, "attempts": job.attempts},
                )
            )
        session.commit()


def _plex_busy(state) -> bool:
    """Is an engine run writing to Plex right now?

    Runs and jobs are separate systems but share one Plex and one throttled plex.tv. Interleaving
    their writes risks a job merging a share filter from a roster snapshot a run is mid-way through
    changing. Jobs simply wait — they are maintenance, never latency-critical.
    """
    service = getattr(state, "run_service", None)
    return bool(service is not None and getattr(service, "is_running", lambda: False)())


_DRAIN_LOCK = asyncio.Lock()

#: Per-event-loop locks. An `asyncio.Lock` binds itself to the loop of the first `await` that
#: CONTENDS on it and raises from any other loop thereafter — so a single module-level lock is only
#: safe while the process has exactly one loop for its lifetime. The server does; a test suite that
#: calls `asyncio.run()` per test does not, and the failure mode is silent (the second writer raises,
#: the drain swallows it, and the job simply never runs). Keyed by loop, pruned as loops close.
_WRITER_LOCKS: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}


def plex_writer_lock() -> asyncio.Lock:
    """The one-writer lock for Plex/plex.tv, held by writer JOBS **and by engine runs**.

    Both sides must take it. A job checking `_plex_busy` is not enough on its own: that only makes
    jobs defer to runs, so a writer already mid-flight kept merging share filters straight through
    the start of a run. Share-filter writes are read-modify-write merges (plex-safety rule 3), so the
    second of two overlapping merges writes back a snapshot taken before the first's
    `label!=shortlist_<slug>` exclude existed — and since the Privacy Check was removed on
    2026-07-16, nothing catches it afterwards. `user.restore` is the worst case: it merges and then
    PROMOTES in straight-line code, so a clobbered merge puts a row on shared Home with the excludes
    gone.

    Public because `RunService` acquires it around the engine run — that is what makes the exclusion
    symmetric rather than one-directional.
    """
    loop = asyncio.get_running_loop()
    for stale in [key for key in _WRITER_LOCKS if key.is_closed()]:
        del _WRITER_LOCKS[stale]
    lock = _WRITER_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _WRITER_LOCKS[loop] = lock
    return lock


#: How many read-only jobs may run at once. Dial to 1 if a PMS complains about the concurrency.
DEFAULT_MAX_PARALLEL_READONLY = 3

#: How long a writer will wait for the writer lock before standing down and requeueing.
#:
#: Long enough for another writer JOB to finish (a `user.cleanup` is seconds), short enough that a
#: writer which loses a late race to a RUN costs the queue a minute rather than the run's full
#: duration. A run is normally excluded by `_plex_busy` before we ever wait; this bounds the race.
WRITER_LOCK_WAIT_S = 60


def _max_parallel_readonly(state) -> int:
    try:
        with state.sessions() as session:
            value = SettingsStore(session).get("jobs.max_parallel_readonly")
    except Exception:
        return DEFAULT_MAX_PARALLEL_READONLY
    if isinstance(value, int) and value >= 1:
        return value
    return DEFAULT_MAX_PARALLEL_READONLY


async def run_pending(state) -> int:
    """Drain the queue. Returns how many jobs were dispatched.

    Three callers reach this: the scheduler tick, `POST /api/system/jobs`, and the disable path.
    The drain LOOP is serialized (claiming is fast, and two loops racing would just interleave for
    no gain); what runs inside it is not. Read-only kinds go out concurrently up to
    `jobs.max_parallel_readonly`; anything that writes to Plex takes `_PLEX_WRITER_LOCK`.

    A caller that finds the loop already running returns immediately rather than queueing behind it
    in a request path.

    Note this no longer bails on `_plex_busy`: a run blocks WRITERS, not the whole queue. Backing up
    the database or re-reading the roster while a run is on is harmless, and refusing to was why a
    server that ran for an hour did no maintenance at all in that time.
    """
    sessions = state.sessions
    if _DRAIN_LOCK.locked():
        return 0
    async with _DRAIN_LOCK:
        return await _drain(state, sessions)


async def _execute(state, sessions, job_id: int, kind: str) -> None:
    """Run one claimed job and close it out. Never raises — a handler's failure is the job's, not
    the drain's, and one bad job must not stop everything queued behind it."""
    with sessions() as session:
        job = session.get(Job, job_id)
        payload = dict(job.payload or {}) if job is not None else {}
        if job is not None:
            job.started_at = datetime.now(UTC)  # it is running NOW, not when it was claimed
            session.commit()
    fn = _HANDLERS.get(kind)
    if fn is None:
        # The kind was removed in an upgrade while a job was queued. Nothing can run it.
        _finish(sessions, job_id, error=f"no handler registered for {kind!r}")
        return
    try:
        if inspect.iscoroutinefunction(fn):
            # An async handler is awaited on THIS loop rather than pushed to a worker thread.
            # `sync.users` and `sync.history` were written as endpoint/scheduler coroutines: they
            # publish to the SSE bus and hand their own blocking work to an executor. Running them
            # under a fresh `asyncio.run()` in a worker thread would put their SSE publishes on a
            # loop nothing is listening to. They already yield, so they do not stall the drain.
            result = await fn(state, payload)
        else:
            result = await asyncio.get_running_loop().run_in_executor(None, functools.partial(fn, state, payload))
        _finish(sessions, job_id, result=result or {})
    except Exception as e:
        # redact: a Plex/plex.tv error can carry a tokened URL (rule 9).
        _finish(sessions, job_id, error=redact(f"{type(e).__name__}: {e}"))


async def _run_writer(state, sessions, job_id: int, kind: str) -> None:
    """A Plex-writing job: exclusive, and never alongside a run.

    Waiting behind ANOTHER JOB is fine and necessary — "disable everyone" queues one `user.cleanup`
    per person and drains them inline, and they have to run serially, not one-per-drain. Waiting
    behind a RUN is not: a run holds the lock for its whole duration, and `_drain` awaits every task
    it dispatched inside a `finally` while holding `_DRAIN_LOCK`, so a parked writer would freeze the
    entire queue — readers included — for the length of the run. That is the exact behaviour this
    change set out to remove.

    So the wait is bounded two ways: skip entirely if a run is already in flight, and cap the wait for
    the lock so losing a late race costs a minute rather than an hour. Standing down is not a failure,
    so the job goes back on the queue with its attempt RETURNED — three unlucky nights would
    otherwise retire a perfectly healthy job.
    """
    if _plex_busy(state):
        _release(sessions, job_id)
        return
    lock = plex_writer_lock()
    try:
        await asyncio.wait_for(lock.acquire(), timeout=WRITER_LOCK_WAIT_S)
    except TimeoutError:
        logger.info("job {} ({}) stood down — the Plex writer lock stayed held", job_id, kind)
        _release(sessions, job_id)
        return
    try:
        # Re-checked with the lock in hand: a run can start between the check above and here.
        if _plex_busy(state):
            _release(sessions, job_id)
            return
        await _execute(state, sessions, job_id, kind)
    finally:
        lock.release()


async def _run_reader(sem: asyncio.Semaphore, state, sessions, job_id: int, kind: str) -> None:
    async with sem:
        await _execute(state, sessions, job_id, kind)


async def _drain(state, sessions) -> int:
    ran = 0
    dispatched: set[asyncio.Task] = set()
    # A separate list, because the done-callback below removes finished tasks from `dispatched` —
    # gathering that set would only ever inspect the SLOW ones, and quietly skip the results of
    # everything that finished first.
    started: list[asyncio.Task] = []
    sem = asyncio.Semaphore(_max_parallel_readonly(state))
    try:
        while True:
            busy = _plex_busy(state)
            claimed = _claim(sessions, allow_writers=not busy, allow_history=not busy)
            if claimed is None:
                break
            job_id, kind = claimed
            runner = (
                _run_writer(state, sessions, job_id, kind)
                if writes_plex(kind)
                else _run_reader(sem, state, sessions, job_id, kind)
            )
            task = asyncio.create_task(runner)
            dispatched.add(task)
            started.append(task)
            task.add_done_callback(dispatched.discard)
            ran += 1
    finally:
        # Await everything dispatched before returning: callers (and tests) treat a completed
        # `run_pending` as "the queue was drained", and a detached task outliving it would make
        # "did that job run?" unanswerable.
        if started:
            # `return_exceptions` so one bad task never strands the others — but the results are
            # LOOKED AT. Swallowing them silently is how a job that never ran looked like a job that
            # ran and did nothing.
            for outcome in await asyncio.gather(*started, return_exceptions=True):
                if isinstance(outcome, BaseException):
                    logger.exception("job dispatch failed", exc_info=outcome)
    return ran


# --- handlers ---------------------------------------------------------------------------------
#
# Each one MUST be idempotent: a job interrupted mid-flight is requeued and replayed on the next
# boot with no way to know how far it got. Every handler here is a converge-to-desired-state
# operation, never a delta, which is what makes replay safe.


@handler("sync.check")
def _sync_check(state, payload: dict) -> dict:
    """Take every Shortlist row the last run could not reach off the OWNER's Home.

    Promotion only ever writes flags for people IN a run, so a row belonging to anyone paused,
    disabled, deselected or caught by a run that died keeps its flags indefinitely. Clearing
    own-home is monotonically private, so this needs no privacy gate.
    """
    from shortlist.engine.models import RunReport
    from shortlist.engine.pipeline import _converge_phase

    report = RunReport(started_at=datetime.now(UTC))
    dry_run = bool(payload.get("dry_run"))
    ctx = state.run_service.build_context(dry_run=dry_run)
    # Deleting is only ever a DELIBERATE act. This job now has a nightly schedule, so an unattended
    # pass must not be able to destroy a collection on its own — it demotes, and reports what it
    # WOULD remove. The owner sees that in the preview and presses Fix, which arrives here with
    # `confirmed`. Without this split, upgrading turned on a job that silently deleted from Plex.
    confirmed = bool(payload.get("confirmed"))
    _converge_phase(ctx, set(), report, may_delete=confirmed and not dry_run)
    # Deletions are reported SEPARATELY and named first. Folding them into `fixed` would hide the one
    # irreversible thing this does behind a number, in the very preview an operator reads to decide
    # whether to run it for real.
    removed = report.orphans_removed
    detail = f"Checked every row; corrected {len(report.converged)}"
    if removed:
        detail += (
            f"; removed {len(removed)} orphaned collection(s)"
            if confirmed and not dry_run
            else f"; {len(removed)} orphaned collection(s) to remove"
        )
    return {"fixed": report.converged, "orphans": removed, "detail": detail}


@handler("privacy.sync")
def _privacy_sync(state, payload: dict) -> dict:
    """Merge every account's share filter without building anything.

    `engine_run(ctx, [])` with no users sweeps unhidable rows and writes every share filter, but
    delivers, creates, promotes and DELETES nothing (plex-safety rule 1) — so it can only ever make
    the server more private. The delete half is enforced, not merely intended: `run()` passes
    `may_delete=bool(users)` to converge, so a run with nobody in it has no authority to destroy
    anyone's collection. It used to inherit that authority from the context and quietly had it.

    That is what makes it safe to fire from a mutation (a user disabled, a
    shared row's audience narrowed) rather than waiting for the nightly run.
    """
    from shortlist.engine.pipeline import run as engine_run

    ctx = state.run_service.build_context(dry_run=False)
    report = engine_run(ctx, [])
    swept = sum(len(titles) for titles in report.swept_rows.values())
    # The reason is carried through to the detail line so the Jobs page answers "why did this fire?"
    # — "someone was removed from a shared row" reads very differently from a nightly housekeeping
    # pass, and an operator seeing filters rewritten deserves to know which.
    reason = str(payload.get("reason") or "")
    detail = "Share filters merged for every account"
    if reason:
        detail += f" after {reason}"
    if swept:
        detail += f"; swept {swept} unhidable row(s)"
    return {"swept": swept, "converged": report.converged, "reason": reason, "detail": detail}


@handler("sync.users")
async def _sync_users(state, payload: dict) -> dict:
    """Pull the roster from plex.tv + Tautulli — the scheduled reconcile, as a durable job.

    It used to be a bare APScheduler coroutine whose only failure path was `logger.exception`. That
    made the ONE thing this does that touches Plex invisible when it broke: it is what notices a new
    account (and writes the filters that stop them seeing everyone's rows) and what notices someone
    leaving the share. A plex.tv wobble at 04:00 silently meant no roster reconcile for 24 hours.

    Idempotent — it upserts from whatever plex.tv currently says.
    """
    from fastapi import HTTPException

    from shortlist.server.api.users import sync_users_from_state

    try:
        result = await sync_users_from_state(state)
    except HTTPException as e:
        # An instance whose wizard is unfinished, or whose token was revoked, has nothing to sync.
        # That is a STATE, not a failure: retrying it three times and then ringing the notification
        # bell — nightly, for ever — is how an owner learns to ignore the bell. `sync_watched` already
        # treats the same condition this way (`run_service.py:199`); these two must not disagree.
        if e.status_code == 409:
            logger.info("user-sync skipped: {}", e.detail)
            return {"skipped": True, "detail": f"Skipped — {e.detail}"}
        raise
    return {
        **result,
        "detail": f"Synced {result['total']} account(s) — {result['added']} new, {result['updated']} updated",
    }


@handler("sync.history")
async def _sync_history(state, payload: dict) -> dict:
    """Re-read every enabled user's watched set. Durable for the same reason `sync.users` is.

    Watch history drives every recommendation, so a silent failure here degrades picks server-wide
    while everything still looks healthy. Reads only — nothing on Plex changes.

    Bails when a run is in flight rather than waiting. `sync_watched` takes `RunService._lock`, which
    a run holds for its whole duration — and `_drain` awaits every task it dispatched before
    returning, holding `_DRAIN_LOCK`. Parking here would therefore freeze the entire queue, readers
    included, for the length of the run. The run refreshes history itself anyway, so there is nothing
    to lose: the scheduler's next tick picks this up.
    """
    if _plex_busy(state):
        return {"detail": "Skipped — a run is already reading watch history"}
    await state.run_service.sync_watched()
    return {"detail": "Watch history re-read for every enabled user"}


@handler("backup.take")
def _backup_take(state, payload: dict) -> dict:
    """Copy the database to /config/backups and prune to the keep limit.

    The one job whose failure nobody would notice until they needed the backup — which is the worst
    possible moment to discover a month of `logger.exception` nobody read.
    """
    from shortlist.server.services.backup import take_backup

    kwargs = {"max_keep": int(payload["max_keep"])} if payload.get("max_keep") else {}
    path = take_backup(state.config_dir, label=payload.get("label") or "scheduled", **kwargs)
    if path is None:
        return {"path": "", "detail": "No database to back up yet"}
    return {"path": str(path), "detail": f"Backed up to {path.name}"}


@handler("user.cleanup")
def _user_cleanup(state, payload: dict) -> dict:
    """Remove a disabled user's collections. Retried, unlike the old fire-and-forget call — which is
    the bug: if Plex was down at the moment of disabling, nothing ever revisited them, because no
    run touches a disabled user."""
    from shortlist.engine.delivery import remove_row_collections
    from shortlist.server.safe_mode import force_dry_run
    from shortlist.server.services.collection_reconcile import forget_user_deliveries

    slug = payload["slug"]
    dry_run = force_dry_run()
    ctx = state.run_service.build_context(dry_run=dry_run)
    removed = remove_row_collections(
        ctx.plex, ctx.config, label=f"{ctx.config.label_prefix}_{slug}", displays=None, dry_run=dry_run
    )
    if not dry_run:
        # Their whole label just came off the server, so every ledger row naming it is now a pointer to
        # nothing. Only after a real removal — a dry run changed nothing and must leave the ledger able
        # to address the collections it previewed.
        with state.sessions() as session:
            forget_user_deliveries(session, slug)
            session.commit()
    # Deleting someone's collections is a destructive Plex write, so it emits its own structured
    # audit row (plex-safety rule 10) — "what changed on whose server at 03:31" must stay answerable
    # from the events feed, not only from the jobs list. `dry_run` is recorded because
    # remove_row_collections fills `removed` either way: without it a preview is indistinguishable
    # in the audit from a real deletion.
    with state.sessions() as session:
        session.add(
            Event(
                scope="user.disable.cleanup",
                level="warn",
                message={
                    "user": slug,
                    "removed": removed,
                    "dry_run": dry_run,
                    "error": None,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()
    return {
        "removed": removed,
        "dry_run": dry_run,
        "detail": f"{'Would remove' if dry_run else 'Removed'} {len(removed)} row(s) for {slug}",
    }


@handler("user.hide")
def _user_hide(state, payload: dict) -> dict:
    """Take a paused user's rows off every surface, keeping the collections.

    Pause is not delete: the collection and its label stay, so every other account's `label!=`
    exclude still matches it and unpausing is a re-promote rather than a full LLM rebuild. Only ever
    removes visibility, so it is safe to replay after a crash.
    """
    slug = payload["slug"]
    # `build_context` ORs in SHORTLIST_DRY_RUN, so `ctx.config.dry_run` can be True despite the False
    # here — and `demote_all` has no dry-run branch of its own (rule 8).
    ctx = state.run_service.build_context(dry_run=False)
    hidden: list[str] = []
    label = f"{ctx.config.label_prefix}_{slug}"
    for section in ctx.plex.sections():
        for collection in ctx.plex.find_owned_collections(section, label):
            if ctx.config.dry_run:
                if ctx.plex.claims_any_surface(collection):
                    logger.info("[dry-run] {}: would take off every surface", collection.title)
                    hidden.append(collection.title)
                continue
            if ctx.plex.demote_all(collection):
                hidden.append(collection.title)
    with state.sessions() as session:
        session.add(
            Event(
                scope="user.pause.hide",
                level="info",
                message={
                    "user": slug,
                    "hidden": len(hidden),
                    "dry_run": ctx.config.dry_run,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()
    verb = "Would hide" if ctx.config.dry_run else "Hid"
    return {"hidden": hidden, "dry_run": ctx.config.dry_run, "detail": f"{verb} {len(hidden)} row(s) for {slug}"}


@handler("user.restore")
def _user_restore(state, payload: dict) -> dict:
    """Put an un-paused user's rows back on the surfaces their rows ask for.

    The exact mirror of `user.hide`: pause demoted the collections but kept them and their labels, so
    this is a re-promote, not a rebuild. Leaving it to "the next run" — which is what used to happen —
    was wrong twice over: a row whose schedule is blank has no next run at all, and neither does one
    while `paused_all` is set, so an un-paused person could stay invisible indefinitely.

    This is the ONE job that makes something more visible, so plex-safety rule 1 applies — and the
    merge happens HERE, in straight-line code, rather than in a `privacy.sync` queued just before it.
    Two queued jobs would not have been ordered: `_claim` skips a job whose retry backoff has not
    elapsed and takes the next one, so a `privacy.sync` that failed against a 503 plex.tv would be
    stepped over and this handler would promote onto a healthy PMS with the excludes unmerged. That is
    exactly the leak the ordering exists to prevent, and nothing downstream would have caught it.

    `engine_run(ctx, [])` merges every account's filter and delivers, creates and promotes nothing, so
    if it raises, this raises with it and the whole job is retried — no promotion happens.
    """
    from shortlist.engine.models import UserProfile, UserType
    from shortlist.engine.pipeline import promote_user_rows
    from shortlist.engine.pipeline import run as engine_run
    from shortlist.server.db.models import Delivery, Run, RunUser, User

    slug = payload["slug"]
    ctx = state.run_service.build_context(dry_run=False)
    with state.sessions() as session:
        user = session.query(User).filter_by(slug=slug).first()
        if user is None or not user.enabled or (user.prefs or {}).get("paused"):
            # Re-queued after a crash but the world moved on (deleted, disabled, paused again). Doing
            # nothing is right: this handler only ever makes rows visible.
            return {"restored": [], "detail": f"{slug} is no longer an un-paused user — nothing restored"}
        profile = UserProfile(
            username=user.username,
            plex_account_id=user.plex_account_id,
            user_type=UserType(user.user_type),
            slug=user.slug,
            nickname=user.nickname or user.friendly_name,
            # Their own row-name override, resolved the way `resolve_row_template` does. Without it the
            # GLOBAL template renders here and the per-library titles built below match nothing, so
            # every one of this person's collections falls to `_promote_one`'s no-spec branch and lands
            # on a surface their row's placement may have switched off.
            row_name_template=(user.prefs or {}).get("row_name_tpl"),
        )
        # {delivered title -> row slug} rebuilt from the last run's breakdown, which is the same map
        # `_promote_phase` passes. It is the only way to place a `{top_seed}` row: its title is
        # different every run, so it cannot be re-rendered from the template here.
        marker = row_marker(user.plex_account_id)
        latest = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
        run_user = (
            session.query(RunUser).filter_by(run_id=latest.id, user_id=user.id).first() if latest is not None else None
        )
        placements = {
            entry["row_title"] + marker: entry["row_slug"]
            for entry in ((run_user.breakdown if run_user else None) or [])
            if entry.get("row_slug") and entry.get("row_title")
        }
        # The AUTHORITATIVE map: {ratingKey -> row slug} straight from the delivery ledger. Titles
        # above are a fallback for rows delivered before the ledger existed; a `{top_seed}` row has no
        # renderable title at all, so without this it lands on `_promote_one`'s no-spec branch and gets
        # its audience's Home rather than the placement the row actually asks for.
        ledger = [d for d in session.query(Delivery).filter_by(user_slug=slug) if d.rating_key]
        # An AMBIGUOUS ratingKey — two rows claiming the same collection — falls through to the title
        # map rather than letting whichever row the query returned last win arbitrarily. Reachable if a
        # run crashed between the delete and the persist on delivery's rebuild path.
        claims: dict[int, int] = {}
        for d in ledger:
            claims[d.rating_key] = claims.get(d.rating_key, 0) + 1
        keys = {d.rating_key: d.collection_slug for d in ledger if claims[d.rating_key] == 1}
        if len(keys) != len(ledger):
            logger.warning(
                "{}: {} ledger key(s) claimed by more than one row — placing those by title",
                slug,
                len(ledger) - len(keys),
            )

    # Rule 1's ordering: every account's excludes are merged BEFORE anything is promoted.
    engine_run(ctx, [])
    restored = promote_user_rows(ctx, profile, placements, placement_keys=keys)
    with state.sessions() as session:
        session.add(
            Event(
                scope="user.unpause.restore",
                level="info",
                message={"user": slug, "restored": len(restored), "at": datetime.now(UTC).isoformat()},
            )
        )
        session.commit()
    return {"restored": sorted(restored), "detail": f"Put {len(restored)} row(s) back for {slug}"}


@handler("row.reconcile")
def _row_reconcile(state, payload: dict) -> dict:
    """Remove a row's collections from Plex after it was deleted, disabled, or lost an audience.

    Durable for the same reason `user.cleanup` is: the previous fire-and-forget call had no retry, so
    a Plex outage at the moment of the edit left the collections on the server with nothing to ever
    revisit them. `_reconcile_row_removal` is removal-only and re-reads the server each time, so
    replaying it after a crash is safe.
    """
    from shortlist.server.services.collection_reconcile import _reconcile_row_removal

    slug = payload["slug"]
    build = payload.get("build", "per_person")
    only_user_ids = set(payload["only_user_ids"]) if payload.get("only_user_ids") is not None else None
    removed: list[str] = []
    _reconcile_row_removal(
        state,
        slug=slug,
        build=build,
        dry_run=False,
        removed=removed,
        only_user_ids=only_user_ids,
        # Carried in the payload by the DELETE path only: the row is gone by the time a retry runs, so
        # the template it was titled with cannot be looked up any more. Everywhere else it reads the DB.
        template=payload.get("template"),
        # Carried by a NARROWED row: only the libraries it walked away from, so the ones it still
        # targets survive. Absent means every library, which is right for an outright removal.
        in_sections=set(payload["in_sections"]) if payload.get("in_sections") is not None else None,
    )
    with state.sessions() as session:
        session.add(
            Event(
                scope=payload.get("scope", "row.reconcile"),
                level="warn",
                message={
                    "slug": slug,
                    "removed": removed,
                    "dry_run": False,
                    "error": None,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()
    return {"removed": removed, "detail": f"Removed {len(removed)} collection(s) for row {slug}"}
