"""Runs API: list, detail with per-user diffs and picks, trigger a run."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func

from shortlist.server.auth import require_owner
from shortlist.server.db.models import PickRow, Run, RunUser, iso_utc

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


@router.get("")
async def list_runs(request: Request, limit: int = 50, collection: str | None = None) -> list[dict]:
    """Recent runs, newest first. `collection` (a row slug) narrows to runs that actually built that
    row — the ones whose picks carry its slug — so the Rows page can link a row to its own history."""
    with request.app.state.sessions() as session:
        query = session.query(Run).order_by(Run.id.desc())
        if collection:
            built_in = session.query(PickRow.run_id).filter(PickRow.collection_slug == collection).distinct()
            query = query.filter(Run.id.in_(built_in))
        runs = query.limit(min(limit, 200)).all()
        return [_run_summary(r) for r in runs]


@router.get("/summary")
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


@router.delete("")
async def clear_runs(request: Request) -> dict:
    """Delete ALL run history: every run, its per-user rows, and its picks. This also clears the
    effectiveness report (it's built from picks). Irreversible; it changes nothing on Plex."""
    with request.app.state.sessions() as session:
        deleted = session.query(func.count(Run.id)).scalar() or 0
        # Picks aren't ORM-cascaded off Run, and a bulk delete bypasses the RunUser cascade too, so
        # clear all three tables explicitly (order doesn't matter — no DB-level FK enforcement here).
        session.query(PickRow).delete(synchronize_session=False)
        session.query(RunUser).delete(synchronize_session=False)
        session.query(Run).delete(synchronize_session=False)
        session.commit()
    return {"deleted": deleted}


def _with_provenance(breakdown: list[dict], picks: list) -> list[dict]:
    """Fill in `sources`/`affinity` on breakdown picks from the picks table.

    The run page renders the stored breakdown blob, which only started carrying provenance from the
    run that introduced it — but the `picks` rows for those same runs have it. Joining on
    (tmdb_id, media_type) means an existing run explains itself immediately instead of staying blank
    until it is rebuilt. Entries that already carry provenance are left alone.
    """
    known = {(p.tmdb_id, p.media_type): p for p in picks}
    out = []
    for entry in breakdown:
        enriched = []
        for pick in entry.get("picks") or []:
            if pick.get("sources"):
                enriched.append(pick)
                continue
            row = known.get((pick.get("tmdb_id"), pick.get("media_type")))
            if row is None:
                enriched.append(pick)
                continue
            enriched.append(
                {**pick, "sources": [s for s in (row.sources or "").split(",") if s], "affinity": row.affinity}
            )
        out.append({**entry, "picks": enriched})
    return out


@router.get("/{run_id}")
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
                        }
                        for p in picks
                    ],
                    # Per-(row, library) breakdown; [] on legacy runs -> UI falls back to diff + picks.
                    "breakdown": _with_provenance(run_user.breakdown or [], picks),
                }
            )
        return {**_run_summary(run), "users": users}


@router.get("/{run_id}/log")
async def get_run_log(run_id: int, request: Request) -> list[dict]:
    """The run's stage activity log (history -> candidates -> curating -> delivering, per user).

    In-memory and live: it seeds the run page's activity feed on load and is topped up by the SSE
    `run.user.stage` stream. Empty for a run whose process has since restarted — the per-user results
    are the durable record; this is the live/recent debugging feed."""
    return request.app.state.run_service.run_log(run_id)


@router.post("", status_code=202)
async def trigger_run(body: RunRequest, request: Request) -> dict:
    run_id = await request.app.state.run_service.start_run(
        trigger="manual", dry_run=body.dry_run, user_ids=body.user_ids, collection_ids=body.collection_ids
    )
    return {"run_id": run_id}


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: int, request: Request) -> dict:
    """Ask the in-flight run to stop. Cooperative — it finishes the person it's on, then stops, and
    still merges the privacy filters + promotes everyone delivered so far. 409 if it isn't running."""
    if not request.app.state.run_service.cancel_run(run_id):
        raise HTTPException(status_code=409, detail="This run isn't currently running.")
    return {"cancelling": True}
