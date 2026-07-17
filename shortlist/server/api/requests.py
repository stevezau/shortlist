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
    updated_at: str | None  # when this row last changed state (the "sent at" for a sent item)


class RequestAction(BaseModel):
    ids: list[int]
    dry_run: bool = False


@router.get("")
def list_requests(request: Request) -> list[RequestCandidateOut]:
    """The whole inbox: pending first (most-wanted, best-rated on top), then sent, then rejected."""
    with request.app.state.sessions() as session:
        rows = session.query(RequestCandidate).all()
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
            updated_at=iso_utc(r.updated_at),
        )
        for r in rows
    ]


@router.post("/reject")
def reject_requests(body: RequestAction, request: Request) -> dict:
    """Dismiss the given titles: they leave the pending list and later runs never re-queue them."""
    with request.app.state.sessions() as session:
        rows = session.query(RequestCandidate).filter(RequestCandidate.id.in_(body.ids)).all()
        for row in rows:
            row.status = "rejected"
        session.add(Event(scope="requests.reject", level="info", message={"ids": body.ids, "count": len(rows)}))
        session.commit()
    return {"rejected": len(rows)}


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
