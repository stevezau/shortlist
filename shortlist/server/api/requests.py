"""The Sonarr/Radarr approval inbox: list wanted-but-missing titles, send the chosen ones, reject the rest.

A request asks a download app for a file — it touches no Plex object. It is gated only on the owner
session and on requests being configured. Sending runs in a
worker thread (the Arr/TMDB clients are sync) and respects ``dry_run``.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import text

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.models import MediaType
from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.request_holds import is_story_film, match_hold
from shortlist.engine.requests import RequestBatch, request_titles_by_row
from shortlist.server.assistant_auth.routes import BrowserOwnerDep
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, Event, RequestCandidate, iso_utc
from shortlist.server.schema_base import PassthroughModel
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
    # TMDB's `original_language` (ISO 639-1, lowercase). "" -> unknown, and the inbox draws no chip:
    # a title queued before 0085, or one only a non-TMDB source ever surfaced.
    language: str = ""
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
    # Which row claimed it — what decides the Sonarr/Radarr target an approval will use.
    # Null for anything queued before per-row settings, which falls back to the global config.
    row_slug: str | None = None


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
            language=r.language or "",
            demand=r.demand,
            tags=list(r.tags or []),
            wanters=list(r.wanters or []),
            why=[RequestWhyOut(**w) for w in (r.why or [])],
            status=r.status,
            detail=r.detail,
            excluded=bool(r.excluded),
            arr_slug=r.arr_slug,
            row_slug=r.row_slug,
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
    # "off" whenever requests route to Radarr/Sonarr, and vice versa — the two targets are
    # exclusive, so at most one of these three is ever anything but "off".
    overseerr: ArrReach = "off"


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

        with state.sessions() as session:
            rows = session.query(RequestCandidate).filter(RequestCandidate.status.in_(("pending", "sent"))).all()

        if cfg.overseerr:
            # One walk of Overseerr's media table answers every row at once, movies and shows alike,
            # because it keys both by TMDB id — so none of the Sonarr v3 TVDB fallback below applies.
            from shortlist.engine.clients.seerr import SeerrClient

            try:
                state_by_key = SeerrClient(cfg.overseerr).media_state()
                reach: ArrReach = "ok"
            except Exception as e:
                state_by_key, reach = {}, "unreachable"
                logger.warning("request status: Overseerr lookup failed ({})", e)
            return {
                "statuses": {r.id: state_by_key.get((r.media_type, r.tmdb_id)) for r in rows},
                "radarr": "off",
                "sonarr": "off",
                "overseerr": reach,
            }

        # One fetch per app up front. A failure here is not fatal: the inbox simply shows no status
        # rather than erroring, which is what it did before this endpoint existed — but it is now
        # REPORTED, so "no badges" can be told apart from "nothing to badge".
        from shortlist.engine.clients.arr import RadarrClient, SonarrClient

        movies: dict[int, str] = {}
        shows_by_tvdb: dict[int, str] = {}
        shows_by_tmdb: dict[int, str] = {}
        radarr_reach: ArrReach = "off"
        sonarr_reach: ArrReach = "off"

        # Each read is a whole-library dump that takes seconds, and the two apps are independent
        # servers, so both start now: the wait is the slower read, not the sum. Results are applied
        # in the old order (Radarr, then Sonarr), and a failure is caught inside its own thread.
        def _guarded(read):
            try:
                return read(), None
            except Exception as e:
                return None, e

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="requests-status") as pool:
            # Each submit runs in a copy of this thread's context, or loguru's `contextualize` keys
            # are lost on the pool threads.
            radarr_read = (
                pool.submit(contextvars.copy_context().run, _guarded, lambda: RadarrClient(cfg.radarr).status_by_tmdb())
                if cfg.radarr
                else None
            )
            sonarr_read = (
                pool.submit(contextvars.copy_context().run, _guarded, lambda: SonarrClient(cfg.sonarr).status_by_ids())
                if cfg.sonarr
                else None
            )
            if radarr_read:
                radarr_reach = "ok"
                result, error = radarr_read.result()
                if error is not None:
                    radarr_reach = "unreachable"
                    logger.warning("request status: Radarr lookup failed ({})", error)
                else:
                    movies = result
            if sonarr_read:
                sonarr_reach = "ok"
                result, error = sonarr_read.result()
                if error is not None:
                    sonarr_reach = "unreachable"
                    logger.warning("request status: Sonarr lookup failed ({})", error)
                else:
                    shows_by_tvdb, shows_by_tmdb = result

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
                # Deliberately NOT a bare `except Exception`: a wrong enum name here would raise AttributeError,
                # be swallowed to a debug line, and the fallback would silently no-op for ever on Sonarr
                # v3, leaving every show with a blank status. Only
                # a transport failure or the TMDB client's own HTTP error is tolerable here; anything
                # else is a bug and must be loud.
                except (httpx.HTTPError, RuntimeError) as e:
                    logger.debug("request status: tvdb lookup for {!r} failed ({})", row.title, e)
                    tvdb_id = None
                status = shows_by_tvdb.get(tvdb_id) if tvdb_id else None
            statuses[row.id] = status

        return {"statuses": statuses, "radarr": radarr_reach, "sonarr": sonarr_reach}

    return await asyncio.get_running_loop().run_in_executor(None, _fetch_statuses)


#: "off" = no URL + key for it; "unreachable" = configured, but the read failed; else "connected".
RowSourceState = Literal["connected", "unreachable", "off"]


class RowSourceServerOut(PassthroughModel):
    """One Radarr/Sonarr server Overseerr sends to, and whether it stamps the requester's tag."""

    kind: str  # "radarr" | "sonarr"
    name: str
    is4k: bool
    tag_requests: bool


class TagMatchOut(PassthroughModel):
    """How one requester tag on Radarr/Sonarr resolved — the preview under "Use my own tags"."""

    label: str
    source: Literal["overseerr", "pattern", "override"]
    user_id: int | None  # the DB user it names; None when it names nobody on the roster
    display_name: str  # "" when it names nobody
    titles: int  # items carrying the tag, matched or not
    ambiguous: bool  # more than one person renders to this tag, so it credits nobody


class PersonReadyOut(PassthroughModel):
    user_id: int
    display_name: str
    linked: bool  # an Overseerr account carries this person's Plex id
    ready: int  # titles they asked for that are on disk, any library


class RowSourcesOut(PassthroughModel):
    """The requests-row setup check: can the row know who asked for what, and for whom?"""

    overseerr: RowSourceState
    radarr: RowSourceState
    sonarr: RowSourceState
    complete: bool  # every configured source was read in full
    problems: list[str]
    seerr_requests: int
    seerr_requesters: int
    seerr_linked: int  # requesters whose account maps to someone on the roster
    servers: list[RowSourceServerOut]
    tagged_movies: int  # movies credited to a person by a Radarr tag alone
    tagged_shows: int
    people: list[PersonReadyOut]
    tags: list[TagMatchOut]


#: The inbox's movies a hold preview reads, most-wanted first. Each costs up to two TMDB reads the first
#: time (cached after), so an inbox of thousands must not turn one chip click into a minute of calls.
HOLD_PREVIEW_LIMIT = 100
_HOLD_PREVIEW_WORKERS = 8


class HoldPreviewIn(BaseModel):
    """Genres and tags the owner is considering, before they are saved."""

    genres: list[int] = Field(default_factory=list, max_length=30)
    tags: list[int] = Field(default_factory=list, max_length=60)


class HeldTitleOut(PassthroughModel):
    tmdb_id: int
    title: str
    year: int | None
    reason: str  # "genre Music" | "tag “concert film”"
    story: bool  # a story film the picks catch — a sign a pick is broader than meant


class HoldPreviewOut(PassthroughModel):
    checked: int  # pending movies read (at most HOLD_PREVIEW_LIMIT)
    held: list[HeldTitleOut]
    unread: int  # pending movies TMDB couldn't answer for; a run would hold them too


@router.post("/hold-preview", response_model=HoldPreviewOut)
async def hold_preview(body: HoldPreviewIn, request: Request) -> dict:
    """Which movies waiting in the inbox these genres/tags would hold — the settings page's live check.

    Reads TMDB through the shared cache, so a title a run already looked at costs nothing. Nothing is saved.
    """
    with request.app.state.sessions() as session:
        rows = (
            session.query(RequestCandidate.tmdb_id, RequestCandidate.title, RequestCandidate.year)
            .filter(
                ~RequestCandidate.hidden,
                RequestCandidate.status == "pending",
                RequestCandidate.media_type == MediaType.MOVIE.value,
            )
            .order_by(RequestCandidate.demand.desc(), RequestCandidate.rating.desc(), RequestCandidate.id)
            .limit(HOLD_PREVIEW_LIMIT)
            .all()
        )
    genres, tags = frozenset(body.genres), frozenset(body.tags)
    if not (genres or tags):
        return {"checked": len(rows), "held": [], "unread": 0}
    tmdb = request.app.state.run_service.build_tmdb_only()
    if tmdb is None:
        raise HTTPException(status_code=503, detail="Add a TMDB API key in Settings first.")

    def judge(tmdb_id: int) -> tuple[str, bool] | None:
        """``(reason, story)`` for a held movie, ``("", False)`` for an allowed one, None if TMDB can't say."""
        try:
            reason = match_hold(tmdb, tmdb_id, genres=genres, tags=tags)
            if not reason:
                return "", False
            genre_ids = [g["id"] for g in tmdb.details(tmdb_id, MediaType.MOVIE).get("genres") or [] if "id" in g]
            return reason, is_story_film(genre_ids)
        except Exception as e:
            logger.debug("hold preview: TMDB read for {} failed ({})", tmdb_id, type(e).__name__)
            return None

    def judge_all() -> list[tuple[str, bool] | None]:
        with ThreadPoolExecutor(max_workers=_HOLD_PREVIEW_WORKERS, thread_name_prefix="hold-preview") as pool:
            return list(pool.map(lambda tid: contextvars.copy_context().run(judge, tid), [r.tmdb_id for r in rows]))

    verdicts = await asyncio.to_thread(judge_all)
    held = [
        {"tmdb_id": r.tmdb_id, "title": r.title, "year": r.year, "reason": v[0], "story": v[1]}
        for r, v in zip(rows, verdicts, strict=True)
        if v is not None and v[0]
    ]
    return {"checked": len(rows), "held": held, "unread": sum(v is None for v in verdicts)}


@router.get("/row-sources", response_model=RowSourcesOut)
async def get_row_sources(
    request: Request,
    pattern: Annotated[
        # 128 is the stored column's length: a longer pattern could never be saved, so it is not previewed.
        str, Query(max_length=128, description="An own-tag pattern to preview, e.g. req-{username}")
    ] = "",
    row_id: Annotated[
        int | None,
        Query(
            description="The requests row being edited. Its pattern is the typed one; every OTHER enabled "
            "requests row's pattern joins it, because a run judges a tag against all of them. Omitted, "
            "every enabled requests row's saved pattern joins the typed one."
        ),
    ] = None,
) -> dict:
    """Read every request source once and say whether a "Your requests" row can be built from it.

    Read-only: nothing is written to Overseerr, the Arrs, or Plex. A source that is down reads as
    "unreachable" with the reason in `problems` — never a 500, because the screen this feeds exists
    precisely to show the owner what is wrong.
    """
    svc = request.app.state.run_service

    def _check() -> dict:
        from shortlist.engine.requests_row import RequestLedger, collect_requests

        sources, profiles, db_ids = svc.build_request_sources_only()
        patterns = {pattern} if pattern else set()
        with request.app.state.sessions() as session:
            saved = session.query(Collection.requests_tag_pattern).filter(Collection.requests_row, Collection.enabled)
            # Without a row_id nothing is being edited, so every enabled row's pattern counts, as in a run.
            others = saved if row_id is None else saved.filter(Collection.id != row_id)
            patterns |= {p.strip() for (p,) in others if p and p.strip()}
        if sources is None:
            ledger = RequestLedger(titles=[], complete=True)
        else:
            try:
                ledger = collect_requests(sources, profiles, patterns=frozenset(patterns))
            except Exception as e:
                # collect_requests swallows per-source failures itself; this is for anything that
                # goes wrong before a read starts (a client refusing its URL, say).
                logger.warning("requests row check: sources could not be read ({})", redact(str(e)))
                ledger = RequestLedger(
                    titles=[], complete=False, problems=[f"Request sources could not be read: {redact(str(e))}"]
                )
                ledger.unreadable = {
                    app
                    for app, target in (
                        ("Overseerr", sources.overseerr),
                        ("Radarr", sources.radarr),
                        ("Sonarr", sources.sonarr),
                    )
                    if target
                }

        def state(target: object, app: str) -> RowSourceState:
            # Tracks the READ, not the wording of `problems`: advice ("requester tags were found ...
            # but Overseerr isn't connected") and degradations (media dates) name an app without
            # that app being down.
            if target is None:
                return "off"
            return "unreachable" if app in ledger.unreadable else "connected"

        by_plex = {p.plex_account_id: p for p in profiles}
        tagged = [t for t in ledger.titles if "tag" in t.found_in]
        return {
            "overseerr": state(sources and sources.overseerr, "Overseerr"),
            "radarr": state(sources and sources.radarr, "Radarr"),
            "sonarr": state(sources and sources.sonarr, "Sonarr"),
            "complete": ledger.complete,
            "problems": ledger.problems,
            "seerr_requests": ledger.seerr_requests,
            "seerr_requesters": ledger.seerr_requesters,
            "seerr_linked": ledger.seerr_linked,
            "servers": ledger.seerr_servers,
            "tagged_movies": sum(1 for t in tagged if t.media_type is MediaType.MOVIE),
            "tagged_shows": sum(1 for t in tagged if t.media_type is MediaType.SHOW),
            "people": [
                {
                    "user_id": db_ids[p.plex_account_id],
                    "display_name": p.display_name,
                    "linked": p.plex_account_id in ledger.seerr_plex_ids,
                    "ready": sum(1 for t in ledger.for_person(p.plex_account_id) if t.on_disk),
                }
                for p in profiles
            ],
            "tags": [
                {
                    "label": m.label,
                    "source": m.source,
                    "user_id": db_ids.get(m.plex_account_id) if m.plex_account_id is not None else None,
                    "display_name": by_plex[m.plex_account_id].display_name if m.plex_account_id in by_plex else "",
                    "titles": m.titles,
                    "ambiguous": m.ambiguous,
                }
                for m in ledger.tag_matches
            ],
        }

    return await asyncio.get_running_loop().run_in_executor(None, _check)


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
    row ``sent`` only when the app accepted it. Known skips leave it available for another attempt;
    uncertain outcomes keep a durable claim until the owner checks the destination and releases it.
    """
    state = request.app.state
    svc = state.run_service

    def _send() -> dict:
        cfg, tmdb = svc.build_requests_context()
        if cfg is None:
            # Names no app: `cfg` is None precisely because requests are off, so which route the
            # owner would have used is not knowable here — and guessing named the wrong one half
            # the time.
            raise HTTPException(status_code=409, detail="Turn on requests in Settings first.")
        from types import SimpleNamespace

        from shortlist.server.services.request_actions import (
            finish_request_dispatch,
            missing_title,
            request_send_entry,
            reserve_request_dispatches,
            start_manual_request_dispatch,
        )

        with state.sessions() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            rows = (
                session.query(RequestCandidate)
                .filter(RequestCandidate.id.in_(body.ids), RequestCandidate.status == "pending")
                .all()
            )
            entries = [request_send_entry(session, row, cfg) for row in rows]
            overrides = {
                c.slug: row_request_overrides(c) for c in session.query(Collection).all() if c.build != "shared"
            }
            cfg_by_row = {"": cfg} | {slug: resolve_request_config(cfg, ov) for slug, ov in overrides.items()}
            claims = []
            if not body.dry_run:
                try:
                    claims = reserve_request_dispatches(session, rows, entries, origin="manual")
                except ValueError as error:
                    raise HTTPException(status_code=409, detail=str(error)) from None
            dispatch_ids = {claim.candidate_id: claim.id for claim in claims}
            session.commit()

        # Individual durable checkpoints share one invocation-local client cache and server clock.
        # No database transaction remains open during metadata reads or acquisition calls.
        batch = RequestBatch()
        outcomes = []
        for entry in entries:
            dispatch_id = dispatch_ids.get(entry["candidate_id"])
            if dispatch_id is not None:
                with state.sessions() as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    current = start_manual_request_dispatch(session, dispatch_id)
                    session.commit()
                if current is None:
                    outcomes.append(
                        {
                            "id": entry["candidate_id"],
                            "title": entry["title"]["title"],
                            "status": "skipped_changed",
                            "detail": "The selected candidate is no longer pending.",
                        }
                    )
                    continue
            slug = entry["row_slug"] if entry["row_slug"] in cfg_by_row else ""
            try:
                report = request_titles_by_row(
                    cfg_by_row, tmdb, [(slug, missing_title(entry["title"]))], dry_run=body.dry_run, batch=batch
                )
                outcome = report.outcomes[0] if report.outcomes else None
                if outcome is None:
                    raise RuntimeError("The acquisition service returned no outcome")
            except Exception:
                outcome = SimpleNamespace(
                    status="error",
                    detail="The acquisition outcome is uncertain; check the destination before retrying.",
                    arr_slug=None,
                )
            with state.sessions() as session:
                if dispatch_id is not None:
                    finish_request_dispatch(session, dispatch_id, outcome)
                else:
                    row = session.get(RequestCandidate, entry["candidate_id"])
                    if row is not None:
                        row.detail = outcome.detail
                        if outcome.arr_slug:
                            row.arr_slug = outcome.arr_slug
                session.commit()
            outcomes.append(
                {
                    "id": entry["candidate_id"],
                    "title": entry["title"]["title"],
                    "status": outcome.status,
                    "detail": outcome.detail,
                }
            )
        with state.sessions() as session:
            session.add(
                Event(scope="requests.send", level="info", message={"dry_run": body.dry_run, "outcomes": outcomes})
            )
            session.commit()
        sent = sum(1 for outcome in outcomes if outcome["status"] in ("requested", "would_request"))
        return {"sent": sent, "dry_run": body.dry_run, "outcomes": outcomes}

    return await asyncio.get_running_loop().run_in_executor(None, _send)


class AcquisitionReleaseIn(BaseModel):
    """One owner-confirmed decision on an exact terminal acquisition record."""

    model_config = {"extra": "forbid", "strict": True}
    review_token: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    expected_status: Literal["outcome_unknown", "succeeded"]
    checked_destination: Literal[True]


@router.get("/acquisition-claims")
def acquisition_claims(
    request: Request,
    owner: BrowserOwnerDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
) -> dict:
    """Show durable acquisition reservations and uncertain outcomes to their owner."""
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch
    from shortlist.server.services.request_actions import acquisition_review_token

    with request.app.state.sessions() as session:
        rows = (
            session.query(AssistantRequestDispatch)
            .filter(
                AssistantRequestDispatch.status.in_(("reserved", "external_started", "outcome_unknown", "succeeded"))
            )
            .order_by(AssistantRequestDispatch.id.desc())
            .offset(offset)
            .limit(limit + 1)
            .all()
        )
        items = [
            {
                "id": row.id,
                "candidate_id": row.candidate_id,
                "origin": row.origin,
                "title": row.request_body["title"]["title"],
                "tmdb_id": row.request_body["title"]["tmdb_id"],
                "media_type": row.request_body["title"]["media_type"],
                "destination": row.destination,
                "status": row.status,
                "created_at": iso_utc(row.created_at),
                "external_started_at": iso_utc(row.external_started_at),
                "finished_at": iso_utc(row.finished_at),
                "review_token": acquisition_review_token(row),
            }
            for row in rows[:limit]
        ]
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}


@router.post("/acquisition-claims/{claim_id}/release")
def release_acquisition_claim(
    claim_id: int,
    body: AcquisitionReleaseIn,
    request: Request,
    owner: BrowserOwnerDep,
) -> dict:
    """Release an exact terminal claim after the owner has checked the remote destination."""
    from shortlist.server.assistant.operation_models import AssistantRequestDispatch
    from shortlist.server.services.request_actions import acquisition_review_token

    with request.app.state.sessions() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        claim = session.get(AssistantRequestDispatch, claim_id)
        if claim is None:
            raise HTTPException(status_code=404, detail="Acquisition claim not found")
        if (
            claim.status not in {"outcome_unknown", "succeeded"}
            or claim.status != body.expected_status
            or acquisition_review_token(claim) != body.review_token
        ):
            raise HTTPException(
                status_code=409, detail="The acquisition changed; review its current state before releasing it."
            )
        previous = claim.status
        claim.status = "released"
        claim.result = {
            **claim.result,
            "released_by_owner": owner.account_id,
            "released_at": datetime.now(UTC).isoformat(),
        }
        session.add(
            Event(
                scope="requests.claim_release",
                level="warning",
                message={
                    "claim_id": claim.id,
                    "owner_account_id": owner.account_id,
                    "previous_status": previous,
                    "tmdb_id": claim.request_body["title"]["tmdb_id"],
                    "media_type": claim.request_body["title"]["media_type"],
                    "destination": claim.destination,
                },
            )
        )
        session.commit()
        return {"id": claim.id, "status": "released"}
