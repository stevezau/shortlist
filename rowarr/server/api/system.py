"""System API: health (unauthenticated, for Docker HEALTHCHECK), version, full uninstall."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

import rowarr
from rowarr.server.auth import require_owner
from rowarr.server.db.models import Event, RestrictionSnapshotRow, User

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": rowarr.__version__}


@router.get("/version", dependencies=[Depends(require_owner)])
async def version() -> dict:
    return {"version": rowarr.__version__}


class UninstallRequest(BaseModel):
    confirm: str = ""
    dry_run: bool = False  # preview: report what WOULD be restored/deleted (rule 8)


@router.post("/uninstall", dependencies=[Depends(require_owner)])
async def uninstall(body: UninstallRequest, request: Request) -> dict:
    """Trust feature: restore every snapshot, delete every rowarr collection, report.

    dry_run=true previews the plan; the real thing requires the literal confirmation
    string UNINSTALL — this is the one deliberately scary button in the product.
    """
    if not body.dry_run and body.confirm != "UNINSTALL":
        raise HTTPException(status_code=422, detail='type "UNINSTALL" to confirm')
    state = request.app.state

    def do_uninstall() -> tuple[dict, list[dict]]:
        from rowarr.engine.models import FilterSnapshot
        from rowarr.engine.privacy import restore_user_restrictions

        service = state.run_service
        ctx = service.build_context(dry_run=body.dry_run)
        per_user_events: list[dict] = []
        restored = 0
        with state.sessions() as session:
            users = {u.id: u for u in session.query(User).all()}
            for row in session.query(RestrictionSnapshotRow).filter_by(reason="initial").all():
                user = users.get(row.user_id)
                if user is None:
                    continue
                snapshot = FilterSnapshot(
                    plex_account_id=user.plex_account_id,
                    username=user.username,
                    taken_at=row.taken_at,
                    filters=row.filters_before,
                )
                if restore_user_restrictions(ctx.plextv, snapshot, dry_run=body.dry_run):
                    restored += 1
                    per_user_events.append(
                        {"user": user.username, "restored_to": row.filters_before, "dry_run": body.dry_run}
                    )
        deleted = []
        for section in ctx.plex.sections():
            for collection in section.collections():
                if any(label.tag.lower().startswith("rowarr_") for label in collection.labels):
                    deleted.append(collection.title)
                    if not body.dry_run:
                        ctx.plex.delete_owned_collection(collection, "rowarr")
        return {"filters_restored": restored, "collections_deleted": deleted, "dry_run": body.dry_run}, per_user_events

    result, per_user = await asyncio.get_running_loop().run_in_executor(None, do_uninstall)
    with state.sessions() as session:
        for entry in per_user:
            session.add(Event(scope="uninstall.user", level="warn", message=entry))
        session.add(
            Event(scope="system.uninstall", level="warn", message={**result, "at": datetime.now(UTC).isoformat()})
        )
        session.commit()
    logger.warning("UNINSTALL {}: {}", "preview" if body.dry_run else "executed", result)
    message = "Preview only — nothing was changed." if body.dry_run else "Your server is as we found it."
    return {**result, "message": message}
