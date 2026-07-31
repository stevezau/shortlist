"""Effectiveness report: is Shortlist actually getting watched?

All of it comes from ``picks.watched_at`` (set when a delivered pick turns up in the person's watch
history), joined against runs, collections and the request queue. A "recommendation" is a distinct
(user, title) pair — a title recommended to one person — and it's a "hit" once that person watches it.
Distinct titles, not pick rows: a title re-recommended over several runs is one recommendation, and one
watch is one hit (counting rows would skew both). Owner-only.

Mostly a read of that history, but not entirely: ``POST /report/sync`` kicks off the watch-status
sync, and ``DELETE /report/deleted-rows`` permanently deletes the pick history of rows that no longer
exist — the one destructive action on the dashboard.

**Everything here is windowed** (``?window=7|30|90|all``, default 30). It used to be lifetime-cumulative,
which made every ratio meaningless: a pick can only ever be CREDITED within ``HIT_WINDOW_DAYS`` of
delivery, but the old denominator counted every pick ever delivered, forever. Each night therefore
added ~60 permanently-uncreditable picks per person to the bottom of the fraction, so the number
measured how long Shortlist had been installed rather than how good the picks were.

The one ratio that survives — ``overall.landing`` — is computed over a **matured cohort**: picks
delivered in the window *and* old enough to have had their full 30 days. Numerator and denominator
then describe the same set of picks, which is the only way the rate means anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shortlist.server.api.schemas_runs import (
    ClearedDeletedRowsOut,
    DeletedRowOut,
    EffectivenessReportOut,
    SyncStartedOut,
)
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    Event,
    PickRow,
    iso_utc,
)
from shortlist.server.scheduler import WATCH_SYNC_JOB_ID
from shortlist.server.services import report_service
from shortlist.server.services.report_service import DEFAULT_WINDOW

router = APIRouter(prefix="/report", tags=["report"], dependencies=[Depends(require_owner)])


@router.post("/sync", status_code=202, response_model=SyncStartedOut)
async def trigger_sync(request: Request) -> dict:
    """Run the daily watch-status sync on demand — refresh every user's watched picks now.

    Queued as a real `sync.history` JOB, the same one the Jobs page runs, rather than the bare
    background task this used to be. That task did the work but left no `jobs` row, so nothing that
    tracks jobs could see it: no toast, nothing in the header's activity popover, nothing in Jobs →
    Activity. The Jobs page appeared to show it only because its progress bar listens to the SSE
    `sync.progress` stream — a second, unrelated channel. One button, two mechanisms, and only one of
    them visible.

    Going through the queue also means it is durable and retried, which fire-and-forget never was.
    """
    from shortlist.server.services.jobs import drain_now, enqueue

    state = request.app.state
    job_id = enqueue(state.sessions, "sync.history")
    # Drained inline so pressing the button still feels instant; if it cannot run right now (a run
    # holds the lock) the row stays queued and the worker picks it up.
    await drain_now(state, "watch sync requested from the dashboard")
    return {"started": True, "job_id": job_id}


def _orphaned_slugs(session: Session) -> set[str]:
    """Row slugs that appear in `picks` but have no live Collection behind them.

    Computed server-side, the same way the report labels a line "deleted", so a client cannot ask us
    to purge a row that still exists by passing its slug.
    """
    live = {c.slug for c in session.query(Collection.slug).all()} | {"", DEFAULT_SLUG}
    used = {slug for (slug,) in session.query(PickRow.collection_slug).distinct().all()}
    return {slug for slug in used if slug not in live}


@router.get("/deleted-rows", response_model=list[DeletedRowOut])
async def deleted_rows(request: Request) -> list[dict]:
    """Pick history belonging to rows that no longer exist, with how much of it there is.

    Its own endpoint rather than a field on the report: the report is windowed, and "what can I clear"
    is a question about ALL of a deleted row's history, not the last 30 days of it.
    """
    with request.app.state.sessions() as session:
        orphans = _orphaned_slugs(session)
        if not orphans:
            return []
        rows = (
            session.query(
                PickRow.collection_slug,
                func.count(PickRow.id),
                func.min(PickRow.created_at),
                func.max(PickRow.created_at),
            )
            .filter(PickRow.collection_slug.in_(orphans))
            .group_by(PickRow.collection_slug)
            .all()
        )
        return [
            {
                "slug": slug,
                "picks": count,
                "first_seen": iso_utc(first),
                "last_seen": iso_utc(last),
            }
            for slug, count, first, last in sorted(rows, key=lambda r: -r[1])
        ]


@router.delete("/deleted-rows", response_model=ClearedDeletedRowsOut)
async def clear_deleted_rows(request: Request, slug: str | None = None) -> dict:
    """Permanently delete the pick history of rows that no longer exist. `slug` clears just one.

    This is the one destructive action on the dashboard, so it is deliberately narrow:

    * Only slugs with no live Collection are eligible — the eligible set is recomputed here rather
      than trusted from the request, so naming a live row's slug deletes nothing.
    * Only `picks` rows are touched. `deliveries` is left alone: it is the ledger of what still
      exists on Plex, and clearing it would strand a real collection with nothing left to clean it up.
    * These picks disappear from everywhere they are counted — not just this dashboard. The per-user
      lifetime stats and a person's pick history on their own page are read from the same rows, so the
      UI has to say "history", not "the numbers above".
    """
    with request.app.state.sessions() as session:
        eligible = _orphaned_slugs(session)
        targets = (eligible & {slug}) if slug is not None else eligible
        if not targets:
            # Nothing eligible is not an error: the row may have been cleared in another tab, or the
            # slug may belong to a row that still exists (in which case refusing is the whole point).
            return {"cleared": 0, "picks": 0, "slugs": []}
        # Per-slug counts, so the audit can answer "which row's 400 picks went" and not just a total.
        per_slug = dict(
            session.query(PickRow.collection_slug, func.count(PickRow.id))
            .filter(PickRow.collection_slug.in_(targets))
            .group_by(PickRow.collection_slug)
            .all()
        )
        # The `NOT EXISTS` is not redundant with `_orphaned_slugs` above — it closes the gap between
        # them. pysqlite opens the write transaction at this DELETE, so the eligibility read was an
        # autocommit snapshot: a row created in between (and `_unique_slug` hands a re-created row the
        # same slug back) would otherwise have its picks deleted as an orphan.
        live_slug = select(Collection.id).where(Collection.slug == PickRow.collection_slug)
        deleted = (
            session.query(PickRow)
            .filter(PickRow.collection_slug.in_(targets), ~live_slug.exists())
            .delete(synchronize_session=False)
        )
        # Audited like every other destructive action (plex-safety rule 10): "where did those numbers
        # go" must be answerable afterwards.
        session.add(
            Event(
                scope="report.clear_deleted_rows",
                level="info",
                message={"rows": per_slug, "picks": deleted},
            )
        )
        session.commit()
    logger.info("cleared pick history for {} deleted row(s): {} picks", len(targets), deleted)
    return {"cleared": len(targets), "picks": deleted, "slugs": sorted(targets)}


@router.get("", response_model=EffectivenessReportOut)
def effectiveness(
    request: Request,
    window: str = Query(DEFAULT_WINDOW, description="Report window in days: 7, 30, 90, or 'all'."),
) -> dict:
    """The dashboard tracking report: headline counts for the chosen window with their change vs the
    previous equal period, the landing rate over a matured cohort, watch momentum, per-user and
    per-row breakdowns, the titles landing best, requests that paid off, and a recent-watches feed.

    Deliberately a plain `def`, not `async def`: the computation is ~30 synchronous queries, one of
    them a `GROUP BY user_id, tmdb_id, media_type` over the whole picks table. On the event loop that
    stalled SSE, `/api/system/health` and every other request for as long as it took; as a sync
    handler Starlette runs it in a worker thread instead.
    """
    # Read off the scheduler here rather than in the service — that layer has no app state.
    scheduler = getattr(request.app.state, "scheduler", None)
    job = scheduler.get_job(WATCH_SYNC_JOB_ID) if scheduler else None
    next_watch_sync = iso_utc(job.next_run_time) if job and job.next_run_time else None
    with request.app.state.sessions() as session:
        return report_service.effectiveness(session, window, next_watch_sync=next_watch_sync)
