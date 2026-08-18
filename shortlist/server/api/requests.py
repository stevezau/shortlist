"""The Sonarr/Radarr approval inbox: list wanted-but-missing titles, send the chosen ones, reject the rest.

A request asks a download app for a file — it touches no Plex object. It is gated only on the owner
session and on requests being configured. Sending runs in a
worker thread (the Arr/TMDB clients are sync) and respects ``dry_run``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field

from shortlist.engine.models import MediaType, MissingTitle
from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.requests import request_titles_by_row
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, Event, RequestCandidate, iso_utc
from shortlist.server.services.context_builder import row_request_overrides

router = APIRouter(prefix="/requests", tags=["requests"], dependencies=[Depends(require_owner)])

# Pending first (the owner's to-do list), then sent, then rejected — so the inbox opens on what needs a decision.
_STATUS_ORDER = {"pending": 0, "sent": 1, "rejected": 2}


class RequestWhyOut(PassthroughModel):
    user: str  # whose taste surfaced it
    row: str  # the row that wanted it (the name the user sees)
    seed: str  # the history title behind it ("because you watched …"); "" for seedless sources
    source: str  # the candidate source that produced it


class RequestCandidateOut(PassthroughModel):
    id: int
    tmdb_id: int
    media_type: str
    title: str
    year: int | None
    imdb_id: str = ""  # "tt…" for a direct IMDb link; "" -> the UI falls back to an IMDb search
    # TMDB poster path ("/abc.jpg"). The UI builds the image URL and its size; "" -> placeholder tile.
    poster_path: str = ""
    # TMDB's synopsis, so an unfamiliar title can be judged in the inbox; "" -> no paragraph is drawn.
    overview: str = ""
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
    # Live Arr download status is fetched separately via GET /requests/status (one round-trip for the
    # whole inbox) and merged in the UI — it is NOT carried on the list payload, which would force an
    # Arr call per row on every list fetch.


class RequestAction(BaseModel):
    #: Bounded because every handler feeds this straight into `.in_()`. SQLite's compiled parameter
    #: ceiling (SQLITE_MAX_VARIABLE_NUMBER, 999 on older builds) turns an over-long list into an
    #: OperationalError — a 500 with a SQL string in it — rather than a refusal the caller can read.
    ids: list[int] = Field(max_length=1000)
    dry_run: bool = False


#: Hard ceiling on one inbox read. The sent log only grows — every run that wants a title the library
#: lacks adds a row — so an unbounded read is a query that gets slower for ever and eventually times
#: out the page. Pending is what the owner acts on and is self-limiting (you clear it); the tail is
#: history, and the sort puts pending first, so a cap can only ever truncate the oldest history.
MAX_INBOX = 500


@router.get("")
def list_requests(
    request: Request,
    wanted_by: Annotated[
        list[str] | None,
        Query(description="Only titles at least one of these people wanted (the `wanters` usernames)."),
    ] = None,
) -> list[RequestCandidateOut]:
    """The whole inbox: pending first (most-wanted, best-rated on top), then sent, then rejected.

    Rows the owner cleared from the Sent log (``hidden``) are excluded — they stay in the DB as sent
    tombstones (so the title isn't re-requested) but never show in the UI again.

    Args:
        request: The FastAPI request, for the session factory.
        wanted_by: Repeated query parameter (``?wanted_by=sarah&wanted_by=mike``) naming the people
            whose titles to keep — matched against ``wanters``, which holds bare Plex usernames.
            A title is kept if ANY of the named people wanted it (union, not intersection), matching
            what the inbox's "Wanted by" chips mean. Omitted (or empty) means everyone, which is the
            unfiltered inbox — no caller that leaves it off sees any change.

    Returns:
        The matching rows, capped at :data:`MAX_INBOX`.

    The cap is applied AFTER the status sort, in Python, because the ordering is by
    (status, demand, rating) and a SQL LIMIT before that sort would cut arbitrary rows rather than
    the tail of the history. ``wanted_by`` is applied BEFORE the cap — a filter applied to the capped
    page could only ever search the 500 rows the cap left, and "what does this new person still
    need?" is precisely the question that wants everything on file for one person.

    The name filter runs in Python rather than SQL: the read below is already unbounded (`.all()`
    over every non-hidden row — the cap bounds the PAYLOAD, not the query), so filtering the rows
    already in memory costs nothing extra, and it avoids depending on SQLite's JSON1 `json_each` to
    ask whether a JSON array column contains a value.
    """
    wanted = {name for name in (wanted_by or []) if name}
    with request.app.state.sessions() as session:
        rows = session.query(RequestCandidate).filter(~RequestCandidate.hidden).all()
    if wanted:
        rows = [r for r in rows if wanted.intersection(r.wanters or ())]
    rows.sort(key=lambda r: (_STATUS_ORDER.get(r.status, 9), -r.demand, -r.rating))
    rows = rows[:MAX_INBOX]
    return [
        RequestCandidateOut(
            id=r.id,
            tmdb_id=r.tmdb_id,
            media_type=r.media_type,
            title=r.title,
            year=r.year,
            imdb_id=r.imdb_id or "",
            poster_path=r.poster_path or "",
            overview=r.overview or "",
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


class RejectedOut(PassthroughModel):
    """How many rows the action actually touched — not how many ids were sent. Each of the four
    inbox actions skips the statuses it does not own, so the count is the only honest receipt.

    ``extra="allow"`` is on every response model here (and every nested one): a strict model would
    silently DROP any key the handler returns but the model has not declared, so a field missed
    here would vanish from the payload rather than fail loudly.
    """

    rejected: int


@router.post("/reject", response_model=RejectedOut)
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


class RestoredOut(PassthroughModel):
    restored: int


@router.post("/restore", response_model=RestoredOut)
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


class DeletedOut(PassthroughModel):
    deleted: int


@router.post("/delete", response_model=DeletedOut)
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


class ClearedOut(PassthroughModel):
    cleared: int


@router.post("/clear", response_model=ClearedOut)
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


#: Whether an Arr answered this fetch. "off" means it isn't configured at all — a distinct thing
#: from a configured app that could not be reached, and the UI has to say which.
ArrReach = Literal["ok", "unreachable", "off"]


class ArrStatusOut(PassthroughModel):
    """Per-row download status, plus whether each app actually answered.

    ``reach`` is the half this used to omit. A failed Arr lookup is swallowed on purpose (one app
    being down must not blank the other), so an unreachable Radarr produced an all-``null`` map —
    byte-identical to "Radarr is fine and tracks none of these". The inbox therefore showed no
    badges, for ever, with nothing anywhere saying why.
    """

    statuses: dict[int, str | None]
    radarr: ArrReach
    sonarr: ArrReach


@router.get("/status", response_model=ArrStatusOut)
async def get_arr_status(request: Request) -> dict:
    """Arr download status for every request row, keyed by request id, plus per-app reachability.

    Covers waiting rows as well as sent ones. A waiting title is normally absent from the Arrs — the
    nightly pass drops anything they already track — so a status there means the owner (or another
    tool) added it by hand since, which is exactly the case where "why is this still waiting?" needs
    an answer. Rejected rows are skipped: nothing is going to happen to them.

    Whole-library maps, not per-title lookups, so the cost is a handful of calls no matter how long
    the inbox is — which is what makes it cheap enough for the inbox to poll. Runs in an executor
    since the Arr clients are sync. A title neither app tracks appears as None.
    """
    state = request.app.state
    svc = state.run_service

    def _fetch_statuses() -> dict:
        cfg, tmdb = svc.build_requests_context()
        if cfg is None:
            return {"statuses": {}, "radarr": "off", "sonarr": "off"}

        from shortlist.engine.clients.arr import RadarrClient, SonarrClient

        with state.sessions() as session:
            rows = session.query(RequestCandidate).filter(RequestCandidate.status.in_(("pending", "sent"))).all()

        # One fetch per app up front. A failure here is not fatal: the inbox simply shows no status
        # rather than erroring, which is what it did before this endpoint existed — but it is now
        # REPORTED, so "no badges" can be told apart from "nothing to badge".
        movies: dict[int, str] = {}
        shows_by_tvdb: dict[int, str] = {}
        shows_by_tmdb: dict[int, str] = {}
        radarr_reach: ArrReach = "off"
        sonarr_reach: ArrReach = "off"
        if cfg.radarr:
            radarr_reach = "ok"
            try:
                movies = RadarrClient(cfg.radarr).status_by_tmdb()
            except Exception as e:
                radarr_reach = "unreachable"
                logger.warning("request status: Radarr lookup failed ({})", e)
        if cfg.sonarr:
            sonarr_reach = "ok"
            try:
                shows_by_tvdb, shows_by_tmdb = SonarrClient(cfg.sonarr).status_by_ids()
            except Exception as e:
                sonarr_reach = "unreachable"
                logger.warning("request status: Sonarr lookup failed ({})", e)

        statuses: dict[int, str | None] = {}
        for row in rows:
            if row.media_type == "movie":
                statuses[row.id] = movies.get(row.tmdb_id)
                continue
            # Sonarr v4 carries tmdbId on every series, so the map answers directly. On v3 it doesn't,
            # and only then is a per-title TMDB→TVDB lookup worth paying for (cached in the client).
            status = shows_by_tmdb.get(row.tmdb_id)
            if status is None and shows_by_tvdb and not shows_by_tmdb:
                try:
                    tvdb_id = tmdb.external_ids(row.tmdb_id, MediaType.SHOW).get("tvdb_id")
                # Deliberately NOT a bare `except Exception`: this used to pass `MediaType.TV`, which
                # does not exist, and the AttributeError was swallowed to a debug line — so on Sonarr
                # v3 the fallback silently no-op'd for ever and every show showed a blank status. Only
                # a transport failure or the TMDB client's own HTTP error is tolerable here; anything
                # else is a bug and must be loud.
                except (httpx.HTTPError, RuntimeError) as e:
                    logger.debug("request status: tvdb lookup for {!r} failed ({})", row.title, e)
                    tvdb_id = None
                status = shows_by_tvdb.get(tvdb_id) if tvdb_id else None
            statuses[row.id] = status

        return {"statuses": statuses, "radarr": radarr_reach, "sonarr": sonarr_reach}

    return await asyncio.get_running_loop().run_in_executor(None, _fetch_statuses)


class SendOutcomeOut(PassthroughModel):
    """What the Arr said about one title. `status` is the engine's outcome — "requested",
    "would_request" on a dry run, or a skip/error reason the owner can act on."""

    id: int
    title: str
    status: str
    detail: str


class SendOut(PassthroughModel):
    sent: int  # counts "would_request" too, so a dry run still reports what it would have done
    dry_run: bool
    outcomes: list[SendOutcomeOut]


@router.post("/send", response_model=SendOut)
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

            def _title(row: RequestCandidate) -> MissingTitle:
                return MissingTitle(
                    tmdb_id=row.tmdb_id,
                    title=row.title,
                    media_type=MediaType(row.media_type),
                    year=row.year,
                    rating=row.rating,
                    vote_count=row.vote_count,
                    demand=row.demand,
                    tags=set(row.tags or []),
                )

            # Send each title under the target of the ROW that surfaced it — the whole point of
            # per-row settings is that a kids row files into a different folder, and an approval is
            # just a delayed send. Grouped so rows sharing a target share one client and its rate
            # limiter (the plex-safety throttle lives on the client).
            #
            # `row_slug` is NULL on everything queued before per-row settings existed, and on a title
            # whose row has since been deleted; both fall back to the global config, which is exactly
            # what they were queued under.
            overrides = {
                c.slug: row_request_overrides(c) for c in session.query(Collection).all() if c.build != "shared"
            }
            # One claim per title tagged with its row, then a single send — so rows sharing a Radarr
            # share one client and one rate limiter. A loop of per-group sends would give each group
            # its own, multiplying the write rate to that server (plex-safety rule 6).
            cfg_by_row = {"": cfg} | {slug: resolve_request_config(cfg, ov) for slug, ov in overrides.items()}
            claims = [(row.row_slug if row.row_slug in cfg_by_row else "", _title(row)) for row in rows]
            report = request_titles_by_row(cfg_by_row, tmdb, claims, dry_run=body.dry_run)
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
                    # Stamped once, here, so "watched since sent" can compare against the real send
                    # time rather than an `updated_at` that later edits move around.
                    row.sent_at = datetime.now(UTC)
                outcomes.append({"id": row.id, "title": row.title, "status": outcome.status, "detail": outcome.detail})
            session.add(
                Event(scope="requests.send", level="info", message={"dry_run": body.dry_run, "outcomes": outcomes})
            )
            session.commit()
            sent = sum(1 for o in outcomes if o["status"] in ("requested", "would_request"))
            return {"sent": sent, "dry_run": body.dry_run, "outcomes": outcomes}

    return await asyncio.get_running_loop().run_in_executor(None, _send)
