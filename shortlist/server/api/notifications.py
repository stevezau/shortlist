"""Notifications API: the owner's current alerts, and dismissing the "update available" note."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

import shortlist
from shortlist.server.auth import require_owner
from shortlist.server.notifications import DISMISSED_KEY, build_notifications
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/notifications", tags=["notifications"], dependencies=[Depends(require_owner)])


@router.get("")
async def list_notifications(request: Request) -> dict:
    """Every currently-firing notification (update available, failed/partial run, paused, errors)."""
    with request.app.state.sessions() as session:
        items = build_notifications(session, SettingsStore(session), shortlist.__version__)
    return {"notifications": items}


class Dismiss(BaseModel):
    id: str


@router.post("/dismiss")
async def dismiss(body: Dismiss, request: Request) -> dict:
    """Hide a notification by id. The id encodes its state (run id / version), so the SAME condition
    stays hidden but a new failure or a newer release surfaces again on its own."""
    with request.app.state.sessions() as session:
        store = SettingsStore(session)
        current = list(store.get(DISMISSED_KEY) or [])
        if body.id not in current:
            # Cap the list so a long-lived install can't grow it unbounded (keep the newest 100).
            store.set(DISMISSED_KEY, [*current, body.id][-100:])
            session.commit()
    return {"ok": True}
