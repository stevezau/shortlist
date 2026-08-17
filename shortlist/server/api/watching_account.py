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

from shortlist.engine.clients.http_retry import redact
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import User
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.services.audit import add_audit
from shortlist.server.services.watching_account import candidate_home_users, transfer_watch_history

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
    # Also mark every title played on the PMS as the target account. Plex stamps a scrobble `now`
    # and cannot backdate it, so this gives checkmarks but not dates — the true dates are kept in
    # `watched_titles.source_viewed_at` either way.
    scrobble: bool = False
    dry_run: bool = False


class TransferOut(PassthroughModel):
    copied: int
    already_present: int
    scrobbled: int
    scrobble_skipped: int
    dry_run: bool
    # Nothing had ever been cached for the owner, so there was nothing to copy — told apart from a
    # plain `copied == 0` because the UI has to say something completely different about it (#88).
    source_empty: bool
    errors: list[str]


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


@router.post("/transfer", response_model=TransferOut)
async def transfer(body: TransferIn, request: Request) -> dict:
    """Copy the owner's watched set onto their watching account.

    The copy into Shortlist is what makes the new account's picks correct and writes nothing to
    Plex. `scrobble` additionally marks each title played on the PMS so Plex shows checkmarks —
    thousands of writes, and Plex dates every one of them today (it cannot be told otherwise), which
    is why the true dates are stored separately.
    """
    state = request.app.state

    def do():
        # Safe mode gates BOTH halves, not just the Plex one. `build_context` is the usual
        # chokepoint, but the plain copy deliberately never builds a context — and it is still an
        # irreversible write into another person's watched set, with no undo in the UI.
        dry_run = force_dry_run() or body.dry_run
        plex = token = None
        if body.scrobble:
            ctx = state.run_service.build_context(dry_run=dry_run, plex_only=True)
            dry_run = ctx.config.dry_run
            with state.sessions() as session:
                target = session.get(User, body.to_user_id)
                if target is None:
                    raise LookupError("that watching account is not a known user")
                account_id = target.plex_account_id
            plex = ctx.plex
            token = ctx.plextv.canary_server_token(account_id)
        with state.sessions() as session:
            owner_id = _owner_id(session)
            report = transfer_watch_history(
                session,
                from_user_id=owner_id,
                to_user_id=body.to_user_id,
                plex=plex,
                target_token=token or "",
                scrobble=body.scrobble,
                dry_run=dry_run,
            )
            # `scrobble` and `from_user_id` are recorded as INTENT, not just outcome: a transfer that asked
            # for checkmarks and got zero is otherwise indistinguishable from one that never asked (rule 10).
            add_audit(
                session,
                "watching_account.transfer",
                "info",
                from_user_id=owner_id,
                to_user_id=body.to_user_id,
                scrobble=body.scrobble,
                **report.as_dict(),
            )
            session.commit()
            return report.as_dict()

    try:
        return await asyncio.get_running_loop().run_in_executor(None, do)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=400, detail=redact(str(e))) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("watch-history transfer failed ({})", type(e).__name__)
        raise HTTPException(status_code=502, detail=redact(f"{type(e).__name__}: {e}")) from e
