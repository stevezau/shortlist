"""Tools — on-demand maintenance actions the owner runs by hand.

These are deliberate "repair / reconcile now" operations, distinct from the nightly schedule: run
them when something has drifted (a roster changed, watched state is out of sync) rather than waiting
for the next scheduled run. Every action here is owner-only and read-mostly — none of them writes to
Plex or plex.tv.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from shortlist.server.auth import require_owner

router = APIRouter(prefix="/tools", tags=["tools"], dependencies=[Depends(require_owner)])


@router.post("/reconcile-watched")
async def reconcile_watched(request: Request) -> dict:
    """Fill every enabled user's watch history from Plex's own database — the only source that sees a
    mark-as-watched (the history API returns plays only). Reads the database read-only, writes only
    our own ``watch_events``, never touches Plex.

    Returns ``{configured, users, added}``: ``configured`` is False when no Plex database is mounted
    (so the UI can say "mount it first" rather than "nothing to add"), ``added`` is how many watched
    events this reconcile discovered that the play history had never seen.
    """
    return await request.app.state.run_service.reconcile_watched_from_db()
