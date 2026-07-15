"""Runs API: list, detail with per-user diffs and picks, trigger a run."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shortlist.server.auth import require_owner
from shortlist.server.db.models import PickRow, Run, iso_utc

router = APIRouter(prefix="/runs", tags=["runs"], dependencies=[Depends(require_owner)])


class RunRequest(BaseModel):
    user_ids: list[int] | None = None
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
    }


@router.get("")
async def list_runs(request: Request, limit: int = 50) -> list[dict]:
    with request.app.state.sessions() as session:
        runs = session.query(Run).order_by(Run.id.desc()).limit(min(limit, 200)).all()
        return [_run_summary(r) for r in runs]


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
                    "duration_ms": run_user.duration_ms,
                    "llm_tokens": run_user.llm_tokens,
                    "diff": run_user.diff or {},
                    "picks": [
                        {"rank": p.rank, "title": p.title, "reason": p.reason, "seed_title": p.seed_title}
                        for p in picks
                    ],
                    # Per-(row, library) breakdown; [] on legacy runs -> UI falls back to diff + picks.
                    "breakdown": run_user.breakdown or [],
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
        trigger="manual", dry_run=body.dry_run, user_ids=body.user_ids
    )
    return {"run_id": run_id}
