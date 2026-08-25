"""The owner's escape from "I see everyone's rows": move their watching to a separate Plex account.

Plex shows the server owner every per-person row on each library's Recommended shelf and offers no
way to hide them — the hiding works through each viewer's share, and the owner has none. The only
real fix is to watch as somebody else, so these endpoints find that account and move the owner's
watch history onto it.

The transfer is dry-runnable and audited (plex-safety rules 8 and 10). Shortlist never CREATES the
Plex account — see the note below `/candidates` for why that is left to the Plex app.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import or_

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.models import UserType
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Job, User, WatchStateSnapshot, iso_utc
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.services.watching_account import TransferReport, candidate_home_users

router = APIRouter(prefix="/watching-account", tags=["watching-account"], dependencies=[Depends(require_owner)])


class HomeUserOut(PassthroughModel):
    """A Home user that could become the owner's watching account."""

    plex_account_id: int
    title: str
    # PIN-protected accounts cannot be switched to automatically, so Shortlist cannot mint the
    # server token a scrobbling transfer needs. Surfaced so the UI can say why, not just disable it.
    protected: bool
    already_a_shortlist_user: bool


class TransferIn(BaseModel):
    to_user_id: int
    dry_run: bool = False


class TransferOut(PassthroughModel):
    """The result of one replication.

    `unmarks` and `offsets_cleared` are the destructive half and are reported separately from
    `applied` on purpose: "wrote 11,000 things" and "removed 412 watches from that account" are not
    the same sentence, and only the second needs anybody's consent.
    """

    planned: int
    applied: int
    # The PMS answered 401/403/404 — the title sits in a library this account is not shared. A normal
    # outcome for a target with narrower sharing, not a failure.
    unreachable: int
    # A write that RAISED. Kept apart from `unreachable` because they are opposite claims: one says
    # the title isn't there for that account, the other says we don't know what happened.
    failed: int
    marks: int
    unmarks: int
    offsets_set: int
    offsets_cleared: int
    # By name, capped — a count is not something a person can check, and this is the only part of the
    # feature that deletes.
    removals_preview: list[str]
    verify_mismatched: int
    verify_checked: int
    # Show rows un-scrobbled because every episode of them was removed. Not a leaf, so counted apart
    # from `applied` — and still a Plex write the audit row has to be able to explain.
    shows_cleared: int
    # Libraries the TARGET account cannot see. Not a failure — those titles simply cannot be written
    # there — but it makes the snapshot partial, so the undo is refused rather than trusted.
    target_unreadable: list[str]
    events_copied: int
    titles_cached: int
    # Where undo restores from. Null on a dry run, which takes no snapshot because it changes nothing.
    snapshot_id: int | None
    dry_run: bool
    # The owner's account has nothing to replicate — told apart from a plain `planned == 0`, because
    # "they already match" is success and the UI has to say something completely different (#88).
    source_empty: bool
    errors: list[str]


class UndoIn(BaseModel):
    snapshot_id: int
    dry_run: bool = False


class SnapshotOut(PassthroughModel):
    """One un-restored snapshot — an undo that is still available."""

    id: int
    user_id: int
    username: str
    taken_at: str | None
    #: How many titles it recorded. The only honest way to say how big the undo is.
    entries: int
    complete: bool


def _owner_id(session) -> int:
    owner = session.query(User).filter(User.user_type == "owner").first()
    if owner is None:
        raise HTTPException(status_code=409, detail="no owner account is registered yet — run a user sync first")
    return owner.id


@router.get("/candidates", response_model=list[HomeUserOut])
async def list_candidates(request: Request) -> list[dict]:
    """Home users on the owner's Plex Home that could become their watching account."""

    def fetch():
        # plex_only: this needs the PMS + plex.tv pair and nothing else — building the full context
        # would couple listing Home users to the TMDB key and the LLM provider being reachable.
        ctx = request.app.state.run_service.build_context(dry_run=True, plex_only=True)
        with request.app.state.sessions() as session:
            return candidate_home_users(ctx.plextv, session)

    try:
        return await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        # A plex.tv error can carry a tokened URL — redact before it reaches the response (rule 9).
        logger.warning("home-user listing failed ({})", type(e).__name__)
        raise HTTPException(status_code=502, detail=redact(f"{type(e).__name__}: {e}")) from e


# NO create-the-account endpoint, deliberately. Making a Home user is two taps in the Plex app, and
# doing it there means Shortlist never owns a plex.tv write that mints an account — which would
# briefly exist with no library filters on it, and whose response shape is an assumption nobody has
# recorded a fixture for (plex-safety rule 11). The guide asks the owner to create it in Plex and
# `/candidates` picks it up.


@router.get("/snapshots", response_model=list[SnapshotOut])
async def list_snapshots(request: Request) -> list[dict]:
    """Transfers that can still be undone, newest first.

    Without this the undo was reachable only from the in-flight response of the transfer that created
    it — so the one case the durable queue exists for (a reverse proxy timing the request out at 60s,
    a 503 while a run holds the Plex lock, or simply a page reload) was also the case where the
    destructive run completed and its undo could not be found.
    """

    def fetch():
        with request.app.state.sessions() as session:
            # UNDOs take snapshots too — that is what makes an undo undoable. But restoring one
            # RE-APPLIES the transfer it reversed, and the undo has already deleted the copied play
            # events, so the re-applied state arrives undated: every title would read as watched
            # today, which is the exact failure `source_viewed_at` exists to prevent. Offering that
            # under "an earlier copy can still be undone" would be the opposite of what it says.
            #
            # Excluded by their JOB KIND rather than a new column: `job_id` is written for both kinds,
            # so the origin is already recorded.
            undo_jobs = session.query(Job.id).filter(Job.kind == "watching_account.undo")
            rows = (
                session.query(WatchStateSnapshot, User)
                .join(User, User.id == WatchStateSnapshot.user_id)
                .filter(
                    WatchStateSnapshot.restored_at.is_(None),
                    or_(
                        WatchStateSnapshot.job_id.is_(None),
                        WatchStateSnapshot.job_id.notin_(undo_jobs),
                    ),
                )
                .order_by(WatchStateSnapshot.taken_at.desc())
                .limit(20)
                .all()
            )
            return [
                {
                    "id": snapshot.id,
                    "user_id": snapshot.user_id,
                    "username": user.username,
                    "taken_at": iso_utc(snapshot.taken_at),
                    "entries": len(snapshot.state or []),
                    "complete": bool(snapshot.complete),
                }
                for snapshot, user in rows
            ]

    return await asyncio.get_running_loop().run_in_executor(None, fetch)


@router.post("/transfer", response_model=TransferOut)
async def transfer(body: TransferIn, request: Request) -> dict:
    """Replicate the owner's watch state onto their watching account.

    Mirrors: the target ends up matching the owner, which means un-marking anything the owner has not
    watched. That is what makes it a replica rather than a merge, and it is what repairs an account
    the pre-1.x transfer spoiled by scrobbling show keys. It is also the only path here that can
    delete watch history, so a snapshot is taken before the first write and `/undo` restores it.

    Plex stamps every write `now` and accepts no date, so the writes go oldest-first — the dates
    cannot be replicated, but the ORDER can, which is what Continue Watching sorts on.
    """
    state = request.app.state
    # Validated BEFORE anything is queued. A job that can only ever fail is worse than a 400: it
    # retries three times, writes a failure to the Jobs page, and tells the caller 502 for what is
    # really "you picked the wrong account".
    with state.sessions() as session:
        owner_id = _owner_id(session)
        target = session.get(User, body.to_user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="that watching account is not a known user")
        if target.id == owner_id:
            raise HTTPException(status_code=400, detail="cannot transfer a watch history onto the same account")
        if UserType(target.user_type) is not UserType.MANAGED:
            raise HTTPException(
                status_code=400,
                detail=(
                    "a watching account must be one of your own Plex Home users — "
                    f"{target.username!r} is a {target.user_type} account"
                ),
            )

    return await _via_job(
        state,
        "watching_account.transfer",
        {"to_user_id": body.to_user_id, "dry_run": force_dry_run() or body.dry_run},
        "watch-state replication",
    )


@router.post("/undo", response_model=TransferOut)
async def undo(body: UndoIn, request: Request) -> dict:
    """Put the watching account back exactly as the transfer found it.

    Restores from the snapshot rather than replaying the writes backwards — with counts and offsets,
    not just watched/unwatched, because re-marking a rewatched film once would leave a third state
    that existed on neither account.
    """
    state = request.app.state
    with state.sessions() as session:
        if session.get(WatchStateSnapshot, body.snapshot_id) is None:
            raise HTTPException(status_code=404, detail="no snapshot with that id — nothing to restore")

    return await _via_job(
        state,
        "watching_account.undo",
        {"snapshot_id": body.snapshot_id, "dry_run": force_dry_run() or body.dry_run},
        "watch-state undo",
    )


async def _via_job(state, kind: str, payload: dict, label: str) -> dict:
    """Queue the work, drain it now, and return the job's report.

    The queue is the SAFETY NET, not a delay — the same pattern `POST /api/system/jobs` uses. It
    matters more here than anywhere else in the app: a heavy account is ~11,000 PMS writes, which a
    reverse proxy will time out at 60s. When that happens the job keeps running, finishes, and its
    report stays readable on the Jobs page — where before, the work simply stopped half-applied with
    no record of how far it got. A failed attempt is retried with backoff rather than lost, and the
    handler is idempotent (it re-plans against a fresh read), so a retry writes only what is missing.

    Errors surface as the job's `error`, which is already redacted (rule 9) — a plex.tv failure can
    carry a tokened URL, and this is the response body.
    """
    from shortlist.server.db.models import Job
    from shortlist.server.services.jobs import enqueue, run_pending

    job_id = enqueue(state.sessions, kind, payload)
    try:
        await run_pending(state)
    except Exception as e:  # a drain failure is still a job row; report it, do not 500 blindly
        logger.warning("{} drain failed ({})", label, type(e).__name__)

    with state.sessions() as session:
        job = session.get(Job, job_id)
        if job is None:  # pragma: no cover — the row was committed a moment ago
            raise HTTPException(status_code=500, detail="the job vanished before it could be read")
        # An attempt that RAISED leaves the job queued for retry, not `failed` — `failed` means the
        # attempts are exhausted. Both must surface the error: reporting a mid-retry failure as
        # "queued, it'll finish" told the caller nothing was wrong while the cause sat in `job.error`.
        if job.error:
            raise HTTPException(status_code=502, detail=redact(job.error))
        if job.status != "done":
            # Genuinely just waiting — queued behind a run holding the Plex lock. The work is
            # committed and the worker will finish it; the caller simply has no report yet.
            raise HTTPException(
                status_code=503,
                detail="Plex is busy with another job right now — this has been queued and will finish on its own.",
            )
        return {**TransferReport().as_dict(), **(job.result or {})}
