"""Effectiveness report: is Shortlist actually getting watched?

All of it comes from ``picks.watched_at`` (set when a delivered pick turns up in the person's watch
history), joined against runs, collections and the request queue. A "recommendation" is a distinct
(user, title) pair — a title recommended to one person — and it's a "hit" once that person watches it.
Distinct titles, not pick rows: a title re-recommended over several runs is one recommendation, and one
watch is one hit (counting rows would skew both). Owner-only, read-only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import String, cast, func

from shortlist.engine.models import DEFAULT_ROW_TEMPLATE
from shortlist.server.auth import require_owner
from shortlist.server.db.models import DEFAULT_SLUG, Collection, PickRow, RequestCandidate, Run, RunUser, User, iso_utc
from shortlist.server.scheduler import WATCH_SYNC_JOB_ID
from shortlist.server.settings_store import SettingsStore

_PLACEHOLDER = re.compile(r"\{[^}]+\}")

router = APIRouter(prefix="/report", tags=["report"], dependencies=[Depends(require_owner)])


@router.post("/sync", status_code=202)
async def trigger_sync(request: Request) -> dict:
    """Run the daily watch-status sync on demand — refresh every user's watched picks now. Fires in
    the background (it fetches history for all users); the report refreshes once it lands."""
    request.app.state.run_service.sync_watched_background()
    return {"started": True}


def _rate(watched: int, delivered: int) -> float | None:
    return round(watched / delivered, 3) if delivered else None


@router.get("")
async def effectiveness(request: Request) -> dict:
    """The dashboard tracking report: headline hit rate + reach, watch momentum, per-user and per-row
    breakdowns, the titles landing best, requests that paid off, and a recent-watches feed."""
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    with request.app.state.sessions() as session:
        # A title within one person's set: (tmdb_id, media_type). `.concat()` (SQL `||`), never
        # func.concat — the latter needs SQLite >= 3.44 and the runtime image ships 3.40.
        title = cast(PickRow.tmdb_id, String).concat("-").concat(PickRow.media_type)
        # A title across everyone: prefix the person, so one film recommended to two people counts twice.
        person_title = cast(PickRow.user_id, String).concat("-").concat(title)

        def counts(group_cols, key_expr):
            """{group key -> (delivered, watched)} distinct-title counts, in two grouped scans. Pass one
            column for scalar keys, or several for tuple keys (grouped by every column together)."""
            cols = list(group_cols) if isinstance(group_cols, (list, tuple)) else [group_cols]

            def scan(*extra):
                rows = session.query(*cols, func.count(func.distinct(key_expr))).filter(*extra).group_by(*cols).all()
                return {(r[:-1] if len(cols) > 1 else r[0]): r[-1] for r in rows}

            delivered, watched = scan(), scan(PickRow.watched_at.isnot(None))
            return {k: (delivered.get(k, 0), watched.get(k, 0)) for k in delivered}

        per_user_raw = counts(PickRow.user_id, title)
        # A row that targets >1 library is one Plex collection PER library, so it's tracked per
        # (row, library) — each library gets its own delivered/watched line, keyed (slug, section, library).
        per_row_raw = counts([PickRow.collection_slug, PickRow.section_key, PickRow.library], person_title)

        delivered_total = sum(d for d, _ in per_user_raw.values())
        watched_total = sum(w for _, w in per_user_raw.values())

        watched_last_7d = (
            session.query(func.count(func.distinct(person_title)))
            .filter(PickRow.watched_at.isnot(None), PickRow.watched_at >= week_ago)
            .scalar()
            or 0
        )
        # Average days from FIRST delivery to FIRST watch, per (user, title) — not per delivery row, so
        # a title re-recommended nightly is one data point measured from when it was first added (MIN
        # created_at over all its rows) to when it was first watched (MIN watched_at). SQLite julianday.
        firsts = (
            session.query(
                func.min(PickRow.created_at).label("added"),
                func.min(PickRow.watched_at).label("watched"),
            )
            .group_by(PickRow.user_id, PickRow.tmdb_id, PickRow.media_type)
            .subquery()
        )
        avg_days = (
            session.query(func.avg(func.julianday(firsts.c.watched) - func.julianday(firsts.c.added)))
            .filter(firsts.c.watched.isnot(None))
            .scalar()
        )

        trend_rows = (
            session.query(func.strftime("%Y-%W", PickRow.watched_at), func.count(func.distinct(person_title)))
            .filter(PickRow.watched_at.isnot(None))
            .group_by(func.strftime("%Y-%W", PickRow.watched_at))
            .order_by(func.strftime("%Y-%W", PickRow.watched_at))
            .all()
        )

        store = SettingsStore(session)
        last_watch_sync = store.get("report.watch_synced_at")  # when the daily job last ran
        users = {u.id: u for u in session.query(User).all()}
        # The name template per row: the row's own template, the default row falling back to the global
        # one (the per-user override tier of engine `resolve_row_template` is dropped for this aggregate
        # label, and a custom row uses its stored name). Rendered per library below.
        default_template = store.get("row.name_template") or DEFAULT_ROW_TEMPLATE
        row_templates = {
            c.slug: (c.name_template or (default_template if c.slug == DEFAULT_SLUG else c.name))
            for c in session.query(Collection).all()
        }

        def row_label(slug: str, library: str) -> str:
            """The row's display name for THIS library: `{library_name}` becomes the library ("Movies"),
            and any other placeholder (e.g. `{top_seed}`, which is per-person) is dropped for the aggregate."""
            template = row_templates.get(slug, DEFAULT_ROW_TEMPLATE)
            name = _PLACEHOLDER.sub(lambda m: library if m.group(0) == "{library_name}" else "", template)
            return " ".join(name.split()) or "Picked for You"

        # Reach: who's actually covered.
        users_enabled = sum(1 for u in users.values() if u.enabled)
        users_with_picks = session.query(func.count(func.distinct(PickRow.user_id))).scalar() or 0
        rows_enabled = session.query(func.count(Collection.id)).filter(Collection.enabled.is_(True)).scalar() or 0

        # Runs summary.
        runs_total = session.query(func.count(Run.id)).scalar() or 0
        last_run = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
        errors_last = (
            session.query(func.count(RunUser.user_id))
            .filter(RunUser.run_id == last_run.id, RunUser.status == "error")
            .scalar()
            if last_run
            else 0
        )

        # Requests that paid off: auto/hand-sent titles that were later watched by anyone.
        sent_keys = {(r.tmdb_id, r.media_type) for r in session.query(RequestCandidate).filter_by(status="sent").all()}
        watched_keys = {
            (tid, mt)
            for tid, mt in session.query(PickRow.tmdb_id, PickRow.media_type)
            .filter(PickRow.watched_at.isnot(None))
            .distinct()
            .all()
        }
        requests = {
            "sent": len(sent_keys),
            "pending": session.query(func.count(RequestCandidate.id)).filter_by(status="pending").scalar() or 0,
            "watched_after_sent": len(sent_keys & watched_keys),
        }

        # The titles landing best: most distinct watchers among delivered picks.
        top_rows = (
            session.query(
                PickRow.tmdb_id,
                PickRow.media_type,
                func.max(PickRow.title),
                func.count(func.distinct(PickRow.user_id)),
            )
            .filter(PickRow.watched_at.isnot(None))
            .group_by(PickRow.tmdb_id, PickRow.media_type)
            .order_by(func.count(func.distinct(PickRow.user_id)).desc())
            .limit(8)
            .all()
        )

        def _breakdown(raw, label):
            return sorted(
                (
                    {**label(key), "delivered": d, "watched": w, "hit_rate": _rate(w, d)}
                    for key, (d, w) in raw.items()
                    if label(key) is not None
                ),
                key=lambda r: (r["hit_rate"] is not None, r["hit_rate"] or 0, r["watched"]),
                reverse=True,
            )

        per_user = _breakdown(
            per_user_raw,
            lambda uid: {"username": users[uid].username, "slug": users[uid].slug} if uid in users else None,
        )
        per_row = _breakdown(
            per_row_raw,
            lambda key: {
                "slug": key[0] or "picked",
                "section_key": key[1],
                "library": key[2],
                "name": row_label(key[0], key[2]),
            },
        )

        recent = [
            {
                "username": users[p.user_id].username if p.user_id in users else "unknown",
                "title": p.title,
                "media_type": p.media_type,
                "row": row_label(p.collection_slug, p.library),
                "library": p.library,
                "seed_title": p.seed_title or "",
                "watched_at": iso_utc(p.watched_at),
            }
            for p in session.query(PickRow)
            .filter(PickRow.watched_at.isnot(None))
            .order_by(PickRow.watched_at.desc())
            .limit(20)
            .all()
        ]

    # When the daily watch-sync last ran and next fires (so the owner can see the report is live).
    scheduler = getattr(request.app.state, "scheduler", None)
    job = scheduler.get_job(WATCH_SYNC_JOB_ID) if scheduler else None
    next_watch_sync = iso_utc(job.next_run_time) if job and job.next_run_time else None

    return {
        "overall": {
            "delivered": delivered_total,
            "watched": watched_total,
            "hit_rate": _rate(watched_total, delivered_total),
            "watched_last_7d": watched_last_7d,
            "avg_days_to_watch": round(avg_days, 1) if avg_days is not None else None,
        },
        "watch_sync": {"last": last_watch_sync, "next": next_watch_sync},
        "coverage": {
            "users_enabled": users_enabled,
            "users_total": len(users),
            "users_with_picks": users_with_picks,
            "rows_enabled": rows_enabled,
        },
        "runs": {
            "total": runs_total,
            "last_finished": iso_utc(last_run.finished_at) if last_run else None,
            "last_status": last_run.status if last_run else None,
            "errors_last": errors_last or 0,
        },
        "requests": requests,
        "trend": [{"week": week, "watched": n} for week, n in trend_rows],
        "per_user": per_user,
        "per_row": per_row,
        "top_titles": [{"tmdb_id": tid, "media_type": mt, "title": ttl, "watchers": n} for tid, mt, ttl, n in top_rows],
        "recent": recent,
    }
