"""Runs API: list, detail with per-user diffs and picks, trigger a run."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import func

from shortlist.server.api.schemas_runs import (
    RunCancelledOut,
    RunCreatedOut,
    RunDetailOut,
    RunLogLineOut,
    RunsDeletedOut,
    RunsSummaryOut,
    RunSummaryOut,
    RunUserTraceOut,
)
from shortlist.server.auth import require_owner
from shortlist.server.db.models import PickRow, RequestCandidate, Run, RunUser, iso_utc

router = APIRouter(prefix="/runs", tags=["runs"], dependencies=[Depends(require_owner)])


class RunRequest(BaseModel):
    user_ids: list[int] | None = None
    # Scope the run to specific rows (None = every row). Privacy is unaffected: build_only only narrows
    # the delivery loop; the sweep, share-filter merge, and promotion still see every row (plex-safety).
    collection_ids: list[int] | None = None
    dry_run: bool = False


def _run_summary(run: Run) -> dict:
    return {
        "id": run.id,
        "trigger": run.trigger,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
        "status": run.status,
        "dry_run": run.dry_run,
        "stats": run.stats or {},
        # WHY a run failed. It was only ever inside `stats`, which nothing rendered — so a run that
        # failed for a run-level reason (a refused share filter, a sweep that could not run) showed
        # "Failed" and nothing else, and the operator had to read container logs (issue #1).
        "error": (run.stats or {}).get("error"),
        "promotion_blockers": (run.stats or {}).get("promotion_blockers") or [],
    }


@router.get("", response_model=list[RunSummaryOut])
async def list_runs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    collection: str | None = None,
    before_id: int | None = None,
) -> list[dict]:
    """Recent runs, newest first. `collection` (a row slug) narrows to runs that actually built that
    row — the ones whose picks carry its slug — so the Rows page can link a row to its own history.

    `before_id` pages backwards through history: pass the id of the oldest run you already have.
    Cursor rather than offset because runs are inserted while you read — an offset would skip or
    repeat rows as the list shifts under you.
    """
    with request.app.state.sessions() as session:
        query = session.query(Run).order_by(Run.id.desc())
        if collection:
            built_in = session.query(PickRow.run_id).filter(PickRow.collection_slug == collection).distinct()
            query = query.filter(Run.id.in_(built_in))
        if before_id is not None:
            query = query.filter(Run.id < before_id)
        runs = query.limit(min(limit, 200)).all()
        return [_run_summary(r) for r in runs]


@router.get("/summary", response_model=RunsSummaryOut)
async def runs_summary(request: Request) -> dict:
    """Totals for the Runs page header: how many runs, how many succeeded/failed, and the last one."""
    with request.app.state.sessions() as session:
        total = session.query(func.count(Run.id)).scalar() or 0
        ok = session.query(func.count(Run.id)).filter(Run.status == "ok").scalar() or 0
        error = session.query(func.count(Run.id)).filter(Run.status == "error").scalar() or 0
        last = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
        return {
            "total": total,
            "ok": ok,
            "error": error,
            "last_finished": iso_utc(last.finished_at) if last else None,
            "last_status": last.status if last else None,
        }


@router.delete("", response_model=RunsDeletedOut)
async def clear_runs(request: Request) -> dict:
    """Delete all run history (the Runs list and per-user detail/traces). Picks are KEPT so the
    dashboard's lifetime metrics survive — only the browsable history is cleared. Changes nothing
    on Plex. Note: the next run will re-curate from scratch (no carry-forward) since picks lose
    their run association."""
    with request.app.state.sessions() as session:
        deleted = session.query(func.count(Run.id)).scalar() or 0
        # Detach picks from their runs (null run_id) so the dashboard metrics survive, then delete
        # the run history itself (per-user traces are the storage hog at ~100 KB per user per run).
        session.query(PickRow).filter(PickRow.run_id.isnot(None)).update(
            {PickRow.run_id: None}, synchronize_session=False
        )
        session.query(RunUser).delete(synchronize_session=False)
        session.query(Run).delete(synchronize_session=False)
        session.commit()
    return {"deleted": deleted}


def _with_provenance(breakdown: list[dict], picks: list) -> list[dict]:
    """Fill in `sources`/`affinity`/`year`/`rating` on breakdown picks from the picks table.

    The run page renders the stored breakdown blob, which only started carrying each of these from
    the run that introduced it — but the `picks` rows for those same runs have them. Joining on
    (tmdb_id, media_type) means an existing run explains itself immediately instead of staying blank
    until it is rebuilt.

    Each field is filled independently: provenance and release year arrived in different releases, so
    a run carrying one but not the other must still gain the missing one. A key already present in
    the blob always wins — the blob is what the engine actually delivered.
    """
    known = {(p.tmdb_id, p.media_type): p for p in picks}
    out = []
    for entry in breakdown:
        enriched = []
        for pick in entry.get("picks") or []:
            row = known.get((pick.get("tmdb_id"), pick.get("media_type")))
            if row is None:
                enriched.append(pick)
                continue
            filled = dict(pick)
            if not filled.get("sources"):
                filled["sources"] = [s for s in (row.sources or "").split(",") if s]
                filled["affinity"] = row.affinity
            # `year` is legitimately None (a cold-start pick has no TMDB year), so presence of the
            # KEY decides, not truthiness — otherwise every such pick is re-looked-up for ever.
            if "year" not in filled:
                filled["year"] = row.year
            if "rating" not in filled:
                filled["rating"] = row.rating
            enriched.append(filled)
        out.append({**entry, "picks": enriched})
    return out


def _tmdb_ids_in(blob: object) -> set[int]:
    """Every ``tmdb_id`` mentioned anywhere in a trace/breakdown blob.

    Walked generically rather than by reaching into named stages: the trace's shape is the engine's,
    it has gained stages several times, and the page looks an outcome up for any candidate it renders.
    A structural walk cannot fall behind that; a hand-written list of paths silently would.
    """
    found: set[int] = set()
    stack: list[object] = [blob]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            value = node.get("tmdb_id")
            if isinstance(value, int):
                found.add(value)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


#: `.in_()` binds one parameter per id and SQLite's compiled ceiling is 999 on older builds, so a
#: long trace is queried in chunks rather than in one statement that would raise.
_ID_CHUNK = 500


def _request_outcomes(session, tmdb_ids: set[int]) -> dict[str, dict]:
    """What became of every wanted-but-missing title, keyed ``"<tmdb_id>:<media_type>"``.

    The trace's "not in your libraries" drops are the titles the curator wanted that no delivery
    library held; some of those become Sonarr/Radarr requests. This joins the request inbox back so
    the trace can say WHICH — "requested from Radarr" vs "queued for your approval". Request state is
    run-WIDE (one row per title, whoever wanted it), so the key is title-only.

    Scoped to the ids this trace actually mentions. It used to read the WHOLE ``request_candidates``
    table for a single user's page — a table that only grows, every night, for ever — and then throw
    away everything the overlay never looked up.

    `hidden` rows are kept: they're still `status="sent"` — the request DID happen, and the trace
    records what happened, not what's currently in the inbox.
    """
    if not tmdb_ids:
        return {}
    ids = sorted(tmdb_ids)
    rows = []
    for start in range(0, len(ids), _ID_CHUNK):
        rows.extend(
            session.query(RequestCandidate).filter(RequestCandidate.tmdb_id.in_(ids[start : start + _ID_CHUNK])).all()
        )
    return {
        f"{r.tmdb_id}:{r.media_type}": {
            "status": r.status,  # pending | sent | rejected
            "detail": r.detail or "",
            "arr_slug": r.arr_slug,
            "excluded": r.excluded,  # on the arr's import-exclusion list — approving is a no-op
        }
        for r in rows
    }


@router.get("/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: int, request: Request) -> dict:
    with request.app.state.sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        users = []
        for run_user in run.users:
            picks = (
                session.query(PickRow).filter_by(run_id=run_id, user_id=run_user.user_id).order_by(PickRow.rank).all()
            )
            users.append(
                {
                    "username": run_user.user.username,
                    # nickname → Tautulli friendly name → username; the runs view shows this so a
                    # person reads the same in runs as on the Users page (see User.display_name).
                    "display_name": run_user.user.display_name,
                    "slug": run_user.user.slug,
                    "status": run_user.status,
                    "error": run_user.error,
                    # Why a `skipped` result happened — an explanation, not a failure (NULL on
                    # legacy rows and on every non-skipped result).
                    "reason": run_user.reason,
                    "duration_ms": run_user.duration_ms,
                    "llm_tokens": run_user.llm_tokens,
                    # Where this user's AI tokens went ({} on legacy rows), and Exa searches (billed
                    # per search, not per token — kept apart from the token totals).
                    "llm_tokens_by_step": run_user.llm_tokens_by_step or {},
                    "exa_searches": run_user.exa_searches,
                    "diff": run_user.diff or {},
                    "picks": [
                        {
                            "rank": p.rank,
                            "title": p.title,
                            "reason": p.reason,
                            "seed_title": p.seed_title,
                            "sources": [s for s in (p.sources or "").split(",") if s],
                            "affinity": p.affinity,
                            "year": p.year,
                            "rating": p.rating,
                        }
                        for p in picks
                    ],
                    # Per-(row, library) breakdown; [] on legacy runs -> UI falls back to diff + picks.
                    "breakdown": _with_provenance(run_user.breakdown or [], picks),
                    # Whether a full pipeline trace was recorded for this user (fetched on demand from
                    # the trace endpoint — the blob is large, so it stays out of the detail payload).
                    "has_trace": bool(run_user.trace),
                }
            )
        # Users who haven't finished yet show as "pending" so the UI can pre-populate the list
        # during a live run instead of only showing people after they complete.
        completed_slugs = {ru.user.slug for ru in run.users}
        expected = (run.stats or {}).get("expected_users", [])
        pending = [
            {
                "username": u["username"],
                "display_name": u.get("display_name", u["username"]),
                "slug": u["slug"],
                "status": "pending",
                "error": None,
                "reason": None,
                "duration_ms": None,
                "llm_tokens": 0,
                "llm_tokens_by_step": {},
                "exa_searches": 0,
                "diff": {},
                "picks": [],
                "breakdown": [],
                "has_trace": False,
            }
            for u in expected
            if u["slug"] not in completed_slugs
        ]
        return {**_run_summary(run), "users": users + pending}


@router.get("/{run_id}/users/{user_id}/trace", response_model=RunUserTraceOut)
async def get_run_user_trace(run_id: int, user_id: int, request: Request) -> dict:
    """The full pipeline trace for one user in one run: seeds derived, each candidate source's
    queries and returns, the web-search / RAG prompts, and the titles proposed. Fetched on demand
    (it's a large blob) so the run-detail page stays light. Empty ``{}`` on a run predating the
    feature or a skipped/cold user — the UI reads that as "no trace for this run", not an error."""
    with request.app.state.sessions() as session:
        run_user = session.get(RunUser, (run_id, user_id))
        if run_user is None:
            raise HTTPException(status_code=404, detail="no such user in this run")
        # The delivered ending of the flow — what each library's row ended up with, and why — lives on
        # the picks table + breakdown blob, not in the trace dict. Join it here so the trace page can
        # show "what we built per library" as the last stage without a second fetch.
        picks = session.query(PickRow).filter_by(run_id=run_id, user_id=run_user.user_id).order_by(PickRow.rank).all()
        trace = run_user.trace or {}
        breakdown = _with_provenance(run_user.breakdown or [], picks)
        return {
            "username": run_user.user.username,
            "display_name": run_user.user.display_name,
            "status": run_user.status,
            # Surfaced so the trace page can explain a failed/skipped person, not just show partial
            # stages: `error` for a failure, `reason` for a (non-failing) skip.
            "error": run_user.error,
            "reason": run_user.reason,
            "trace": trace,
            "breakdown": breakdown,
            # What became of the titles the curator wanted but no library held. The trace marks those
            # "not in your libraries"; this says whether the request subsystem then asked Sonarr/Radarr
            # for them (or queued them for the owner), keyed by "<tmdb_id>:<media_type>" so the UI can
            # overlay the outcome onto that fate. Run-WIDE state (a title is requested once for the
            # whole server, whoever wanted it) — the overlay is scoped by only matching this user's
            # missing titles. Empty on a deployment with requests off.
            "requests": _request_outcomes(session, _tmdb_ids_in(trace) | _tmdb_ids_in(breakdown)),
        }


@router.get("/{run_id}/log", response_model=list[RunLogLineOut])
async def get_run_log(
    run_id: int,
    request: Request,
    after_seq: int | None = None,
    format: str = "json",
) -> list[dict] | PlainTextResponse:
    """The run's activity log: per-user stages, then the server-wide phases that follow them.

    Served from the live in-memory tail while the run is in flight and from `run_log_lines`
    afterwards, so a run whose process has since restarted still has a readable log.

    `after_seq` returns only lines past that sequence number, for topping up without refetching the
    whole feed. `format=text` renders it as plain text for the download button — that branch returns
    a `Response` directly, which FastAPI hands back untouched, so the schema describes the JSON one.
    """
    lines = request.app.state.run_service.run_log(run_id, after_seq=after_seq)
    if format == "text":
        return PlainTextResponse(
            _log_as_text(lines), headers={"Content-Disposition": f'inline; filename="run-{run_id}.log"'}
        )
    return lines


def _log_as_text(lines: list[dict]) -> str:
    """One line per entry: timestamp, subject, stage, then counts and reason. Deliberately plain —
    this is what someone pastes into a bug report."""
    out = []
    for entry in lines:
        counts = " ".join(f"{k}={v}" for k, v in (entry.get("counts") or {}).items())
        parts = [entry.get("ts", ""), entry.get("user", ""), entry.get("stage", ""), counts, entry.get("reason", "")]
        out.append("  ".join(p for p in parts if p))
    return "\n".join(out) + ("\n" if out else "")


@router.post("", status_code=202, response_model=RunCreatedOut)
async def trigger_run(body: RunRequest, request: Request) -> dict:
    run_id = await request.app.state.run_service.start_run(
        trigger="manual", dry_run=body.dry_run, user_ids=body.user_ids, collection_ids=body.collection_ids
    )
    return {"run_id": run_id}


@router.post("/{run_id}/cancel", response_model=RunCancelledOut)
async def cancel_run(run_id: int, request: Request) -> dict:
    """Ask the in-flight run to stop. Cooperative — it finishes the person it's on, then stops, and
    still merges the privacy filters + promotes everyone delivered so far. 409 if it isn't running."""
    if not request.app.state.run_service.cancel_run(run_id):
        raise HTTPException(status_code=409, detail="This run isn't currently running.")
    return {"cancelling": True}
