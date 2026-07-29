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
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import text

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.delivery import row_marker
from shortlist.server.db.models import Event, Job

# A job still marked `running` this long after it started is presumed dead — its process is gone.
# Generous on purpose: a real sync check on a large server legitimately takes minutes, and requeuing
# a job that is merely slow means running it twice.
STALE_AFTER = timedelta(minutes=30)

# Backoff between attempts, indexed by attempt number. A Plex/plex.tv outage is usually minutes, so
# the tail is deliberately long rather than hammering a server that is already unhappy.
_BACKOFF_S = (30, 300, 900)

Handler = Callable[[object, dict], dict]  # (app.state, payload) -> result

_HANDLERS: dict[str, Handler] = {}

# Kinds the UI may trigger by hand. A deliberate allow-list, not `_HANDLERS.keys()`: `user.cleanup`
# takes a slug and DELETES that person's rows, so it must never be runnable from a generic button.
KINDS = ("sync.check", "privacy.sync")


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


def _claim(sessions) -> int | None:
    """Take the oldest queued job whose backoff has elapsed, atomically. Returns its id.

    SQLite has no ``SELECT ... FOR UPDATE SKIP LOCKED``, so the select-then-update is wrapped in
    ``BEGIN IMMEDIATE``, which takes the write lock up front and makes the pair atomic against any
    other claimer. There is one worker today; this keeps it correct if that ever changes.
    """
    now = datetime.now(UTC)
    with sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        try:
            for job in session.query(Job).filter(Job.status == "queued").order_by(Job.created_at, Job.id).all():
                if job.attempts:
                    ready_at = job.finished_at or job.created_at
                    if ready_at is not None and ready_at.tzinfo is None:
                        ready_at = ready_at.replace(tzinfo=UTC)
                    wait = _BACKOFF_S[min(job.attempts - 1, len(_BACKOFF_S) - 1)]
                    if ready_at is not None and now - ready_at < timedelta(seconds=wait):
                        continue  # still backing off after a failure
                job.status = "running"
                job.started_at = now
                job.attempts += 1
                session.commit()
                return job.id
            session.rollback()
            return None
        except Exception:
            session.rollback()
            raise


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


async def run_pending(state) -> int:
    """Drain the queue. Returns how many jobs ran.

    Three callers reach this: the 10s scheduler tick, `POST /api/system/jobs`, and the disable path.
    `max_instances=1` only guards the scheduler's own tick, and `_claim` being atomic just means two
    drains take DIFFERENT jobs — then run them at once, each with its own PlexClient, its own
    `write_lock` and its own adaptive plex.tv throttle. Two handlers writing plex.tv concurrently is
    exactly what rule 6 forbids, so drains are serialized here. A caller that finds the lock held
    returns immediately rather than queueing behind a long drain in a request path.
    """
    sessions = state.sessions
    if _plex_busy(state) or _DRAIN_LOCK.locked():
        return 0
    async with _DRAIN_LOCK:
        return await _drain(state, sessions)


async def _drain(state, sessions) -> int:
    ran = 0
    while True:
        job_id = _claim(sessions)
        if job_id is None:
            return ran
        with sessions() as session:
            job = session.get(Job, job_id)
            kind, payload = job.kind, dict(job.payload or {})
        fn = _HANDLERS.get(kind)
        if fn is None:
            # The kind was removed in an upgrade while a job was queued. Nothing can run it.
            _finish(sessions, job_id, error=f"no handler registered for {kind!r}")
            continue
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
        ran += 1
        if _plex_busy(state):
            return ran  # a run started; yield the server to it


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
    ctx = state.run_service.build_context(dry_run=bool(payload.get("dry_run")))
    _converge_phase(ctx, set(), report)
    # Deletions are reported SEPARATELY and named first. Folding them into `fixed` would hide the one
    # irreversible thing this does behind a number, in the very preview an operator reads to decide
    # whether to run it for real.
    removed = report.orphans_removed
    detail = f"Checked every row; corrected {len(report.converged)}"
    if removed:
        detail += (
            f"; {len(removed)} orphaned collection(s) to remove"
            if ctx.config.dry_run
            else (f"; removed {len(removed)} orphaned collection(s)")
        )
    return {"fixed": report.converged, "orphans": removed, "detail": detail}


@handler("privacy.sync")
def _privacy_sync(state, payload: dict) -> dict:
    """Merge every account's share filter without building anything.

    `engine_run(ctx, [])` with no users sweeps unhidable rows and writes every share filter, but
    delivers, creates and promotes NOTHING (plex-safety rule 1) — so it can only ever make the
    server more private. That is what makes it safe to fire from a mutation (a user disabled, a
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
    """
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
        keys = {
            d.rating_key: d.collection_slug for d in session.query(Delivery).filter_by(user_slug=slug) if d.rating_key
        }

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
