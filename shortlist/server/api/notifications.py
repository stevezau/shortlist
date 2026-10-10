"""Notifications API: the owner's current alerts, dismissing them, and the post-upgrade release notes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import shortlist
from shortlist.server.auth import require_owner
from shortlist.server.notifications import DISMISSED_KEY, build_notifications
from shortlist.server.schema_base import PassthroughModel
from shortlist.server.services.audit import Level
from shortlist.server.settings_store import SettingsStore
from shortlist.server.whats_new import mark_seen, pending

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
    #: Every id the owner has dismissed. Returned because dismissal is not only the bell's business:
    #: the owner-shelf warning renders as an inline note on the Users page as well, and both surfaces
    #: report the SAME fact. Without this the note has no way to know it was already acknowledged, and
    #: the owner would have to dismiss the same thing in two places.
    dismissed: list[str]


@router.get("", response_model=NotificationsOut)
async def list_notifications(request: Request) -> dict:
    """Every currently-firing notification (update available, failed/partial run, paused, errors),
    plus the ids already dismissed so inline surfaces reporting the same fact can hide themselves."""
    with request.app.state.sessions() as session:
        store = SettingsStore(session)
        items = build_notifications(session, store, shortlist.__version__)
        dismissed = list(store.get(DISMISSED_KEY) or [])
    return {"notifications": items, "dismissed": dismissed}


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


class ReleaseNotesOut(PassthroughModel):
    version: str
    url: str  # the release on GitHub
    published_at: str
    notes: str  # the release body, markdown — rendered by the dialog, which allows no raw HTML


class WhatsNewOut(PassthroughModel):
    #: The running build. Not necessarily ``releases[0]``: that release may not be published yet.
    version: str
    #: Every release after the last one the owner closed, newest first. Empty: nothing to show.
    releases: list[ReleaseNotesOut]


# Plain `def`, not `async def`: an unread release asks GitHub, and a blocking read inside a coroutine
# would stall every other request for as long as GitHub takes to answer.
@router.get("/whats-new", response_model=WhatsNewOut)
def whats_new(request: Request) -> dict:
    """The release notes the owner has not read since upgrading, for the What's new dialog."""
    with request.app.state.sessions() as session:
        releases = pending(SettingsStore(session), shortlist.__version__)
    return {"version": shortlist.__version__, "releases": releases}


class SeenRelease(BaseModel):
    version: str


@router.post("/whats-new/seen", response_model=DismissedOut)
def whats_new_seen(body: SeenRelease, request: Request) -> dict:
    """Close the What's new dialog for good: record the version whose notes it showed."""
    with request.app.state.sessions() as session:
        try:
            mark_seen(SettingsStore(session), body.version, shortlist.__version__)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        session.commit()
    return {"ok": True}
