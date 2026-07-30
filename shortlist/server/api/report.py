"""Effectiveness report: is Shortlist actually getting watched?

All of it comes from ``picks.watched_at`` (set when a delivered pick turns up in the person's watch
history), joined against runs, collections and the request queue. A "recommendation" is a distinct
(user, title) pair — a title recommended to one person — and it's a "hit" once that person watches it.
Distinct titles, not pick rows: a title re-recommended over several runs is one recommendation, and one
watch is one hit (counting rows would skew both). Owner-only, read-only.

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

import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session

from shortlist.engine.models import DEFAULT_ROW_TEMPLATE
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    Event,
    PickRow,
    RequestCandidate,
    Run,
    RunUser,
    User,
    iso_utc,
)
from shortlist.server.scheduler import WATCH_SYNC_JOB_ID
from shortlist.server.services.run_service import HIT_WINDOW_DAYS
from shortlist.server.settings_store import SettingsStore

_PLACEHOLDER = re.compile(r"\{[^}]+\}")

#: Selectable report windows, in days. ``None`` = all time.
WINDOWS: dict[str, int | None] = {"7": 7, "30": 30, "90": 90, "all": None}
DEFAULT_WINDOW = "30"

#: How many weekly buckets the trend chart carries. The trend is the LONG view — it deliberately
#: ignores the window selector, because a 7-day window would leave it with one bar.
TREND_WEEKS = 16

router = APIRouter(prefix="/report", tags=["report"], dependencies=[Depends(require_owner)])


@router.post("/sync", status_code=202)
async def trigger_sync(request: Request) -> dict:
    """Run the daily watch-status sync on demand — refresh every user's watched picks now. Fires in
    the background (it fetches history for all users); the report refreshes once it lands."""
    request.app.state.run_service.sync_watched_background()
    return {"started": True}


def _rate(watched: int, delivered: int) -> float | None:
    return round(watched / delivered, 3) if delivered else None


def _delta(current: int | float | None, previous: int | float | None) -> float | None:
    """Change vs the previous equal-length period, or None when there is nothing to compare against
    (the ``all`` window, or a stat that was null in either period)."""
    if current is None or previous is None:
        return None
    return round(current - previous, 1)


def _orphaned_slugs(session: Session) -> set[str]:
    """Row slugs that appear in `picks` but have no live Collection behind them.

    Computed server-side, the same way the report labels a line "deleted", so a client cannot ask us
    to purge a row that still exists by passing its slug.
    """
    live = {c.slug for c in session.query(Collection.slug).all()} | {"", DEFAULT_SLUG}
    used = {slug for (slug,) in session.query(PickRow.collection_slug).distinct().all()}
    return {slug for slug in used if slug not in live}


@router.get("/deleted-rows")
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


@router.delete("/deleted-rows")
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


@router.get("")
async def effectiveness(
    request: Request,
    window: str = Query(DEFAULT_WINDOW, description="Report window in days: 7, 30, 90, or 'all'."),
) -> dict:
    """The dashboard tracking report: headline counts for the chosen window with their change vs the
    previous equal period, the landing rate over a matured cohort, watch momentum, per-user and
    per-row breakdowns, the titles landing best, requests that paid off, and a recent-watches feed."""
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    days = WINDOWS[window]
    now = datetime.now(UTC)

    # The current period is [since, now); the previous is [prev_since, since). Both are None for
    # "all", which has no previous period and therefore no deltas.
    since = now - timedelta(days=days) if days is not None else None
    prev_since = now - timedelta(days=2 * days) if days is not None else None

    with request.app.state.sessions() as session:
        # A title within one person's set: (tmdb_id, media_type). `.concat()` (SQL `||`), never
        # func.concat — the latter needs SQLite >= 3.44 and the runtime image ships 3.40.
        title = cast(PickRow.tmdb_id, String).concat("-").concat(PickRow.media_type)
        # A title across everyone: prefix the person, so one film recommended to two people counts twice.
        person_title = cast(PickRow.user_id, String).concat("-").concat(title)

        def in_period(column, start, end=None):
            """Filters restricting `column` to [start, end). Empty when start is None (all time)."""
            clauses = []
            if start is not None:
                clauses.append(column >= start)
            if end is not None:
                clauses.append(column < end)
            return clauses

        def watched_in(start, end=None):
            """Filters for picks WATCHED in [start, end) — the numerator side of every count here."""
            return [PickRow.watched_at.isnot(None), *in_period(PickRow.watched_at, start, end)]

        def counts(group_cols, key_expr, start):
            """{group key -> (delivered, watched)} distinct-title counts for one period, in two scans.

            `delivered` counts picks CREATED in the period; `watched` counts picks WATCHED in it —
            deliberately not the same set. A pick delivered last month and watched this week is a
            watch this week, and pinning it to its delivery period would hide it entirely. These are
            counts, never a ratio, so the mismatch is honest rather than misleading.
            """
            cols = list(group_cols) if isinstance(group_cols, (list, tuple)) else [group_cols]

            def scan(*extra):
                rows = session.query(*cols, func.count(func.distinct(key_expr))).filter(*extra).group_by(*cols).all()
                return {(r[:-1] if len(cols) > 1 else r[0]): r[-1] for r in rows}

            delivered = scan(*in_period(PickRow.created_at, start))
            watched = scan(*watched_in(start))
            return {k: (delivered.get(k, 0), watched.get(k, 0)) for k in set(delivered) | set(watched)}

        per_user_raw = counts(PickRow.user_id, title, since)
        # A row that targets >1 library is one Plex collection PER library, so it's tracked per
        # (row, library) — each library gets its own delivered/watched line, keyed (slug, section, library).
        per_row_raw = counts([PickRow.collection_slug, PickRow.section_key, PickRow.library], person_title, since)

        def watched_count(start, end=None) -> int:
            return session.query(func.count(func.distinct(person_title))).filter(*watched_in(start, end)).scalar() or 0

        def watchers_count(start, end=None) -> int:
            return (
                session.query(func.count(func.distinct(PickRow.user_id))).filter(*watched_in(start, end)).scalar() or 0
            )

        def avg_days_to_watch(start, end=None) -> float | None:
            """Average days from FIRST delivery to FIRST watch, over titles first watched in the period.

            Per (user, title), not per delivery row — a title re-recommended nightly is one data point,
            measured from when it was first added (MIN created_at) to when it was first watched.
            """
            firsts = (
                session.query(
                    func.min(PickRow.created_at).label("added"),
                    func.min(PickRow.watched_at).label("watched"),
                )
                .group_by(PickRow.user_id, PickRow.tmdb_id, PickRow.media_type)
                .subquery()
            )
            clauses = [firsts.c.watched.isnot(None), *in_period(firsts.c.watched, start, end)]
            value = (
                session.query(func.avg(func.julianday(firsts.c.watched) - func.julianday(firsts.c.added)))
                .filter(*clauses)
                .scalar()
            )
            return round(value, 1) if value is not None else None

        watched_now = watched_count(since)
        watchers_now = watchers_count(since)
        avg_now = avg_days_to_watch(since)
        watched_prev = watched_count(prev_since, since) if since else None
        watchers_prev = watchers_count(prev_since, since) if since else None
        avg_prev = avg_days_to_watch(prev_since, since) if since else None

        delivered_now = (
            session.query(func.count(func.distinct(person_title)))
            .filter(*in_period(PickRow.created_at, since))
            .scalar()
            or 0
        )

        # --- The landing rate, over a MATURED cohort -------------------------------------------
        # The equally-long window ending `HIT_WINDOW_DAYS` ago — i.e. the most recent stretch of
        # picks that have all had their full 30 days. Anything younger cannot have been credited yet,
        # so including it would recreate the very bug this rewrite exists to fix, inside a smaller
        # box. Note this is a SHIFTED window, not an intersection with the selected one: for
        # `window=30` the cohort is [now-60d, now-30d). The UI prints `cohort_from`/`cohort_to` so the
        # dates are never left to be inferred.
        matured_until = now - timedelta(days=HIT_WINDOW_DAYS)
        matured_since = matured_until - timedelta(days=days) if days is not None else None
        cohort = in_period(PickRow.created_at, matured_since, matured_until)
        cohort_delivered = session.query(func.count(func.distinct(person_title))).filter(*cohort).scalar() or 0
        cohort_watched = (
            session.query(func.count(func.distinct(person_title)))
            .filter(PickRow.watched_at.isnot(None), *cohort)
            .scalar()
            or 0
        )
        landing = {
            "delivered": cohort_delivered,
            "watched": cohort_watched,
            "rate": _rate(cohort_watched, cohort_delivered),
            "cohort_from": iso_utc(matured_since) if matured_since else None,
            "cohort_to": iso_utc(matured_until),
            "matured_days": HIT_WINDOW_DAYS,
        }

        # The trend ignores the window on purpose — see TREND_WEEKS.
        trend_rows = (
            session.query(func.strftime("%Y-%W", PickRow.watched_at), func.count(func.distinct(person_title)))
            .filter(PickRow.watched_at.isnot(None))
            .group_by(func.strftime("%Y-%W", PickRow.watched_at))
            .order_by(func.strftime("%Y-%W", PickRow.watched_at).desc())
            .limit(TREND_WEEKS)
            .all()
        )
        trend_rows = list(reversed(trend_rows))

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

        def row_template(slug: str) -> str | None:
            """This row's name template, or None once the row itself is gone.

            Picks outlive the row that made them (deleting a row keeps its watch history), and a slug
            with no Collection behind it must NOT borrow the default row's template — every deleted row
            then renders "✨ Movies Picked for You" and the breakdown shows the default row two, three,
            five times over with different numbers. The caller labels those by slug instead.
            """
            if slug in row_templates:
                return row_templates[slug]
            # Pre-0004 picks predate multi-row and carry a blank slug; they ARE the default row.
            return default_template if slug in ("", DEFAULT_SLUG) else None

        def row_label(slug: str, library: str) -> str:
            """The row's display name for THIS library: `{library_name}` becomes the library ("Movies"),
            and any other placeholder (e.g. `{top_seed}`, which is per-person) is dropped for the aggregate.
            A deleted row has no template left, so its slug is the only identity it still has."""
            template = row_template(slug)
            if template is None:
                return slug
            name = _PLACEHOLDER.sub(lambda m: library if m.group(0) == "{library_name}" else "", template)
            return " ".join(name.split()) or "Picked for You"

        # Reach: who's actually covered. `users_enabled`/`rows_enabled` describe the server as it is
        # NOW, so they are deliberately not windowed — "3 of 11 people" only reads if 11 is current.
        users_enabled = sum(1 for u in users.values() if u.enabled)
        users_with_picks = (
            session.query(func.count(func.distinct(PickRow.user_id)))
            .filter(*in_period(PickRow.created_at, since))
            .scalar()
            or 0
        )
        rows_enabled = session.query(func.count(Collection.id)).filter(Collection.enabled.is_(True)).scalar() or 0

        # Runs. `total` stays all-time (it is the odometer); `in_window` is what the delta is about.
        runs_total = session.query(func.count(Run.id)).scalar() or 0
        runs_in_window = session.query(func.count(Run.id)).filter(*in_period(Run.started_at, since)).scalar() or 0
        runs_prev = (
            session.query(func.count(Run.id)).filter(*in_period(Run.started_at, prev_since, since)).scalar() or 0
            if since
            else None
        )
        last_run = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
        errors_last = (
            session.query(func.count(RunUser.user_id))
            .filter(RunUser.run_id == last_run.id, RunUser.status == "error")
            .scalar()
            if last_run
            else 0
        )

        # Requests that paid off: titles asked of Sonarr/Radarr that were LATER watched by someone.
        #
        # "Later" is the point. This used to be a plain set intersection with no ordering check, so a
        # title watched, then deleted from the library, then re-requested counted as a request that
        # paid off. It now compares against `sent_at`, stamped once when the status flips to "sent".
        #
        # `updated_at` is the fallback for rows sent before that column existed. It is a poor proxy —
        # it has `onupdate`, so clearing an old title from the Sent log bumps it — but it is what
        # those rows have, and it is no worse than the behaviour they already had.
        def sent_time(row) -> datetime | None:
            return _as_utc(row.sent_at or row.updated_at) if (row.sent_at or row.updated_at) else None

        sent_rows = [
            row
            for row in session.query(RequestCandidate).filter(RequestCandidate.status == "sent").all()
            if (when := sent_time(row)) is not None and (since is None or when >= since)
        ]
        sent = {(row.tmdb_id, row.media_type): sent_time(row) for row in sent_rows}
        watched_at_by_title: dict[tuple[int, str], datetime] = {}
        for tid, mt, watched in (
            session.query(PickRow.tmdb_id, PickRow.media_type, func.max(PickRow.watched_at))
            .filter(PickRow.watched_at.isnot(None))
            .group_by(PickRow.tmdb_id, PickRow.media_type)
            .all()
        ):
            watched_at_by_title[(tid, mt)] = watched
        paid_off = sum(
            1
            for key, sent_at in sent.items()
            if (watched := watched_at_by_title.get(key)) is not None
            and (sent_at is None or _as_utc(watched) >= sent_at)
        )
        requests = {
            "sent": len(sent),
            "pending": session.query(func.count(RequestCandidate.id)).filter_by(status="pending").scalar() or 0,
            "watched_after_sent": paid_off,
        }

        # The titles landing best: most distinct watchers among picks watched in the window.
        top_rows = (
            session.query(
                PickRow.tmdb_id,
                PickRow.media_type,
                func.max(PickRow.title),
                func.count(func.distinct(PickRow.user_id)),
            )
            .filter(*watched_in(since))
            .group_by(PickRow.tmdb_id, PickRow.media_type)
            .order_by(func.count(func.distinct(PickRow.user_id)).desc())
            .limit(8)
            .all()
        )

        def _breakdown(raw, label):
            """Counts, sorted by what was actually watched.

            Sorting by rate is what put `1/31` above `3/103` — a single data point outranking three
            times the evidence. Watched count first, delivered as the tiebreak, so the list reads as
            "who is getting the most out of this".
            """
            return sorted(
                ({**label(key), "delivered": d, "watched": w} for key, (d, w) in raw.items() if label(key) is not None),
                key=lambda r: (r["watched"], r["delivered"]),
                reverse=True,
            )

        per_user = _breakdown(
            per_user_raw,
            lambda uid: (
                {
                    "username": users[uid].username,
                    "display_name": users[uid].display_name,  # nickname → Tautulli → username
                    "slug": users[uid].slug,
                }
                if uid in users
                else None
            ),
        )
        per_row = _breakdown(
            per_row_raw,
            lambda key: {
                "slug": key[0] or DEFAULT_SLUG,
                "section_key": key[1],
                "library": key[2],
                "name": row_label(key[0], key[2]),
                # History from a row that no longer exists. Flagged so the UI can collapse it out of
                # the way rather than showing it as another nameless copy of the default row.
                "deleted": row_template(key[0]) is None,
            },
        )

        # One line per (person, title), like every other figure here — NOT one per pick row. A title
        # re-recommended over several runs has one pick row per run, and the watch-sync stamps the same
        # watched_at on every one of them, so the raw feed repeated a single watch once per delivery
        # ("Jarrah watched Beckham" five times). Credit the newest delivery (its row/library label is
        # the current one) at the latest time that person watched it.
        latest = (
            session.query(
                func.max(PickRow.id).label("pick_id"),
                func.max(PickRow.watched_at).label("watched"),
            )
            .filter(*watched_in(since))
            .group_by(PickRow.user_id, PickRow.tmdb_id, PickRow.media_type)
            .order_by(func.max(PickRow.watched_at).desc())
            .limit(20)
            .subquery()
        )
        recent = [
            {
                "username": users[p.user_id].username if p.user_id in users else "unknown",
                "display_name": users[p.user_id].display_name if p.user_id in users else "unknown",
                "title": p.title,
                "media_type": p.media_type,
                "row": row_label(p.collection_slug, p.library),
                "library": p.library,
                "seed_title": p.seed_title or "",
                "watched_at": iso_utc(watched),
            }
            for p, watched in session.query(PickRow, latest.c.watched)
            .join(latest, PickRow.id == latest.c.pick_id)
            .order_by(latest.c.watched.desc())
            .all()
        ]

    # When the daily watch-sync last ran and next fires (so the owner can see the report is live).
    scheduler = getattr(request.app.state, "scheduler", None)
    job = scheduler.get_job(WATCH_SYNC_JOB_ID) if scheduler else None
    next_watch_sync = iso_utc(job.next_run_time) if job and job.next_run_time else None

    return {
        "window": window,
        "window_days": days,
        "since": iso_utc(since) if since else None,
        "overall": {
            "delivered": delivered_now,
            "watched": watched_now,
            "watched_prev": watched_prev,
            "watched_delta": _delta(watched_now, watched_prev),
            "avg_days_to_watch": avg_now,
            "avg_days_to_watch_delta": _delta(avg_now, avg_prev),
            "landing": landing,
        },
        "watch_sync": {"last": last_watch_sync, "next": next_watch_sync},
        "coverage": {
            "users_enabled": users_enabled,
            "users_total": len(users),
            "users_with_picks": users_with_picks,
            "users_watched": watchers_now,
            "users_watched_delta": _delta(watchers_now, watchers_prev),
            "rows_enabled": rows_enabled,
        },
        "runs": {
            "total": runs_total,
            "in_window": runs_in_window,
            "in_window_delta": _delta(runs_in_window, runs_prev),
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


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes for some columns and aware ones for others; comparing the
    two raises. Treat naive as UTC, which is what every writer in this app stores."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)
