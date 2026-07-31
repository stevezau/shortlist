"""Events API: live SSE stream + the structured audit feed (the 'what changed at 03:31' answer)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from starlette.responses import StreamingResponse

from shortlist.server.api.schemas_events import (
    RunFinishedEvent,
    RunProgressEvent,
    SyncFinishedEvent,
    SyncProgressEvent,
    UninstallProgressEvent,
)
from shortlist.server.api.schemas_runs import RunLogLineOut
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Event, iso_utc

router = APIRouter(prefix="/events", tags=["events"], dependencies=[Depends(require_owner)])

#: The payload of one SSE frame's `data:`, by event name — see `api/schemas_events.py`. Declared as a
#: union under the route's `responses` because that is the only hook FastAPI gives for putting a
#: schema into `components.schemas` when no handler returns it: an endless `text/event-stream` has no
#: response model, so without this the SPA is left hand-writing all five shapes.
#:
#: It documents; it never validates. The handler returns a `StreamingResponse`, which FastAPI hands
#: back untouched, so nothing here can reject a frame.
SseEventPayload = (
    RunLogLineOut | RunProgressEvent | RunFinishedEvent | UninstallProgressEvent | SyncProgressEvent | SyncFinishedEvent
)


class _EventStreamResponse(StreamingResponse):
    """Never instantiated — it exists so the OpenAPI response is labelled `text/event-stream`.

    FastAPI takes the media type of a documented response from the route's `response_class`, and
    `StreamingResponse` declares none, so `SseEventPayload` would otherwise be published under
    `application/json` — a lie about an endpoint that never sends one. The handler builds its own
    `StreamingResponse` and FastAPI returns a `Response` untouched, so this only affects the schema.
    """

    media_type = "text/event-stream"


@router.get(
    "",
    response_class=_EventStreamResponse,
    responses={
        200: {
            "description": (
                "An endless `text/event-stream`. Each frame names its event (`run.user.stage`, "
                "`run.progress`, `run.finished`, `uninstall.progress`, `sync.progress`, "
                "`sync.finished`) and carries one of these payloads as JSON in `data:`."
            ),
            "model": SseEventPayload,
        }
    },
)
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
    limit: int = Query(200, ge=1, le=1000),
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
