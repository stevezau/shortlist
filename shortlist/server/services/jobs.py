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
from shortlist.engine.models import LABEL_PREFIX
from shortlist.server.db.models import Job
from shortlist.server.services.audit import add_audit, write_audit
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
            "Asks plex.tv who currently has access to your server, and brings Shortlist's list of "
            "people into line with it. Someone new is added, and the filter that hides other people's "
            "rows from them is written immediately, so they never get a look at anyone else's row. "
            "Someone who no longer has access is switched off and their rows are taken off Plex. "
            "Display names are refreshed from Tautulli if you have it connected, and a row still "
            "sitting on Plex under somebody's old name is renamed to match."
            "\n\nRuns at 04:47 by default. If it ever looks like half the people you share with "
            "vanished at once — almost always a bad reply from plex.tv rather than a real change — "
            "nobody is switched off and the refusal is recorded for you to look at instead."
        ),
        manual=True,
        # A WRITER, despite the name. The handler calls `rename_after_nickname` inline, which renames
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
            "Re-reads what each person has watched, straight from your Plex server. This is what "
            "every recommendation is built from, and it is also how Shortlist notices that a title it "
            "put in front of someone was actually watched — the numbers on your dashboard come from "
            "it. Reads only: nothing on Plex changes and nobody's rows move."
            "\n\nRuns at 04:17 by default, and reads every watched title each time rather than only "
            "what changed. Measured on a 47-user server, reading everything took the same time as "
            "reading the recent slice — Plex sends a whole page either way — and reading only the "
            "recent slice missed a series you marked as watched by hand, because Plex leaves those "
            "without a date. A library the read cannot see in full is topped up rather than replaced, "
            "and un-watching is handled separately, on a slower schedule — so one thin answer from Plex "
            "cannot be mistaken for a pile of things being un-watched."
        ),
        manual=True,
        writes_plex=False,  # "Reads only — nothing on Plex changes", as the description says
        schedule_job_id="watch-sync",
        schedule_setting="sync.watch_cron",
    ),
    JobKind(
        kind="sync.check",
        label="Check and fix rows on Plex",
        description=(
            "Looks at every collection Shortlist has put on your Plex server and puts back the ones "
            "that ended up in the wrong place — somebody else's row left sitting on YOUR Home screen, "
            "a paused person's row that never came down, or rows that drifted to the bottom of a "
            "library's Recommended shelf instead of sitting where you asked for them. Press Check now "
            "and it tells you what it would change without touching anything; the Fix button then "
            "makes those changes."
            "\n\nIt can also DELETE a collection, which is the one thing here you cannot undo. A "
            "collection labelled for somebody Shortlist no longer knows about can't be hidden from "
            "anyone, so the only way to clear it out of your Collections tab is to remove it. Only "
            "the Fix button ever does that: the nightly pass takes such a collection off the shelves, "
            "reports it, and leaves the deleting to you."
            "\n\nRows fall out of step when a run doesn't finish, when the container restarts "
            "mid-write, or when someone was paused or disabled while their row was already live — a "
            "run only updates the people in that run, so everyone else keeps whatever they last had."
            "\n\nRuns nightly at 05:45 by default — after the rows build and after the privacy pass, "
            "so it checks the state those actually left behind. Because it writes corrections to "
            "Plex unattended, it is the one schedule you can switch off completely: open this job and "
            "choose Off under Frequency. It then stays off rather than falling back to 05:45, and "
            "Check now still works whenever you press it."
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
            "Goes through every Plex account you share with and updates the filter that keeps other "
            "people's rows out of sight. That filter is the whole privacy system: each account is "
            "told to ignore the label Shortlist puts on everybody else's collections."
            "\n\nIt builds no rows, delivers nothing, puts nothing on anyone's Home screen and deletes "
            "nothing, so the worst it can do to who-sees-what is make your server more private — which "
            "is why it is safe to press at any time."
            "\n\nThe one other thing it does is cosmetic: it puts your rows back where you asked for "
            "them in each library's Recommended shelf, if something has shuffled them. That changes "
            "the order they appear in, never who can see them."
            "\n\nRuns at 05:15 by default, after the two syncs above, so it works from a list of "
            "people that has just been refreshed."
        ),
        manual=True,
        schedule_job_id="privacy-sync",
        schedule_setting="privacy.sync_cron",
        trigger=(
            "Also runs on its own whenever something changes who should see what: someone switched "
            "on or off, a row's audience changed, or a new account turning up on your server."
        ),
    ),
    JobKind(
        kind="backup.take",
        label="Back up the database",
        description=(
            "Takes a copy of Shortlist's own database — your rows, settings, people and history — and "
            "saves it under /config/backups, where you can restore it from below. The oldest copies "
            "are deleted once there are more than the number you keep (10 by default). Nothing on "
            "Plex is touched."
            "\n\nRuns at 03:00 by default, before anything else in the night."
        ),
        manual=True,
        writes_plex=False,  # local filesystem only
        schedule_job_id="db-backup",
        schedule_setting="backup.cron",
    ),
    JobKind(
        kind="watch.reconcile",
        label="Credit a finished playback",
        description=(
            "Works out what someone just watched from their row, the moment they stop playing. It "
            "reads nothing from Plex and changes nothing there — everything it needs was already "
            "recorded while the video was playing."
            "\n\nThis is what puts a partial watch on your dashboard straight away. Plex only "
            "records that a title was finished, so someone who starts a pick and gives up halfway "
            "leaves no trace in its history at all; the only moment that fact exists is while it is "
            "playing, and this is what saves it."
            "\n\nQueued automatically when a playback session ends. There is nothing to schedule "
            "and nothing to run by hand — the nightly watch-history sync covers the same ground and "
            "more."
        ),
        manual=False,
        writes_plex=False,  # local database only
        trigger=(
            "Runs when a playback session ends — nothing schedules it, and there is nothing to run by "
            "hand. If it never runs (Shortlist restarted mid-playback, say), nothing is lost: the "
            "nightly watch-history sync reaches the same conclusion from the same records."
        ),
    ),
    JobKind(
        kind="maintenance.prune",
        label="Clear out old records",
        description=(
            "Deletes Shortlist's own old records so the database doesn't grow for ever: run history "
            "older than the “Runs kept” limit in Settings → Advanced (three months by "
            "default), and saved lookups from TMDB and your libraries that have since expired. "
            "Nothing on Plex changes and nobody's rows move — this only touches Shortlist's own "
            "database."
            "\n\nWhat it never deletes: the figures on your dashboard, and the record of which "
            "collection belongs to whom, so clearing history can't strand a row on somebody's server. "
            "It also trims the log of what Shortlist has changed, to the “Change log kept” limit in "
            "the same place — which is set to Forever by default, so nothing there is deleted until "
            "you choose a limit."
            "\n\nRuns nightly at 06:15 by default, last of the night, once everything else has "
            "finished writing."
        ),
        manual=True,
        writes_plex=False,  # local database only
        # Queued by the run service the moment a run's results are safely persisted. It used to run
        # INSIDE that persist's transaction, so a bulk delete that failed took the whole persist with
        # it — discarding the record of a run that had already written to Plex.
        schedule_job_id="maintenance-prune",
        schedule_setting="maintenance.prune_cron",
        trigger=(
            "Also runs after every row build. The timer still matters: a server whose rows have no "
            "schedule — or one paused from the Danger Zone — has no builds to trigger it, and would "
            "never tidy up at all."
        ),
    ),
    JobKind(
        kind="user.cleanup",
        label="Remove a disabled person's rows",
        description=(
            "Deletes the Plex collections belonging to somebody who has been switched off, so their "
            "row stops sitting in your Collections tab. What Shortlist knows about them stays, so "
            "switching them back on builds their row again on the next run."
        ),
        manual=False,
        trigger=(
            "Queued when you switch somebody off, and when a people sync notices somebody no longer "
            "has access to your server."
        ),
    ),
    JobKind(
        kind="user.hide",
        label="Hide a paused person's rows",
        description=(
            "Takes a paused person's rows off every Plex shelf without deleting anything. The "
            "collection stays where it is, so un-pausing puts the same row straight back instead of "
            "waiting for it to be built again."
        ),
        manual=False,
        trigger="Queued when you pause somebody.",
    ),
    JobKind(
        kind="rows.visibility",
        label="Show and hide rows for today",
        description=(
            "Puts each row on the Plex shelves its own day schedule asks for. A row on its day off is "
            "hidden, not deleted — it keeps its titles, so it comes straight back on its next day "
            "without being built again. Everybody's privacy filters are re-merged before anything is "
            "shown, and if that fails nothing appears."
        ),
        manual=True,
        schedule_job_id="rows-visibility",
        schedule_setting="rows.visibility_cron",
        trigger="Runs at midnight, and whenever you change which days a row appears on.",
    ),
    JobKind(
        kind="user.restore",
        label="Put an un-paused person's rows back",
        description=(
            "Puts an un-paused person's rows back on the shelves that pausing took them off. "
            "Everybody else's privacy filters are re-merged first, and if that fails nothing is put "
            "back — a row must never reappear before the filters that hide it from other people do."
        ),
        manual=False,
        trigger="Queued when you un-pause somebody.",
    ),
    JobKind(
        kind="watching_account.transfer",
        label="Copy your watch history to your watching account",
        description=(
            "Makes your watching account's watch history match yours: the same films ticked off, the "
            "same episodes of each show, and anything you are part-way through sitting at the same "
            "point in Continue Watching. Anything watched on that account that you have not watched "
            "is un-ticked, which is what makes the two match. Your own account is never written to."
            "\n\nOn a heavy library this is several thousand writes to Plex, which is why it runs "
            "here rather than while you wait. The account's state is saved before the first write, so "
            "it can be put back exactly."
        ),
        manual=False,
        # A writer, and the only one here that can REMOVE watch history. It takes the exclusive Plex
        # lock like every other writer: a run converging collections while this rewrites thousands of
        # watch flags is exactly the overlap that lock exists for.
        writes_plex=True,
        trigger="Queued from the watching-account page when you copy your history across.",
    ),
    JobKind(
        kind="watching_account.undo",
        label="Undo a watch-history copy",
        description=(
            "Puts a watching account back exactly as it was before the copy — rewatch counts and "
            "part-watched positions included, not just watched or unwatched. Restores from the "
            "snapshot the copy took rather than replaying its writes backwards, because re-ticking "
            "what was un-ticked would leave a state that existed on neither account."
        ),
        manual=False,
        writes_plex=True,
        trigger="Queued when you press Undo after copying a watch history across.",
    ),
    JobKind(
        kind="row.reconcile",
        label="Remove a row's collections from Plex",
        description=(
            "Takes a row's collections off Plex once they should no longer be there — the row was "
            "deleted or switched off, somebody was dropped from who gets it, or it stopped covering a "
            "library it had already built in. It only ever removes; building and renaming are done "
            "elsewhere."
        ),
        manual=False,
        trigger=(
            "Queued when you delete a row, switch one off, change how it is built, narrow who gets "
            "it, or drop one of its libraries."
        ),
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


#: Strong refs to in-flight background drains. A bare `create_task` can be garbage-collected
#: mid-flight; the JOBS survive that (they are committed rows) but the attempt would not.
_BACKGROUND_DRAINS: set[asyncio.Task] = set()


def drain_in_background(state, reason: str) -> None:
    """Run the queue now, without making the caller wait for it.

    For a mutation whose Plex work is a per-user walk: the DB change is done, and that is what the
    response reports. `drain_now` AWAITS every job it dispatches, so calling it from a handler holds
    the request open for the full sync/cleanup — seconds to minutes — while the page sits on a
    spinner and the toast that says "started" cannot appear until the thing has already finished.

    Nothing is lost by not waiting: the jobs are committed rows, the worker retries them with
    backoff, and they are visible in the header's activity popover while they run.
    """
    task = asyncio.create_task(drain_now(state, reason))
    _BACKGROUND_DRAINS.add(task)
    task.add_done_callback(_BACKGROUND_DRAINS.discard)


async def queue_privacy_sync(state, reason: str) -> None:
    """Queue a share-filter pass because `reason` left a filter write owed, and drain it now.

    The one entry point for "something changed and somebody's `label!=` excludes are now wrong".
    `privacy.sync` is `engine_run(ctx, [])`: it merges every account's filter and creates, promotes
    and delivers nothing, so it can only ever make the server MORE private (plex-safety rule 1) —
    which is what makes it safe to fire straight from a mutation handler. It does also reposition our
    hubs on the Recommended shelf, which is a Plex WRITE but a position-only one: it cannot promote,
    unhide, or change who sees anything.

    ONE EXCEPTION, and it is bounded: for an account the owner set `manage_sharing=0` ("leave their Plex sharing
    alone"), the pass REMOVES our per-person excludes from that one account instead of merging into it
    (`pipeline._leave_sharing_alone`). That widens what that account sees, by request. It stays handler-safe
    because it creates and promotes nothing — no row becomes visible that was not already on the server — and it
    touches nobody else's filter, so every other account still excludes that person's row. Anything else that
    widens visibility needs its own argument; rule 1 does not cover it.
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
            # `started_at is None` means CLAIMED BUT NOT YET STARTED — `_claim` leaves it unset on
            # purpose and `_execute` stamps it. A writer can sit there for up to WRITER_LOCK_WAIT_S
            # waiting for the Plex lock, and a reader waits on the semaphore unbounded. Requeuing
            # those is not recovery, it is duplication: the original coroutine is still alive and
            # about to run the job, and the requeued copy gets claimed by the next drain, so one
            # "disable everyone" batch could execute the same cleanup twice.
            #
            # At BOOT the same state means the opposite — no coroutine survived the restart — which
            # is exactly what `boot=True` is for, and why it still reclaims them.
            if not boot and (started is None or now - started < STALE_AFTER):
                continue  # genuinely still running (or still waiting to) in this process
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


def _claim(
    sessions, *, allow_writers: bool = True, allow_history: bool = True, only_kind: str | None = None
) -> tuple[int, str] | None:
    """Take the oldest runnable queued job whose backoff has elapsed, atomically. Returns (id, kind).

    SQLite has no ``SELECT ... FOR UPDATE SKIP LOCKED``, so the select-then-update is wrapped in
    ``BEGIN IMMEDIATE``, which takes the write lock up front and makes the pair atomic against any
    other claimer — including the concurrent read-only handlers this now dispatches.
    """
    now = datetime.now(UTC)
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        try:
            query = session.query(Job).filter(Job.status == "queued")
            if only_kind is not None:
                query = query.filter(Job.kind == only_kind)
            for job in query.order_by(Job.created_at, Job.id).all():
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
            add_audit(session, "job.failed", "error", job_id=job.id, kind=job.kind, error=error, attempts=job.attempts)
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


async def drain_kind(state, kind: str) -> int:
    """Run the queued jobs of ONE kind, and nothing else.

    `drain_now` drains the WHOLE queue, which is right for an operator action — they changed
    something and everything pending should settle. It is wrong as a reflex to someone pressing play.
    Seven kinds write to Plex or plex.tv, and the listener's wake fires on every session start and
    stop, including from `_close_all` during shutdown — after the lifespan has deliberately stopped
    the scheduler first. That let a playback event begin a share-filter merge on the way out of the
    process, with the drain never resuming to mark the job finished.

    So a playback event may only ever run the read-only pass it actually asked for. Same lock and
    same claim path; only the kind is narrowed.
    """
    # NOT while a run holds the writer. Only the job HANDLER goes to an executor; `_claim`'s
    # `BEGIN IMMEDIATE` is synchronous SQLite on the event loop, so against a held writer it stalls
    # the loop for the full `busy_timeout` — measured at 5.24s, blocking the websocket, every SSE
    # stream and every request — and then raises `OperationalError` anyway. The job is not lost: the
    # scheduler tick drains it the moment the run finishes. Skipping costs a delay that was going to
    # happen regardless; not skipping costs five seconds of the whole app.
    if _plex_busy(state):
        return 0
    sessions = state.sessions
    if _DRAIN_LOCK.locked():
        return 0
    async with _DRAIN_LOCK:
        return await _drain(state, sessions, only_kind=kind)


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
    # A handler that declares `job_id` gets its own row id. Only the watching-account transfer wants
    # it — a retry must reuse the snapshot the FIRST attempt took, or it records the half-mirrored
    # state the failed attempt left behind and the real "before" is lost. Passed by signature rather
    # than to every handler, so the other fifteen are untouched.
    if "job_id" in inspect.signature(fn).parameters:
        fn = functools.partial(fn, job_id=job_id)
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


async def _drain(state, sessions, only_kind: str | None = None) -> int:
    ran = 0
    dispatched: set[asyncio.Task] = set()
    # A separate list, because the done-callback below removes finished tasks from `dispatched` —
    # gathering that set would only ever inspect the SLOW ones, and quietly skip the results of
    # everything that finished first.
    started: list[asyncio.Task] = []
    writers: list[tuple[int, str]] = []
    sem = asyncio.Semaphore(_max_parallel_readonly(state))
    try:
        while True:
            busy = _plex_busy(state)
            claimed = _claim(sessions, allow_writers=not busy, allow_history=not busy, only_kind=only_kind)
            if claimed is None:
                break
            job_id, kind = claimed
            if writes_plex(kind):
                # Writers are SERIALISED by the Plex lock anyway, so dispatching them as N concurrent
                # tasks just puts N-1 of them in a 60s `wait_for` on that lock. Disable forty people
                # and the tail stands down on timeout and is re-claimed next tick — the same work,
                # done repeatedly. Queue them for one sequential runner instead: identical ordering,
                # no contention, and a run can still interleave because the lock is taken per job.
                writers.append((job_id, kind))
                ran += 1
                continue
            task = asyncio.create_task(_run_reader(sem, state, sessions, job_id, kind))
            dispatched.add(task)
            started.append(task)
            task.add_done_callback(dispatched.discard)
            ran += 1
    finally:
        # The writers, in claim order, one at a time.
        if writers:

            async def _run_writers_in_order() -> None:
                for job_id, kind in writers:
                    await _run_writer(state, sessions, job_id, kind)

            task = asyncio.create_task(_run_writers_in_order())
            dispatched.add(task)
            started.append(task)
            task.add_done_callback(dispatched.discard)
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
#
# ONE dry-run idiom, everywhere (plex-safety rule 8):
#
#     requested = payload.get("dry_run", False)
#     ctx = state.run_service.build_context(dry_run=requested)
#     dry_run = ctx.config.dry_run or requested
#
# and then `dry_run` for every decision, log line, audit field and detail string.
#
# Both halves matter. `build_context` ORs `SHORTLIST_DRY_RUN` in, so the value the caller asked for
# and the value that governs the writes are NOT the same variable — reading the caller's is how the
# audit trail came to record a preview as a real removal. And the `or requested` is the floor: the
# chokepoint may force a preview ON, never off, so a context that somehow dropped the flag cannot
# turn "show me what this would delete" into a deletion. Five handlers wrote five different versions
# of this; only one was right.


@handler("sync.check")
def _sync_check(state, payload: dict) -> dict:
    """Take every Shortlist row the last run could not reach off the OWNER's Home.

    Promotion only ever writes flags for people IN a run, so a row belonging to anyone paused,
    disabled, deselected or caught by a run that died keeps its flags indefinitely. Clearing
    own-home is monotonically private, so this needs no privacy gate.
    """
    from shortlist.engine.models import RunReport
    from shortlist.engine.pipeline import _build_indexes, _converge_phase, _order_phase

    report = RunReport(started_at=datetime.now(UTC))
    requested = payload.get("dry_run", False)
    ctx = state.run_service.build_context(dry_run=requested)
    dry_run = ctx.config.dry_run or requested
    # Deleting is only ever a DELIBERATE act. This job now has a nightly schedule, so an unattended
    # pass must not be able to destroy a collection on its own — it demotes, and reports what it
    # WOULD remove. The owner sees that in the preview and presses Fix, which arrives here with
    # `confirmed`. Without this split, upgrading turned on a job that silently deleted from Plex.
    confirmed = bool(payload.get("confirmed"))
    # A DRY RUN is granted the same authority so that it REPORTS the orphans a real pass would
    # destroy. `may_delete=confirmed or dry_run` withheld it, so converge took the "not allowed
    # to delete" branch and filed every orphan under `converged` instead — leaving `orphans` empty in
    # the preview the UI reads, so its "this will delete N collections, this cannot be undone"
    # callout could never render and Fix deleted unannounced. Granting it here cannot delete
    # anything: `dry_run` is True only when `ctx.config.dry_run` is (it is one of the two terms it is
    # OR'd from), and converge checks that flag before every delete, logging the would-be removal.
    _converge_phase(ctx, set(), report, may_delete=confirmed or dry_run)
    # A row stranded at the bottom of the Recommended shelf IS a row "in the wrong place", which is
    # what this button says it fixes — so put the shelf right here too, not only on a full run. It is
    # cosmetic and privacy-neutral (positions only, on hubs already promoted and browse-hidden), so it
    # needs no privacy gate and `_apply_order` swallows its own failures. `_build_indexes` with no
    # users names the libraries rows live in without reading a single item inside them.
    _build_indexes(ctx, [], ctx.plex.sections())
    _order_phase(ctx, report)
    _audit_hub_orderings(state, report, dry_run)
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
    moved_in = [e for e in report.hub_orderings if e.get("placed") is not False]
    if moved_in:
        libraries = ", ".join(entry.get("library", "?") for entry in moved_in)
        detail += f"; {'would reposition' if dry_run else 'repositioned'} rows on the shelf in {libraries}"
    # Placements we could NOT honour (`pipeline.UNPLACEABLE`). Said out loud rather than folded into
    # the line above, which would report a burial as a reposition.
    unplaced = [e for e in report.hub_orderings if e.get("placed") is False]
    if unplaced:
        detail += f"; could NOT place rows in {', '.join(e.get('library', '?') for e in unplaced)}"
    return {"fixed": report.converged, "orphans": removed, "detail": detail}


def _require_filters_merged(report, what: str) -> None:
    """Raise unless every account's excludes were actually merged.

    `engine_run(ctx, [])` does NOT raise when the privacy pass fails — it reports by RETURN VALUE
    (`_privacy_sync_phase` gives back None for an unreadable roster, False for a failed write, and
    `run()` returns the report early). Two handlers here were written against a docstring that
    claimed otherwise, so they read a failed pass as a success:

    * `user.restore` promoted a row onto shared Home with no exclude hiding it from anyone else —
      the leak direction, on the one job whose whole purpose is making something MORE visible.
    * `privacy.sync` marked itself `done` with "Share filters merged for every account", retiring an
      owed HIDE from the durable queue so nothing ever retried it.

    Raising puts both back on the queue's retry-with-backoff, and on exhaustion into the `job.failed`
    audit that reaches the notification bell — which is what their own docstrings already promise.

    A plex.tv 422 for a restricted/managed account is NOT a blocker (`pipeline.py:561` skips it as
    expected), so this cannot fire nightly over an account Plex will never accept filters for.
    """
    if report.error or report.promotion_blockers:
        why = report.error or "; ".join(report.promotion_blockers)
        raise RuntimeError(f"share filters were not merged, so {what} is unsafe: {why}")


def _audit_hub_orderings(state, report, dry_run: bool) -> None:
    """Audit each library whose Recommended-shelf order we moved, and each one whose configured
    placement could not be applied at all (`pipeline.UNPLACEABLE`) — plex-safety rule 10.

    `run_persistence._emit_hub_ordering_events` only fires for a persisted RUN, and the two handlers
    that now order — `privacy.sync` and `sync.check` — persist no run. Without this the `verified: False`
    record exists only on the one path that never needed it, and a shelf we lost to another tool would
    go unrecorded on both paths this was fixed for.

    An unplaceable entry gets its OWN scope. `_shelf_contention` reads `shelf.order`/`run.hub_order`
    within a 500-event budget looking for rows moved over and over; one stale anchor writes a record
    every pass, and `privacy.sync` fires on every who-sees-what change, so sharing the scope would let
    a setting nobody has fixed crowd out the evidence contention detection actually needs.
    """
    for entry in report.hub_orderings:
        verified = entry.get("verified")
        unplaced = entry.get("placed") is False
        write_audit(
            state,
            "shelf.unplaced" if unplaced else "shelf.order",
            # A dry run asked Plex for nothing, so it is a preview either way, never a warning.
            "info" if dry_run else ("warning" if unplaced or verified is False else "info"),
            library=entry.get("library"),
            anchor=entry.get("anchor"),
            moved=entry.get("moved", []),
            verified=verified,
            reason=entry.get("reason"),
            # The row that could not be placed, when the record is about one. Kept apart from
            # `anchor`, which everywhere else names what we anchored TO (rule 10).
            row=entry.get("row"),
            dry_run=dry_run,
        )


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

    ONE EXCEPTION, and it is bounded: for an account the owner set `manage_sharing=0` ("leave their Plex sharing
    alone"), the pass REMOVES our per-person excludes from that one account instead of merging into it
    (`pipeline._leave_sharing_alone`). That widens what that account sees, by request. It stays handler-safe
    because it creates and promotes nothing — no row becomes visible that was not already on the server — and it
    touches nobody else's filter, so every other account still excludes that person's row. Anything else that
    widens visibility needs its own argument; rule 1 does not cover it.

    It DOES write one more thing to Plex: the Recommended-shelf position of our own hubs. That is a
    real PUT, so it is named here rather than left to be discovered — but it is position-only, on hubs
    already promoted and already covered by the excludes this pass merges, so it cannot make anything
    visible to anyone. Before 2026-08-12 this pass ordered nothing at all, which is why a shelf left in
    pieces could not be repaired by any job.
    """
    from shortlist.engine.pipeline import run as engine_run

    requested = payload.get("dry_run", False)
    ctx = state.run_service.build_context(dry_run=requested)
    dry_run = ctx.config.dry_run or requested
    report = engine_run(ctx, [])
    _require_filters_merged(report, "reporting the filters as merged")
    _audit_hub_orderings(state, report, dry_run)
    swept = sum(len(titles) for titles in report.swept_rows.values())
    # The reason is carried through to the detail line so the Jobs page answers "why did this fire?"
    # — "someone was removed from a shared row" reads very differently from a nightly housekeeping
    # pass, and an operator seeing filters rewritten deserves to know which.
    reason = str(payload.get("reason") or "")
    detail = "Share filters would be merged" if dry_run else "Share filters merged for every account"
    if reason:
        detail += f" after {reason}"
    if swept:
        detail += f"; swept {swept} unhidable row(s)"
    # `privacy.sync` persists no run, so `report.left_alone_failures` has nowhere else to surface —
    # and this handler is the one the "leave their sharing alone" PATCH fires. Without this the toast
    # says the filters were merged while that account kept every exclude we promised to remove.
    if report.left_alone_failures:
        failed = len(report.left_alone_failures)
        detail += f"; could NOT clear Shortlist's exclusions from {failed} left-alone account(s)"
    # This job repositions rows on the shelf now, and its own description promises it does. Reported
    # here in the same words `sync.check` uses, so the two jobs do not describe the same write
    # differently — the audit event is written either way (`_audit_hub_orderings`, rule 10); this is
    # the line an operator actually reads on the Jobs page.
    moved_in = [e for e in report.hub_orderings if e.get("placed") is not False]
    if moved_in:
        libraries = ", ".join(entry.get("library", "?") for entry in moved_in)
        detail += f"; {'would reposition' if dry_run else 'repositioned'} rows on the shelf in {libraries}"
    # Placements we could NOT honour (`pipeline.UNPLACEABLE`). Said out loud rather than folded into
    # the line above, which would report a burial as a reposition.
    unplaced = [e for e in report.hub_orderings if e.get("placed") is False]
    if unplaced:
        detail += f"; could NOT place rows in {', '.join(e.get('library', '?') for e in unplaced)}"
    return {"swept": swept, "converged": report.converged, "reason": reason, "dry_run": dry_run, "detail": detail}


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

    from shortlist.server.services.user_sync import sync_users_from_state

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
    """Re-read every enabled user's watched set, plus the owner's. Durable for the same reason
    `sync.users` is.

    The owner is included whether or not they have a row: `user_sync` creates them `enabled=False`,
    and their watched set is what the watching-account transfer copies FROM (#88).

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
    return {"detail": "Watch history re-read for every enabled user, plus the owner"}


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


@handler("watch.reconcile")
def _watch_reconcile(state, payload: dict) -> dict:
    """Credit what playback alone can justify, then tell the dashboard.

    The SSE is not a nicety. Without it the owner watches something, the credit lands in the database
    seconds later, and the page in front of them still says nothing until they reload — which reads as
    the feature not working. It rides `sync.finished`, the event the report query already invalidates
    on, rather than inventing a second channel for the same fact.

    But under its OWN kind. Sent as `watched` it impersonated the watch-history sync, and the Jobs
    page renders that as "Synced N users — watch history is up to date and the effectiveness report
    reflects it now" — so anyone stopping a video while that page was open saw a green success for
    work nobody did, with a count that means "users whose picks changed".

    Deliberately does NOT wait for a run, unlike the full watch sync which takes `run_lock`. That lock
    exists because the full pass re-reads Plex and rewrites every user; this writes only our own
    `picks` stamps and touches no Plex state at all. Running mid-build is safe in the direction that
    matters: `_apply_outcomes` will not stamp a delivery row whose `created_at` is later than the
    watch, so a pick the run is creating right now can never be credited for a play that predates it.
    The failure it can have is CONSERVATIVE — a row not yet in the delivery ledger yields no credit,
    and the nightly sync reaches the same conclusion later from the same records. Waiting instead
    would mean an 88-minute run silently swallowing every partial watch made during it.
    """
    from shortlist.server.services.run_persistence import reconcile_from_events

    changed = reconcile_from_events(state.sessions)
    if changed:
        state.bus.publish("sync.finished", {"kind": "credited", "ok": True, "count": changed})
    return {"users_credited": changed}


@handler("maintenance.prune")
def _maintenance_prune(state, payload: dict) -> dict:
    """Apply the retention limits to `runs`, `events` and the cache table.

    Its OWN transaction, which is the whole point of it being a job. This used to run inside the
    run-persist transaction: a bulk delete that failed there — a locked database, a FK the delete
    order got wrong — rolled back the persist along with it, discarding the record of a run that had
    already written to Plex. Retention is housekeeping; it must never be able to cost a run.

    Idempotent by construction: it deletes whatever is currently older than the limit, so replaying
    it after a crash simply finds nothing left to delete.
    """
    from shortlist.server.services.run_persistence import prune_events, prune_expired_cache, prune_runs

    with state.sessions() as session:
        store = SettingsStore(session)
        # `or 0` on BOTH: a legacy or hand-edited row storing null made `int(None)` raise — and it
        # raised inside the run's persist transaction, where the cost was the whole persist.
        months = int(store.get("runs.retention") or 0)
        event_months = int(store.get("events.retention") or 0)
        # Legacy DBs store the old count-based "100" — values beyond the new 24-month max are treated
        # as 0 (keep forever) until the owner visits Settings and sets a real month value.
        runs = prune_runs(session, months if 0 < months <= 24 else 0)
        events = prune_events(session, event_months if 0 < event_months <= 24 else 0)
        cached = prune_expired_cache(session)
        session.commit()
    return {
        "runs": runs,
        "events": events,
        "cache_rows": cached,
        "detail": f"Pruned {runs} run(s), {events} audit event(s) and {cached} expired cache row(s)",
    }


@handler("user.cleanup")
def _user_cleanup(state, payload: dict) -> dict:
    """Remove a disabled user's collections. Retried, unlike the old fire-and-forget call — which is
    the bug: if Plex was down at the moment of disabling, nothing ever revisited them, because no
    run touches a disabled user."""
    from shortlist.engine.delivery import remove_row_collections
    from shortlist.server.services.collection_reconcile import forget_user_deliveries

    slug = payload["slug"]
    # Through the chokepoint, not `force_dry_run()` here: this used to call the safe-mode helper
    # itself, which is the one idiom `build_context` exists to make unnecessary — and the only way the
    # value the handler logs can drift from the value the writes actually used.
    requested = payload.get("dry_run", False)
    ctx = state.run_service.build_context(dry_run=requested, plex_only=True)
    dry_run = ctx.config.dry_run or requested
    removed = remove_row_collections(
        ctx.plex, ctx.config, label=f"{LABEL_PREFIX}_{slug}", displays=None, dry_run=dry_run
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
    write_audit(state, "user.disable.cleanup", "warning", user=slug, removed=removed, dry_run=dry_run)
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
    # the payload asks for — and `demote_all` has no dry-run branch of its own (rule 8).
    requested = payload.get("dry_run", False)
    ctx = state.run_service.build_context(dry_run=requested, plex_only=True)
    dry_run = ctx.config.dry_run or requested
    hidden: list[str] = []
    label = f"{LABEL_PREFIX}_{slug}"
    for section in ctx.plex.sections():
        for collection in ctx.plex.find_owned_collections(section, label):
            if dry_run:
                if ctx.plex.claims_any_surface(collection):
                    logger.info("[dry-run] {}: would take off every surface", collection.title)
                    hidden.append(collection.title)
                continue
            if ctx.plex.demote_all(collection):
                hidden.append(collection.title)
    write_audit(state, "user.pause.hide", "info", user=slug, hidden=len(hidden), dry_run=dry_run)
    verb = "Would hide" if dry_run else "Hid"
    return {"hidden": hidden, "dry_run": dry_run, "detail": f"{verb} {len(hidden)} row(s) for {slug}"}


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

    `engine_run(ctx, [])` merges every account's filter and delivers, creates and promotes nothing. It
    does NOT raise on failure — it reports by RETURN VALUE — so `_require_filters_merged` checks the
    report and raises. The whole job is then retried, and nothing is promoted meanwhile.
    """
    from shortlist.engine.models import UserProfile, UserType
    from shortlist.engine.pipeline import promote_user_rows
    from shortlist.engine.pipeline import run as engine_run
    from shortlist.server.db.models import Delivery, Run, RunUser, User

    slug = payload["slug"]
    requested = payload.get("dry_run", False)
    ctx = state.run_service.build_context(dry_run=requested)
    dry_run = ctx.config.dry_run or requested
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

    # Rule 1's ordering: every account's excludes are merged BEFORE anything is promoted — and the
    # merge is CHECKED, not assumed. `promote_user_rows` has no `filters_ok` guard of its own (only
    # `_promote_phase` does), so an unchecked failure here promotes a private row with nothing
    # hiding it.
    report = engine_run(ctx, [])
    _require_filters_merged(report, f"promoting {slug}'s rows")
    restored = promote_user_rows(ctx, profile, placements, placement_keys=keys)
    # `dry_run` recorded, not assumed False: `promote_user_rows` carries its own safe-mode guard, so
    # under SHORTLIST_DRY_RUN it returns the keys it WOULD have promoted and nothing on Plex moved.
    # Auditing that as a real restore is the audit trail lying about a visibility change.
    write_audit(state, "user.unpause.restore", "info", user=slug, restored=len(restored), dry_run=dry_run)
    verb = "Would put" if dry_run else "Put"
    return {
        "restored": sorted(restored),
        "dry_run": dry_run,
        "detail": f"{verb} {len(restored)} row(s) back for {slug}",
    }


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
    # The EFFECTIVE value, back off the context the removal actually used. This audited a hardcoded
    # `False` while `build_context` had OR'd SHORTLIST_DRY_RUN in below it — so under safe mode the
    # audit trail recorded a preview as a real deletion of somebody's row.
    dry_run = _reconcile_row_removal(
        state,
        slug=slug,
        build=build,
        dry_run=payload.get("dry_run", False),
        removed=removed,
        only_user_ids=only_user_ids,
        # Carried in the payload by the DELETE path only: the row is gone by the time a retry runs, so
        # the template it was titled with cannot be looked up any more. Everywhere else it reads the DB.
        template=payload.get("template"),
        # Carried by a NARROWED row: only the libraries it walked away from, so the ones it still
        # targets survive. Absent means every library, which is right for an outright removal.
        in_sections=set(payload["in_sections"]) if payload.get("in_sections") is not None else None,
    )
    write_audit(state, payload.get("scope", "row.reconcile"), "warning", slug=slug, removed=removed, dry_run=dry_run)
    verb = "Would remove" if dry_run else "Removed"
    return {
        "removed": removed,
        "dry_run": dry_run,
        "detail": f"{verb} {len(removed)} collection(s) for row {slug}",
    }


@handler("watching_account.transfer")
def _watching_account_transfer(state, payload: dict, job_id: int | None = None) -> dict:
    """Replicate one account's watch state onto a watching account.

    The source is the owner unless `from_user_id` names another — and it is read with THAT account's
    own server token, never the admin's, or the owner's history lands on the target while the audit
    row records somebody else as the source.

    On the queue rather than inline in the request for two reasons. It is ~11,000 PMS writes on a
    heavy account, which is minutes of work behind a reverse proxy that will happily time the request
    out at 60s — and the job row means the work still finishes, and is still recoverable from the
    Jobs page, when it does. And it is the only path in Shortlist that can DELETE watch history, so it
    needs the same durable record as every other writer.

    Idempotent, which the queue requires of every handler: the write plan is rebuilt from a fresh read
    of BOTH accounts each attempt, so a replay writes only what is still missing rather than
    re-applying what already landed. The rewatch counts are the case that would break under a naive
    replay, and `WriteOp.scrobbles` carries the shortfall precisely so they do not.
    """
    from shortlist.engine.history import ShareTokenWatchSource
    from shortlist.engine.models import UserProfile, UserType
    from shortlist.server.db.models import User
    from shortlist.server.services.watching_account import transfer_watch_history

    to_user_id = int(payload["to_user_id"])
    requested = bool(payload.get("dry_run", False))
    ctx = state.run_service.build_context(dry_run=requested, plex_only=True)
    # `or requested` is the floor, matching every other writer here: the chokepoint may force a dry
    # run ON, never off, so a context that dropped the flag cannot turn a preview into a real run.
    dry_run = bool(ctx.config.dry_run) or requested

    with state.sessions() as session:
        owner = session.query(User).filter(User.user_type == "owner").first()
        if owner is None:
            raise LookupError("no owner account is registered yet — run a user sync first")
        source_id = int(payload.get("from_user_id") or owner.id)
        source = session.get(User, source_id)
        target = session.get(User, to_user_id)
        if source is None or target is None:
            raise LookupError("both the source and the target must be known users")
        source_profile = UserProfile(
            username=source.username,
            plex_account_id=source.plex_account_id,
            user_type=UserType(source.user_type),
            slug=source.slug,
        )
        account_id = target.plex_account_id

    # The SOURCE's own token, not the admin's. Reading a shared account with the admin token would
    # replicate the OWNER's watching onto the target while claiming to copy that person's — one
    # account's history silently wearing another's name, which is the failure `_check_pair` guards
    # for the target and nothing guarded for the source while it was hardcoded to the owner.
    #
    # `server_token_for` is the single implementation of the owner/shared/managed split; a second
    # copy here would be a second place to get it wrong.
    source_token = ShareTokenWatchSource(ctx.plex, ctx.plextv, owner_token=ctx.plex.token).server_token_for(
        source_profile
    )
    if not source_token:
        raise LookupError(f"no server token could be obtained for {source.username!r} — cannot read its watching")

    token = ctx.plextv.canary_server_token(account_id)
    with state.sessions() as session:
        report = transfer_watch_history(
            session,
            sessions=state.sessions,
            job_id=job_id,
            from_user_id=source_id,
            to_user_id=to_user_id,
            plex=ctx.plex,
            source_token=source_token,
            target_token=token,
            dry_run=dry_run,
        )
        add_audit(
            session,
            "watching_account.transfer",
            "info",
            from_user_id=source_id,
            to_user_id=to_user_id,
            **report.as_dict(),
        )
        session.commit()
        out = report.as_dict()

    verb = "Would copy" if dry_run else "Copied"
    removed = report.unmarks + report.offsets_cleared
    out["detail"] = (
        f"{verb} {report.marks} title(s) onto that account"
        + (f", removing {removed}" if removed else "")
        + (" (preview)" if dry_run else "")
    )
    return out


@handler("watching_account.undo")
def _watching_account_undo(state, payload: dict, job_id: int | None = None) -> dict:
    """Put a watching account back exactly as its transfer found it.

    Restores from the snapshot rather than replaying the writes backwards — see `undo_transfer`. Safe
    to replay: the snapshot is stamped `restored_at` on success and a second pass refuses rather than
    planning against a state it no longer describes.
    """
    from shortlist.server.db.models import WatchStateSnapshot
    from shortlist.server.services.watching_account import undo_transfer

    snapshot_id = int(payload["snapshot_id"])
    requested = bool(payload.get("dry_run", False))
    with state.sessions() as session:
        snapshot = session.get(WatchStateSnapshot, snapshot_id)
        if snapshot is None:
            raise LookupError("no snapshot with that id — nothing to restore")
        user_id = snapshot.user_id
        from shortlist.server.db.models import User

        user = session.get(User, user_id)
        account_id = user.plex_account_id if user else 0

    ctx = state.run_service.build_context(dry_run=requested, plex_only=True)
    dry_run = bool(ctx.config.dry_run) or requested
    token = ctx.plextv.canary_server_token(account_id)

    with state.sessions() as session:
        report = undo_transfer(
            session,
            sessions=state.sessions,
            job_id=job_id,
            snapshot_id=snapshot_id,
            plex=ctx.plex,
            target_token=token,
            dry_run=dry_run,
        )
        add_audit(
            session,
            "watching_account.undo",
            "info",
            restored_snapshot_id=snapshot_id,
            to_user_id=user_id,
            **report.as_dict(),
        )
        session.commit()
        out = report.as_dict()

    out["detail"] = f"{'Would restore' if dry_run else 'Restored'} {report.planned} change(s) on that account"
    if report.errors:
        out["detail"] = report.errors[0]
    return out


@handler("rows.visibility")
def _rows_visibility(state, payload: dict) -> dict:
    """Make Plex match today's row day-schedules ("When it appears", issue #102).

    Rows build at 03:30, so a run cannot be what turns a row over: a Monday row would sit on people's
    Home until 03:30 Tuesday, and a weekly-rebuilding row for days. This is what makes a day schedule
    mean anything, and it is why the schedule is a MIDNIGHT job rather than a flag a run reads.

    **The gate comes first, before anything is built.** ``build_context`` constructs a PMS client (an
    HTTP fetch), plex.tv, TMDB and the curator, so deciding whether there is work AFTER it would make
    the advertised "a quiet night costs nothing" false on every night of the year. So the decision is
    taken straight from the database.

    Only rows that actually carry a schedule are considered. Every row has ``show_days=[]`` and
    ``shown_state=NULL`` immediately after migration 0088, and counting those as a change would make
    the first midnight after upgrade converge every server that has never touched this feature. A row
    whose days were just CLEARED is still settled once, because it has a non-NULL ``shown_state``; it
    then drops out of tracking on the next tick that has any work at all — the final loop records
    every slug it considered, not just the ones that moved. So a clear that does not flip today's
    answer settles as soon as some other row turns over, which is the state we want anyway; it is
    never worth a write on a quiet tick purely to tidy up.

    plex-safety rule 1: this can make a row MORE visible, so every account's excludes are merged and
    CHECKED first, exactly as ``user.restore`` does. Someone may have joined the server while a row was
    hidden, and their share carries no `label!=` exclude for it yet. A failed merge raises so the whole
    job retries, and ``shown_state`` is written only after the converge lands, so a retry still sees
    work to do rather than "nothing changed".

    The roster comes from ``enabled_profiles``, NOT a query written here: that is the one place the
    three exclusions live — the Danger Zone's ``paused_all`` kill switch, an account with a parental
    restriction profile (Plex refuses it a label filter, so it can never have a private row), and a
    individually paused person. This is the first scheduled task that writes to people's shelves, so a
    kill switch it did not honour would be a kill switch in name only.
    """
    from shortlist.engine.pipeline import identity_map, promote_shared_row, promote_user_rows
    from shortlist.engine.pipeline import run as engine_run
    from shortlist.engine.rows import row_is_shown
    from shortlist.server.db.models import Collection, Delivery
    from shortlist.server.services.context_builder import _local_now

    requested = bool(payload.get("dry_run", False))
    now = _local_now()

    # --- the gate: pure DB, no clients, no network -----------------------------------------
    with state.sessions() as session:
        rows = session.query(Collection).filter_by(enabled=True).all()
        # A row is tracked if it HAS a schedule, or if it had one recently enough that we recorded a
        # state for it (so clearing the days converges once, then stops being asked about).
        desired = {
            row.slug: row_is_shown(row.show_days, now)
            for row in rows
            if (row.show_days or []) or row.shown_state is not None
        }
        tracked = {row.slug: bool(row.show_days) for row in rows if row.slug in desired}
        changed = sorted(row.slug for row in rows if row.slug in desired and row.shown_state != desired[row.slug])

    if not changed:
        return {"changed": [], "dry_run": requested, "detail": "Every row is already on the surfaces today asks for"}

    # The Danger Zone kill switch stops this like every other scheduled task — and CRUCIALLY without
    # recording `shown_state`, so the transition is still owed when the pause is lifted.
    #
    # Found live: `enabled_profiles` already returns [] under `paused_all`, so the converge loop
    # promoted nothing, but the handler still wrote the state as though it had. Clearing the pause
    # then reported "every row is already on the surfaces today asks for" and the row stayed up on a
    # day its schedule said to hide it — permanently, until the owner next edited the days.
    with state.sessions() as session:
        if SettingsStore(session, state.secrets).get("paused_all"):
            logger.info("all runs are paused (Settings → Danger Zone) — leaving today's row schedule unapplied")
            return {
                "changed": [],
                "dry_run": requested,
                "detail": f"{len(changed)} row(s) are waiting for today's schedule — everything is paused",
            }

    shown = [slug for slug in changed if desired.get(slug)]
    hidden = [slug for slug in changed if not desired.get(slug)]
    parts = []
    if shown:
        parts.append(f"showing {', '.join(shown)}")
    if hidden:
        parts.append(f"hiding {', '.join(hidden)}")
    summary = f"Today's schedule: {' and '.join(parts)}"

    ctx = state.run_service.build_context(dry_run=requested)
    dry_run = ctx.config.dry_run or requested

    if dry_run:
        # Audited like every other preview (rule 10): a visibility change nobody can account for is
        # the thing the events feed exists to prevent, and `user.hide`/`user.restore` both record one.
        write_audit(state, "rows.visibility", "info", changed=changed, collections=0, dry_run=True)
        logger.info("[dry-run] rows.visibility: would converge {}", ", ".join(changed))
        return {"changed": changed, "dry_run": True, "detail": f"Would update {len(changed)} row(s) for today"}

    # Rule 1's ordering, in straight-line code — see the docstring. `engine_run(ctx, [])` merges every
    # account's filter and creates/promotes nothing; it reports failure by RETURN VALUE, so it is
    # checked rather than assumed.
    report = engine_run(ctx, [])
    _require_filters_merged(report, "applying today's row schedule")

    touched: set[int] = set()
    with state.sessions() as session:
        profiles = [p for p in state.run_service.enabled_profiles(session) if p.plex_account_id]
        # {ratingKey -> row slug} per user, from the delivery ledger. The AUTHORITATIVE answer to
        # "which of this person's rows is this collection": a per-person row's Plex label names the
        # PERSON, not the row (all of Sarah's rows carry `shortlist_sarah`).
        #
        # Through the engine's `identity_map`, not a second copy of the rule written here: an ambiguous
        # key must be DROPPED rather than arbitrated, and that is the branch where guessing wrong
        # promotes a row today's schedule says to hide. One implementation, one set of tests.
        ledger_by_user = identity_map(
            {(d.user_slug, d.collection_slug, d.library_key): d.rating_key for d in session.query(Delivery)}
        )

    for profile in profiles:
        # All-or-nothing on purpose: `shown_state` is recorded only after every profile is through, so
        # raising here leaves the whole pass owed and the durable queue retries it with backoff. The
        # alternative — carrying on and recording success — would mark rows converged that are not.
        promote_user_rows(
            ctx,
            profile,
            {},
            placement_keys=ledger_by_user.get(profile.slug, {}),
            into=touched,
            # A collection this cannot identify is LEFT ALONE. Promotion's no-spec fallback shows the
            # row, which is right for a run and exactly wrong here: a `{top_seed}` row with no ledger
            # key would be promoted onto Home on a day its schedule says to hide it.
            skip_unmatched=True,
        )

    for spec in ctx.config.shared_rows():
        promote_shared_row(ctx, spec, into=touched)

    # Only AFTER the converge landed: writing this first would make a failed retry see "nothing
    # changed" and leave the row wrong until the schedule next moved — a day, or a week. A row whose
    # days were cleared goes back to NULL so it stops being tracked at all.
    with state.sessions() as session:
        for row in session.query(Collection).filter_by(enabled=True):
            if row.slug in desired:
                row.shown_state = desired[row.slug] if tracked.get(row.slug) else None
        session.commit()

    write_audit(state, "rows.visibility", "info", changed=changed, collections=len(touched), dry_run=False)
    return {"changed": changed, "collections": len(touched), "dry_run": False, "detail": summary}
