"""Early "Your requests" updates (#191): refresh a requests row soon after a request lands on Plex.

A scheduler job calls `tick` every few minutes. It reads Overseerr's recent completions, works out which
(request, requests row, person) triples still need an update, and, behind a batching gate, starts one ordinary
run. There is no state table: what is owed is derived from `runs` and `run_users`, so a restart forgets nothing.
The poll only reads Overseerr and calls `start_run`; it never writes to Plex.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.clients.seerr import SeerrClient
from shortlist.engine.models import RequestSources, UserProfile
from shortlist.engine.requests_row import _iso, seerr_identity, seerr_request_title
from shortlist.server.db.models import Collection, Run, RunUser, User
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.scheduler import crontab_trigger
from shortlist.server.services.audit import add_audit
from shortlist.server.services.theme_rotation import rotating_users, row_media
from shortlist.server.settings_store import SettingsStore

#: A request must sit this long before a run starts, so a burst of completions becomes one run.
SETTLE_MIN = 10
#: ...but never wait longer than this for the oldest one, however steady the trickle.
MAX_WAIT_MIN = 30
LOOKBACK = timedelta(hours=24)
#: Early runs spent on one (request, row, person) before giving up and leaving it to the nightly run.
MAX_EARLY_RUNS = 3
#: Verdicts that say "not there yet", so a later run may still add the title.
WAITING_VERDICTS = frozenset({"not_on_plex", "season_not_landed"})

_TRIGGER = "request_landed"
_MIN_GAP_KEY = "requests.landed_min_gap_min"
_DEFAULT_MIN_GAP_MIN = 60
_BUSY = ("queued", "running")


@dataclass(frozen=True)
class Owed:
    """One completed request that a requests row may still need to show for one person."""

    request_id: int
    tmdb_id: int
    completed_at: datetime
    collection_id: int
    user_id: int


def _now() -> datetime:
    return datetime.now(UTC)


def _utc(moment: datetime) -> datetime:
    """The database hands back naive UTC; make it comparable with aware times."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _early_rows(session: Session) -> list[Collection]:
    return list(
        session.scalars(
            select(Collection)
            .where(
                Collection.enabled.is_(True),
                Collection.requests_row.is_(True),
                Collection.requests_early_refresh.is_(True),
            )
            .order_by(Collection.id)
        )
    )


def landings(
    session: Session,
    completed: list[dict],
    sources: RequestSources,
    people: list[UserProfile],
    db_ids: dict[int, int],
    read: dict[int, int | None],
) -> list[Owed]:
    """Pair each completed request with every requests row that should now show it for its requester.

    Args:
        session: Database session.
        completed: Overseerr's recently completed requests.
        sources: Where requests are read; supplies the account whose requests are never a person's.
        people: The enabled roster.
        db_ids: Plex account id -> ``users.id``.
        read: Overseerr user id -> ``plexId``.

    Returns:
        One `Owed` per (request, row, person); requests that match nobody are left out.
    """
    by_plex = {p.plex_account_id: p for p in people}
    seerr_plex, chosen = seerr_identity(people, read)
    rows = [(row, {u.id for u in rotating_users(session, row)}) for row in _early_rows(session)]
    out: list[Owed] = []
    for r in completed:
        title = seerr_request_title(r, seerr_plex, chosen, by_plex, sources.exclude_seerr_user_id)
        completed_at = _iso(r.get("updatedAt"))
        user_id = db_ids.get(title.plex_account_id) if title else None
        if title is None or completed_at is None or user_id is None or not isinstance(r.get("id"), int):
            continue
        for row, audience in rows:
            if user_id in audience and title.media_type in row_media(row):
                out.append(Owed(r["id"], title.tmdb_id, completed_at, row.id, user_id))
    return out


def _still_waiting(trace: dict | None, row_slug: str, tmdb_id: int) -> bool:
    """Whether the run's trace left the title out only because it was not there yet."""
    verdicts = [
        entry["result"]
        for sel in (trace or {}).get("selection", [])
        if sel.get("row") == row_slug
        for entry in sel.get("requests", [])
        if entry.get("tmdb_id") == tmdb_id
    ]
    return bool(verdicts) and all(v in WAITING_VERDICTS for v in verdicts)


def still_owed(session: Session, owed: list[Owed]) -> list[Owed]:
    """The entries no run has dealt with yet, judged from the runs table alone.

    A run covers an entry when it matches this instance's mode (safe mode runs are all dry, live mode's are real,
    so an owner's manual dry run settles nothing), started after the request completed and named both
    the row and the person.
    An entry is settled by a covering run that is still in flight, that failed (an early run only; the nightly
    one will try again), that left the title out for a reason that will not change, or by `MAX_EARLY_RUNS`
    early runs having already tried.
    """
    if not owed:
        return []
    row_slugs = {i: slug for i, slug in session.execute(select(Collection.id, Collection.slug))}
    user_slugs = {i: slug for i, slug in session.execute(select(User.id, User.slug))}
    # `runs.started_at` is stored as naive UTC, so compare like with like.
    naive_since = min(o.completed_at for o in owed).astimezone(UTC).replace(tzinfo=None)
    runs = [
        (
            run,
            {x["slug"] for x in run.stats.get("expected_rows", [])},
            {x["slug"] for x in run.stats.get("expected_users", [])},
        )
        for run in session.scalars(
            select(Run).where(Run.dry_run.is_(force_dry_run()), Run.started_at > naive_since).order_by(Run.started_at)
        )
    ]
    traces: dict[tuple[int, int], dict | None] = {}

    def trace_of(run_id: int, user_id: int) -> dict | None:
        if (run_id, user_id) not in traces:
            row = session.get(RunUser, (run_id, user_id))
            traces[run_id, user_id] = row.trace if row else None
        return traces[run_id, user_id]

    left: list[Owed] = []
    for o in owed:
        row_slug, user_slug = row_slugs.get(o.collection_id), user_slugs.get(o.user_id)
        covering = [
            run
            for run, rows, users in runs
            if _utc(run.started_at) > o.completed_at and row_slug in rows and user_slug in users
        ]
        early = [run for run in covering if run.trigger == _TRIGGER]
        settled = (
            any(run.status in _BUSY for run in covering)
            or any(run.status in ("error", "aborted") for run in early)
            or len(early) >= MAX_EARLY_RUNS
            or any(
                run.status == "ok" and not _still_waiting(trace_of(run.id, o.user_id), row_slug, o.tmdb_id)
                for run in covering
            )
        )
        if not settled:
            left.append(o)
    return left


def _fires_within(cron: str, now: datetime, span: timedelta) -> bool:
    """Whether a row's own cron next fires inside ``span``; a blank or invalid cron never does."""
    if not cron.strip():
        return False
    try:
        trigger = crontab_trigger(cron)
    except ValueError:
        return False
    local = now.astimezone()
    nxt = trigger.get_next_fire_time(None, local)
    return nxt is not None and nxt - local <= span


def should_start(session: Session, owed: list[Owed], now: datetime, min_gap: timedelta) -> bool:
    """Whether to start the early run now: something is owed, nothing else is running, and the timing is right."""
    if not owed:
        return False
    if session.scalar(select(Run.id).where(Run.status.in_(_BUSY)).limit(1)) is not None:
        return False
    last_early = session.scalar(select(Run.started_at).where(Run.trigger == _TRIGGER).order_by(Run.started_at.desc()))
    if last_early is not None and now - _utc(last_early) < min_gap:
        return False
    newest = max(o.completed_at for o in owed)
    oldest = min(o.completed_at for o in owed)
    if now - newest < timedelta(minutes=SETTLE_MIN) and now - oldest < timedelta(minutes=MAX_WAIT_MIN):
        return False
    row_ids = {o.collection_id for o in owed}
    schedules = list(session.scalars(select(Collection.schedule).where(Collection.id.in_(row_ids))))
    return not (schedules and all(_fires_within(cron or "", now, min_gap) for cron in schedules))


async def tick(app) -> None:
    """One poll: read recent completions, and start a run if a request is owed and the gate allows it."""
    sessions = app.state.sessions
    with sessions() as session:
        if not _early_rows(session):
            return
    sources, people, db_ids = await asyncio.to_thread(app.state.run_service.build_request_sources_only)
    if sources is None or sources.overseerr is None:
        return
    now = _now()
    try:
        client = SeerrClient(sources.overseerr)
        completed = await asyncio.to_thread(client.recently_completed, now - LOOKBACK)
        if not completed:
            return
        read = await asyncio.to_thread(client.user_plex_ids)
    except Exception as e:
        logger.warning("requests row: could not read recent Overseerr completions ({})", e)
        return
    with sessions() as session:
        owed = still_owed(session, landings(session, completed, sources, people, db_ids, read))
        min_gap = timedelta(minutes=SettingsStore(session, app.state.secrets).get(_MIN_GAP_KEY, _DEFAULT_MIN_GAP_MIN))
        if not should_start(session, owed, now, min_gap):
            return
    user_ids = sorted({o.user_id for o in owed})
    collection_ids = sorted({o.collection_id for o in owed})
    run_id = await app.state.run_service.start_run(
        trigger=_TRIGGER, dry_run=False, user_ids=user_ids, collection_ids=collection_ids
    )
    logger.info("requests row: run {} started for {} request(s) that landed", run_id, len({o.request_id for o in owed}))
    with sessions() as session:
        add_audit(
            session,
            "requests.landed",
            "info",
            run_id=run_id,
            people=len(user_ids),
            requests=len({o.request_id for o in owed}),
        )
        session.commit()
