"""Notifications API: the owner's current alerts, and dismissing the "update available" note."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

import shortlist
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.notifications import DISMISSED_KEY, build_notifications
from shortlist.server.services.audit import Level
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/notifications", tags=["notifications"], dependencies=[Depends(require_owner)])


class NotificationOut(PassthroughModel):
    """One alert as the React bell renders it — plain text throughout, no HTML.

    ``extra="allow"`` (here and on the wrapper) is not optional: a strict Pydantic response model
    silently DROPS any key it does not declare, so a field missed here would vanish from the payload
    instead of failing loudly.
    """

    id: str  # encodes the state it reports (run id, version), so a NEW occurrence re-surfaces
    #: `audit.Level`, not a Literal repeated here: the audit levels and these severities are the same
    #: three words on purpose (see that module), and two copies could disagree.
    severity: Level
    title: str
    body: str
    action_url: str
    action_label: str
    dismissable: bool  # enforced on READ as well as on dismiss — see build_notifications


class NotificationsOut(PassthroughModel):
    notifications: list[NotificationOut]


@router.get("", response_model=NotificationsOut)
async def list_notifications(request: Request) -> dict:
    """Every currently-firing notification (update available, failed/partial run, paused, errors)."""
    with request.app.state.sessions() as session:
        items = build_notifications(session, SettingsStore(session), shortlist.__version__)
    return {"notifications": items}


class Dismiss(BaseModel):
    id: str


class DismissedOut(PassthroughModel):
    ok: bool


@router.post("/dismiss", response_model=DismissedOut)
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
