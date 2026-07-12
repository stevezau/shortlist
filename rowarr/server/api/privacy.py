"""Privacy API: status, on-demand check (T1 + T2 when a canary exists), snapshots."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from rowarr.server.auth import require_owner
from rowarr.server.db.models import PrivacyCheck, RestrictionSnapshotRow, User, iso_utc
from rowarr.server.services.privacy_state import privacy_summary

router = APIRouter(prefix="/privacy", tags=["privacy"], dependencies=[Depends(require_owner)])


@router.get("/status")
async def status(request: Request) -> dict:
    with request.app.state.sessions() as session:
        return privacy_summary(session)


class CheckRequest(BaseModel):
    probe: bool = False  # full probe (creates/removes a throwaway collection) vs read-only T1/T2


@router.post("/check")
async def run_check(request: Request, body: CheckRequest | None = None) -> dict:
    """Run T1 (always) and T2 (when a non-PIN Home canary exists); persist results.

    With probe=true, runs the full wizard-style Privacy Probe instead (creates a throwaway
    labeled collection, verifies it hides for the canary, cleans up in finally).
    """
    state = request.app.state
    loop = asyncio.get_running_loop()
    probe_mode = bool(body and body.probe)

    def check():
        from rowarr.engine.verify import check_t1, check_t2
        from rowarr.server.services.run_service import RunService

        service: RunService = state.run_service
        ctx = service.build_context(dry_run=True)
        with state.sessions() as session:
            profiles = service.enabled_profiles(session)
        collections = ctx.plex.owned_collections(ctx.config.label_prefix)
        stored = {slug: row.label for slug, row in collections.items()}
        canary = next(
            (
                p
                for p in profiles
                for u in ctx.plextv.home_users()
                if int(u.get("id", 0)) == p.plex_account_id and not u.get("protected")
            ),
            None,
        )
        if probe_mode:
            if canary is None:
                raise RuntimeError("the Privacy Probe needs a Home user without a PIN as canary")
            from rowarr.engine.probe import run_privacy_probe

            def on_step(message: str) -> None:
                loop.call_soon_threadsafe(state.bus.publish, "privacy.probe.step", {"message": message})

            # ctx.snapshots is the DB-backed store: the canary's pre-probe filters are
            # persisted BEFORE the probe touches their share (plex-safety rule 2).
            return [run_privacy_probe(ctx.plex, ctx.plextv, canary, ctx.snapshots, on_step=on_step)]
        results = [check_t1(ctx.plextv, ctx.known_slugs, stored)]
        if canary is not None:
            try:
                results.append(check_t2(ctx.plex, ctx.plextv, canary, collections))
            except Exception:
                logger.exception("T2 check failed to execute")
        return results

    try:
        results = await asyncio.get_running_loop().run_in_executor(None, check)
    except RuntimeError as e:
        # e.g. "the Privacy Probe needs a Home user without a PIN as canary" — that's a setup
        # problem the owner can fix, not a server fault. Say so plainly instead of a raw 500.
        raise HTTPException(status_code=409, detail=str(e)) from e
    with state.sessions() as session:
        for result in results:
            session.add(PrivacyCheck(tier=result.tier, passed=result.passed, detail=result.detail))
        session.commit()
    state.bus.publish("privacy.status", {"passed": all(r.passed for r in results)})
    return {
        "passed": all(r.passed for r in results),
        "tiers": {r.tier: r.passed for r in results},
        # A failing privacy check is the one result an owner must be able to act on: which row
        # is visible to whom. Returning a bare `false` makes them go digging in the database.
        "detail": {r.tier: r.detail for r in results if not r.passed},
    }


@router.get("/snapshots")
async def snapshots(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        rows = session.query(RestrictionSnapshotRow).order_by(RestrictionSnapshotRow.id.desc()).limit(100).all()
        users = {u.id: u.username for u in session.query(User).all()}
        return [
            {
                "id": row.id,
                "username": users.get(row.user_id, "?"),
                "taken_at": iso_utc(row.taken_at),
                "reason": row.reason,
                "filters_before": row.filters_before,
            }
            for row in rows
        ]
