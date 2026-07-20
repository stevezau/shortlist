"""The Sonarr/Radarr approval inbox: list wanted-but-missing titles, send the chosen ones, reject the rest.

A request asks a download app for a file — it touches no Plex object. It is gated only on the owner
session and on requests being configured. Sending runs in a
worker thread (the Arr/TMDB clients are sync) and respects ``dry_run``.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shortlist.engine.models import MediaType, MissingTitle
from shortlist.engine.requests import request_titles
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Event, RequestCandidate, iso_utc

router = APIRouter(prefix="/requests", tags=["requests"], dependencies=[Depends(require_owner)])

# Pending first (the owner's to-do list), then sent, then rejected — so the inbox opens on what needs a decision.
_STATUS_ORDER = {"pending": 0, "sent": 1, "rejected": 2}


class RequestWhyOut(BaseModel):
    user: str  # whose taste surfaced it
    row: str  # the row that wanted it (the name the user sees)
    seed: str  # the history title behind it ("because you watched …"); "" for seedless sources
    source: str  # the candidate source that produced it


class RequestCandidateOut(BaseModel):
    id: int
    tmdb_id: int
    media_type: str
    title: str
    year: int | None
    imdb_id: str = ""  # "tt…" for a direct IMDb link; "" -> the UI falls back to an IMDb search
    rating: float
    vote_count: int
    demand: int
    tags: list[str]
    wanters: list[str]
    why: list[RequestWhyOut]  # per (person, row) provenance — which row, and why it got here
    status: str
    detail: str
    excluded: bool = False  # on a Sonarr/Radarr exclusion list — the inbox warns approving is a no-op
    arr_slug: str | None = None  # the arr titleSlug -> the sent log deep-links straight to its page
    updated_at: str | None  # when this row last changed state (the "sent at" for a sent item)


class RequestAction(BaseModel):
    ids: list[int]
    dry_run: bool = False


@router.get("")
def list_requests(request: Request) -> list[RequestCandidateOut]:
    """The whole inbox: pending first (most-wanted, best-rated on top), then sent, then rejected.

    Rows the owner cleared from the Sent log (``hidden``) are excluded — they stay in the DB as sent
    tombstones (so the title isn't re-requested) but never show in the UI again.
    """
    with request.app.state.sessions() as session:
        rows = session.query(RequestCandidate).filter(~RequestCandidate.hidden).all()
    rows.sort(key=lambda r: (_STATUS_ORDER.get(r.status, 9), -r.demand, -r.rating))
    return [
        RequestCandidateOut(
            id=r.id,
            tmdb_id=r.tmdb_id,
            media_type=r.media_type,
            title=r.title,
            year=r.year,
            imdb_id=r.imdb_id or "",
            rating=r.rating,
            vote_count=r.vote_count,
            demand=r.demand,
            tags=list(r.tags or []),
            wanters=list(r.wanters or []),
            why=[RequestWhyOut(**w) for w in (r.why or [])],
            status=r.status,
            detail=r.detail,
            excluded=bool(r.excluded),
            arr_slug=r.arr_slug,
            updated_at=iso_utc(r.updated_at),
        )
        for r in rows
    ]


@router.post("/reject")
def reject_requests(body: RequestAction, request: Request) -> dict:
    """Permanently dismiss the given titles.

    A rejected title is kept on file as a tombstone: it leaves the pending list AND every later run
    skips re-queuing it (``_persist_request_queue`` only touches ``pending`` rows), so a dismissed
    suggestion can never come back on its own. Use ``/delete`` instead to remove a title without
    blocking it — or to lift a rejection so a future run may surface it again.
    """
    with request.app.state.sessions() as session:
        rows = session.query(RequestCandidate).filter(RequestCandidate.id.in_(body.ids)).all()
        for row in rows:
            row.status = "rejected"
        session.add(Event(scope="requests.reject", level="info", message={"ids": body.ids, "count": len(rows)}))
        session.commit()
    return {"rejected": len(rows)}


@router.post("/restore")
def restore_requests(body: RequestAction, request: Request) -> dict:
    """Un-reject: move rejected titles back to the pending queue (Waiting) so they can be sent again.

    Only ``rejected`` rows are restored; ``pending``/``sent`` are left as they are. The row keeps its
    recorded demand/wanters/why/tags, so it reappears in Waiting exactly as it was, ready to send —
    unlike a run, which would only re-surface it if the same taste turned it up again.
    """
    with request.app.state.sessions() as session:
        rows = (
            session.query(RequestCandidate)
            .filter(RequestCandidate.id.in_(body.ids), RequestCandidate.status == "rejected")
            .all()
        )
        for row in rows:
            row.status = "pending"
        session.add(Event(scope="requests.restore", level="info", message={"ids": body.ids, "count": len(rows)}))
        session.commit()
    return {"restored": len(rows)}


@router.post("/delete")
def delete_requests(body: RequestAction, request: Request) -> dict:
    """Remove the given titles from the inbox entirely, leaving no trace.

    Unlike ``/reject`` (a permanent tombstone), a deleted row is gone — so if a later run's picks turn
    up the same title again, it returns to the pending queue. Two uses: clear a title off the list
    without blocking it forever, or delete a previously *rejected* title to let it come back.

    ``sent`` rows are never deleted: that status is a load-bearing tombstone (``_persist_request_queue``)
    that stops a still-downloading title from being seen as "missing" and re-requested every night.
    Dropping it would resurrect that bug, so a ``sent`` id in the request is skipped, not deleted.
    """
    with request.app.state.sessions() as session:
        rows = (
            session.query(RequestCandidate)
            .filter(RequestCandidate.id.in_(body.ids), RequestCandidate.status != "sent")
            .all()
        )
        count = len(rows)
        for row in rows:
            session.delete(row)
        session.add(Event(scope="requests.delete", level="info", message={"ids": body.ids, "count": count}))
        session.commit()
    return {"deleted": count}


@router.post("/clear")
def clear_requests(body: RequestAction, request: Request) -> dict:
    """Clear the given SENT titles from the send log — hide them, don't delete them.

    A sent row is a load-bearing tombstone: dropping it lets a still-downloading title look "missing"
    and get re-requested every night (see ``delete_requests``). So "clear" sets ``hidden`` instead —
    the row stays ``sent`` and keeps protecting against re-request, but never shows in the inbox again.
    Only ``sent`` rows are cleared; a pending/rejected id is ignored (those have Delete / Reject).
    """
    with request.app.state.sessions() as session:
        rows = (
            session.query(RequestCandidate)
            .filter(RequestCandidate.id.in_(body.ids), RequestCandidate.status == "sent")
            .all()
        )
        count = 0
        for row in rows:
            if not row.hidden:
                row.hidden = True
                count += 1
        session.add(Event(scope="requests.clear", level="info", message={"ids": body.ids, "count": count}))
        session.commit()
    return {"cleared": count}


@router.post("/send")
async def send_requests(body: RequestAction, request: Request) -> dict:
    """Ask Sonarr/Radarr for the chosen pending titles.

    A dry run previews the outcomes without asking and leaves every row pending. A real send marks a
    row ``sent`` only when the app accepted it; a skip/error leaves it pending with the reason recorded,
    so the owner can see why it didn't go and try again.
    """
    state = request.app.state
    svc = state.run_service

    def _send() -> dict:
        cfg, tmdb = svc.build_requests_context()
        if cfg is None:
            raise HTTPException(status_code=409, detail="Turn on Sonarr/Radarr requests in Settings first.")
        with state.sessions() as session:
            rows = (
                session.query(RequestCandidate)
                .filter(RequestCandidate.id.in_(body.ids), RequestCandidate.status == "pending")
                .all()
            )
            titles = [
                MissingTitle(
                    tmdb_id=row.tmdb_id,
                    title=row.title,
                    media_type=MediaType(row.media_type),
                    year=row.year,
                    rating=row.rating,
                    vote_count=row.vote_count,
                    demand=row.demand,
                    tags=set(row.tags or []),
                )
                for row in rows
            ]
            report = request_titles(cfg, tmdb, titles, dry_run=body.dry_run)
            by_key = {(o.tmdb_id, o.media_type.value): o for o in report.outcomes}
            outcomes = []
            for row in rows:
                outcome = by_key.get((row.tmdb_id, row.media_type))
                if outcome is None:
                    continue
                row.detail = outcome.detail
                if outcome.arr_slug:
                    row.arr_slug = outcome.arr_slug  # so the sent log deep-links to the arr page
                if not body.dry_run and outcome.status == "requested":
                    row.status = "sent"
                outcomes.append({"id": row.id, "title": row.title, "status": outcome.status, "detail": outcome.detail})
            session.add(
                Event(scope="requests.send", level="info", message={"dry_run": body.dry_run, "outcomes": outcomes})
            )
            session.commit()
            sent = sum(1 for o in outcomes if o["status"] in ("requested", "would_request"))
            return {"sent": sent, "dry_run": body.dry_run, "outcomes": outcomes}

    return await asyncio.get_running_loop().run_in_executor(None, _send)
