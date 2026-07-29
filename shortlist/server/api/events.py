"""Events API: live SSE stream + the structured audit feed (the 'what changed at 03:31' answer)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from starlette.responses import StreamingResponse

from shortlist.server.auth import require_owner
from shortlist.server.db.models import Event, iso_utc

router = APIRouter(prefix="/events", tags=["events"], dependencies=[Depends(require_owner)])


@router.get("")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(
        request.app.state.bus.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/log")
async def audit_log(
    request: Request,
    scope: str | None = None,
    limit: int = 200,
    before_id: int | None = None,
) -> list[dict]:
    """The audit trail, newest first. `before_id` pages backwards — pass the id of the oldest entry
    you already have. A cursor, not an offset: events are appended while you read."""
    with request.app.state.sessions() as session:
        query = session.query(Event).order_by(Event.id.desc())
        if scope:
            query = query.filter(Event.scope == scope)
        if before_id is not None:
            query = query.filter(Event.id < before_id)
        return [
            {"id": e.id, "ts": iso_utc(e.ts), "level": e.level, "scope": e.scope, "message": e.message}
            for e in query.limit(min(limit, 1000)).all()
        ]
