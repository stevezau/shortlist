"""Run persistence + audit — everything a finished run leaves behind in SQLite.

The run rows, per-user results, picks, the delivery ledger, the approval inbox, the `events` audit
trail (plex-safety rule 10) and the retention prunes that trim them. Written as plain functions
taking a session (or a session factory) so the retention pass can be driven by the
`maintenance.prune` job without going through a live `RunService`.

Nothing here talks to Plex. It reads a finished ``EngineReport`` and writes what it says.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.models import SHARED_SLUG_PREFIX
from shortlist.engine.requests import QUEUE_REASON_PREFIXES
from shortlist.server.db.models import (
    Delivery,
    Event,
    PickRow,
    RequestCandidate,
    Run,
    RunLogLine,
    RunUser,
    User,
)
from shortlist.server.services import jobs
from shortlist.server.services.audit import add_audit

HIT_WINDOW_DAYS = 30  # a pick counts as a hit if it is watched within 30 days of being recommended


def _record_deliveries(session: Session, user_slug: str, breakdown: list[dict]) -> None:
    """Upsert this user's delivery-ledger rows from a run's per-(row, library) breakdown.

    The ledger is what lets a later reconcile find a collection it cannot re-derive a title for (a
    `{top_seed}` row renders differently every run). It is written here, on the persist path, so it
    stays in step with what the run actually delivered.

    Entries with no `rating_key` are skipped: legacy breakdowns predate the field, and a dry run never
    reaches this function at all. A row without a key is simply not in the ledger, which leaves the
    reconciles exactly where they were before it existed — the fallback still applies.
    """
    for entry in breakdown or []:
        rating_key = int(entry.get("rating_key") or 0)
        slug, library_key = entry.get("row_slug") or "", str(entry.get("library_key") or "")
        if not rating_key or not slug or not library_key:
            continue
        row = session.get(Delivery, (slug, user_slug, library_key))
        if row is None:
            row = Delivery(collection_slug=slug, user_slug=user_slug, library_key=library_key)
            session.add(row)
        row.rating_key = rating_key
        row.title = entry.get("row_title") or ""
        row.updated_at = datetime.now(UTC)


def _forget_removed_deliveries(session: Session, user_slug: str, removed: list[dict]) -> None:
    """Drop the ledger rows for collections this run DELETED in-run (a muted/retired row, or one a
    cold start skipped), so the ledger stays a record of what IS on the server.

    The on-demand reconciles already do this via `collection_reconcile._forget_deliveries`; an in-run
    removal had no equivalent, so its ratingKey survived its collection. That matters because these
    paths REPEAT — a cold-skipped user is skipped again every night — and Plex reuses
    `metadata_items.id`, so a kept key can come to name a different collection. `promote_user_rows`
    reads this ledger too, so it is not only removals that a stale key would misdirect.
    """
    for entry in removed or []:
        slug, library_key = entry.get("row_slug") or "", str(entry.get("library_key") or "")
        if not slug or not library_key:
            continue
        row = session.get(Delivery, (slug, user_slug, library_key))
        if row is not None:
            session.delete(row)


def _why_json(why) -> list[dict]:
    """Serialize a missing title's provenance for storage + the API: [{user, row, seed, source}]."""
    return [{"user": w.user, "row": w.row, "seed": w.seed, "source": w.source} for w in why]


def _is_failure_detail(detail: str | None) -> bool:
    """Whether a detail describes a send that was ATTEMPTED and failed, rather than a threshold reason
    for one that was never attempted.

    The queue reasons are settings-derived and finite (`engine/requests.py`); anything else came from
    an exception while actually talking to Radarr/Sonarr, and is the fact worth keeping.
    """
    if not detail:
        return False
    return not detail.startswith(QUEUE_REASON_PREFIXES)


def _refresh_pending(row: RequestCandidate, m) -> None:
    """Update a still-pending inbox row with what this run now knows about the title."""
    row.title = m.title
    row.year = m.year
    row.imdb_id = m.imdb_id or row.imdb_id  # keep a known id if a later run couldn't re-fetch it
    # Same rule as imdb_id, and it is also what backfills rows queued before 0044: the first run to
    # re-surface the title fills the artwork in.
    row.poster_path = m.poster_path or row.poster_path
    row.rating = m.rating
    row.vote_count = m.vote_count
    row.demand = m.demand
    row.tags = sorted(m.tags)
    row.wanters = sorted(m.wanters)
    row.why = _why_json(m.why)
    # A queued title now always carries a reason ("max_per_run (5) already filled"), so a plain
    # `m.detail or row.detail` would overwrite yesterday's REAL failure ("Sonarr returned HTTP 503")
    # with today's threshold note — erasing the only record that Sonarr was broken. A failure detail
    # is the more important fact, so it survives until a send actually succeeds.
    if m.detail and (not _is_failure_detail(row.detail) or _is_failure_detail(m.detail)):
        row.detail = m.detail
    row.excluded = m.excluded  # refresh the exclusion flag each run (a removed exclusion clears it)


def _candidate_row(m, run_id: int, *, status: str) -> RequestCandidate:
    """One inbox row for a missing title, in whichever state the run left it."""
    return RequestCandidate(
        tmdb_id=m.tmdb_id,
        media_type=m.media_type.value,
        title=m.title,
        year=m.year,
        imdb_id=m.imdb_id,
        poster_path=m.poster_path,
        rating=m.rating,
        vote_count=m.vote_count,
        demand=m.demand,
        tags=sorted(m.tags),
        wanters=sorted(m.wanters),
        why=_why_json(m.why),
        status=status,
        detail=m.detail,  # why it is not here yet: a threshold, or a real send failure
        excluded=m.excluded,  # on a Sonarr/Radarr exclusion list — flagged in the inbox
        arr_slug=m.arr_slug,  # set for auto-sent titles, so the inbox deep-links to the arr page
        first_seen_run_id=run_id,
    )


def reconcile_watched(sessions: sessionmaker[Session], profiles) -> None:
    """Mark the picks a person actually watched — the hit rate, and the whole point of the app.

    `picks.watched_at` was declared, migrated and read by the hit-rate query, but never WRITTEN:
    every user's hit rate was structurally 0%, while the docs promised "expect 20-40%".

    A pick counts as a hit only when the watch happened AFTER we recommended it (the run that
    produced it) and within 30 days — recommending something they had already seen isn't a hit,
    and neither is a watch a year later. `history_depth` is refreshed here too; it was likewise
    surfaced in the UI and written nowhere, so every user read "0 titles watched".
    """
    with sessions() as session:
        for profile in profiles:
            user = session.query(User).filter_by(slug=profile.slug).first()
            if user is None:
                continue
            # Only when this pass actually READ their history. A scoped run — "the common case,
            # not the rare one" per `WatchSync.has_a_row_in_scope` — leaves every out-of-scope profile
            # with an empty list, and writing that reintroduced the exact bug the docstring above
            # says this line fixed: "every user read 0 titles watched", now for the majority of
            # people on most nights. An empty history with no prior value still writes 0, which
            # is the truth for someone who genuinely has none.
            if profile.history or "history_depth" not in (user.prefs or {}):
                user.prefs = {**(user.prefs or {}), "history_depth": len(profile.history)}

            latest_watch: dict[tuple[int, str], datetime] = {}
            for item in profile.history:
                if item.tmdb_id is None:
                    continue
                key = (item.tmdb_id, str(item.media_type))
                when = item.watched_at if item.watched_at.tzinfo else item.watched_at.replace(tzinfo=UTC)
                if key not in latest_watch or when > latest_watch[key]:
                    latest_watch[key] = when
            if not latest_watch:
                continue

            # Only picks recent enough to still be creditable: a pick older than the window can
            # never become a hit, so scanning every unwatched pick ever recorded is dead work
            # that grows without bound. Uses the pick's own created_at (when it was delivered),
            # not the run's started_at — so picks that outlive their run (after clear/prune) are
            # still creditable.
            cutoff = datetime.now(UTC) - timedelta(days=HIT_WINDOW_DAYS)
            unwatched = (
                session.query(PickRow)
                .filter(
                    PickRow.user_id == user.id,
                    PickRow.watched_at.is_(None),
                    PickRow.created_at >= cutoff,
                )
                .all()
            )
            for pick in unwatched:
                watched = latest_watch.get((pick.tmdb_id, pick.media_type))
                if watched is None:
                    continue
                since = pick.created_at if pick.created_at.tzinfo else pick.created_at.replace(tzinfo=UTC)
                if since <= watched <= since + timedelta(days=HIT_WINDOW_DAYS):
                    pick.watched_at = watched
        session.commit()


def persist_user_live(
    sessions: sessionmaker[Session],
    persist_lock: threading.Lock,
    run_id: int,
    profile,
    user_report,
    dry_run: bool,
) -> None:
    """Persist ONE user's results as they finish (called from the engine's worker threads), so the
    run page shows each person on completion rather than the whole roster only at run's end. Its
    commits are serialized (SQLite single-writer) and it never re-writes a user already stored, so
    the end-of-run `persist_report` stays a safe backstop + reconciler. A shared-row/unknown slug
    has no user row here and is handled only at run end."""
    with persist_lock, sessions() as session:
        user = session.query(User).filter_by(slug=profile.slug).first()
        if user is None:
            return
        if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is not None:
            return
        _persist_user_report(session, run_id, user, user_report, dry_run)
        session.commit()


def persist_report(
    sessions: sessionmaker[Session], run_id: int, report, *, status: str | None = None, error: str | None = None
) -> None:
    """Persist a run's outcome. `status`/`error` override what the report says — the gated
    path uses them so a refused run is never even momentarily written as a success."""
    with sessions() as session:
        run = session.get(Run, run_id)
        users_by_slug = {u.slug: u for u in session.query(User).all()}
        # Skipped is its OWN outcome, not a success: a skipped user built nothing, and folding
        # them into `ok` made a run where every single person was skipped report "3 succeeded ·
        # all succeeded" above three rows badged "Skipped".
        ok = errors = skipped = 0
        for user_report in report.users:
            user = users_by_slug.get(user_report.slug)
            if user is None:
                # A SHARED row files its report under `shared_<slug>`, which is nobody's user
                # slug — so this `continue` silently dropped it: a real Plex collection was
                # created, labelled and promoted with no run record and NO AUDIT EVENT at all
                # (plex-safety rule 10), and a failed shared row produced an errored run with
                # nothing to show for it.
                if user_report.slug.startswith(f"{SHARED_SLUG_PREFIX}_"):
                    if user_report.status == "error":
                        errors += 1
                    elif user_report.status == "skipped":
                        skipped += 1
                    _emit_shared_row_event(session, run_id, user_report, report.dry_run)
                continue
            if user_report.status == "error":
                errors += 1
            elif user_report.status == "skipped":
                skipped += 1
            else:
                ok += 1
            # Skip anyone already written by the live per-user persist — still counted above for
            # the finalize stats. This backstops users the live path missed (e.g. it errored).
            if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is None:
                _persist_user_report(session, run_id, user, user_report, report.dry_run)
        _emit_sweep_event(session, run_id, report)
        _emit_privacy_sync_events(session, run_id, report)
        _emit_hub_ordering_events(session, run_id, report)
        _emit_request_events(session, run_id, report)
        persist_request_queue(session, run_id, report)
        if report.error:
            _add_event(session, "run", "error", run_id, error=report.error)
        _finalize_run(run, report, status, error, ok, errors, skipped)
        session.commit()
    # Retention is applied AFTER this transaction commits, as its own `maintenance.prune` job.
    # It used to share this transaction: a bulk delete across runs/run_users/run_log_lines/picks
    # that failed took the persist down with it, discarding the results of a run that had already
    # written to Plex. Housekeeping must never be able to cost a run its record.
    _queue_retention_prune(sessions)


def _queue_retention_prune(sessions: sessionmaker[Session]) -> None:
    """Queue the retention pass. Never raises — the run is already persisted and safe."""
    try:
        jobs.enqueue(sessions, "maintenance.prune")
    except Exception as e:
        logger.warning("could not queue the retention prune ({}) — the next run will", type(e).__name__)


def prune_expired_cache(session: Session) -> int:
    """Drop cache rows whose TTL has passed.

    `DbCache.get` filters on `expires_at`, but nothing ever DELETED an expired row, so the table
    only grew. `library_index` is the worst of them: its key deliberately changes whenever the
    library changes, so every library edit stranded a whole-library JSON blob that could never be
    read again — in the same file the nightly backup copies in full and keeps ten of.
    """
    from shortlist.server.db.models import CacheRow

    removed = session.query(CacheRow).filter(CacheRow.expires_at < time.time()).delete(synchronize_session=False)
    if removed:
        logger.info("pruned {} expired cache row(s)", removed)
    return removed


def prune_runs(session: Session, retention_months: int) -> int:
    """Delete runs older than `retention_months`, returning how many went. 0 = keep forever.

    What is pruned: the run row, its per-user results/traces, and its activity log — the storage
    hog (~100 KB per user per run in trace blobs).

    What is NOT, and must never be:

    * **picks** — the impact ledger. Their `run_id` is nulled, not the row deleted, so the
      dashboard's history survives a run being pruned.
    * **deliveries** — the record of which Plex collection is which row for which person. It is
      keyed by SLUG rather than by foreign key precisely so it outlives runs and rows; deleting
      it would strand real collections on real users' servers with nothing left that knows to
      clean them up.
    """
    if retention_months <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_months * 30)
    old = session.query(Run.id).filter(Run.started_at < cutoff).all()
    old_ids = [rid for (rid,) in old]
    if not old_ids:
        return 0
    session.query(PickRow).filter(PickRow.run_id.in_(old_ids)).update({PickRow.run_id: None}, synchronize_session=False)
    session.query(RunUser).filter(RunUser.run_id.in_(old_ids)).delete(synchronize_session=False)
    # Explicit, not left to the FK's ON DELETE CASCADE: SQLite only enforces foreign keys when
    # `PRAGMA foreign_keys` is on, and a bulk ORM delete does not cascade in Python either.
    session.query(RunLogLine).filter(RunLogLine.run_id.in_(old_ids)).delete(synchronize_session=False)
    return session.query(Run).filter(Run.id.in_(old_ids)).delete(synchronize_session=False)


def prune_events(session: Session, retention_months: int) -> int:
    """Trim the audit trail, returning how many events went. 0 = keep forever, the default.

    Separate from run retention on purpose: `events` is the answer to "what changed on whose
    share at 03:31" (plex-safety rule 10), so it is the one thing an operator may want to keep
    far longer than the run detail around it.
    """
    if retention_months <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_months * 30)
    return session.query(Event).filter(Event.ts < cutoff).delete(synchronize_session=False)


def _add_event(session: Session, scope: str, level: str, run_id: int, *, dry_run: bool | None = None, **fields):
    """Append one audit Event, injecting the run_id (and dry_run, where relevant) that every
    emitter shares (plex-safety rule 10). Callers pass only their distinctive message fields."""
    extra = {"dry_run": dry_run} if dry_run is not None else {}
    add_audit(session, scope, level, run_id=run_id, **extra, **fields)


def _emit_shared_row_event(session: Session, run_id: int, user_report, dry_run: bool) -> None:
    """The audit record for a shared row — it has no user, so it gets no RunUser row.

    Rule 10: every write, real or dry-run, leaves a structured event with its diff. "What changed
    on the shared row at 03:31" must be answerable from the UI.
    """
    _add_event(
        session,
        "run.shared",
        "error" if user_report.status == "error" else "info",
        run_id,
        dry_run=dry_run,
        row=user_report.slug,
        status=user_report.status,
        picks=len(user_report.picks),
        error=user_report.error,
        # A shared row gets no RunUser row, so this event is the ONLY place its outcome is
        # recorded — without the reason, "why did my shared row build nothing" is answerable
        # from the container log and nowhere else (issue #3).
        reason=user_report.reason,
        diff=user_report.diff.__dict__ if user_report.diff else {},
    )


def _persist_user_report(session: Session, run_id: int, user: User, user_report, dry_run: bool) -> None:
    """One user's RunUser row, their picks (non-dry-run only), and their run.user audit event."""
    user.cold_start = user_report.status == "cold_start"
    session.add(
        RunUser(
            run_id=run_id,
            user_id=user.id,
            status=user_report.status,
            error=user_report.error,
            reason=user_report.reason,
            duration_ms=int(user_report.duration_s * 1000),
            llm_tokens=user_report.llm_tokens,
            llm_tokens_by_step=dict(user_report.llm_tokens_by_step),
            exa_searches=user_report.exa_searches,
            diff=user_report.diff.__dict__ if user_report.diff else {},
            breakdown=user_report.breakdown,
            trace=user_report.trace,
        )
    )
    if not dry_run:
        # Forget BEFORE recording: a row removed and then re-delivered in the same run (a retitle that
        # went through delete+create) must end up with the entry the delivery just wrote, not without one.
        _forget_removed_deliveries(session, user.slug, user_report.removed_deliveries)
        _record_deliveries(session, user.slug, user_report.breakdown)
        for pick in user_report.picks:
            session.add(
                PickRow(
                    run_id=run_id,
                    user_id=user.id,
                    tmdb_id=pick.tmdb_id,
                    media_type=pick.media_type.value,
                    rating_key=pick.rating_key,
                    rank=pick.rank,
                    collection_slug=pick.collection_slug,
                    section_key=pick.section_key,
                    library=pick.library,
                    title=pick.title,
                    reason=pick.reason,
                    sources=",".join(pick.sources),
                    affinity=pick.affinity,
                    seed_tmdb_id=pick.seed_tmdb_id,
                    seed_title=pick.seed_title,
                    rating=pick.rating,
                    year=pick.year,
                    recipe=pick.recipe,
                )
            )
    _add_event(
        session,
        "run.user",
        "error" if user_report.status == "error" else "info",
        run_id,
        dry_run=dry_run,
        user=user_report.slug,
        status=user_report.status,
        diff=user_report.diff.__dict__ if user_report.diff else {},
        privacy_synced=user_report.privacy_synced,
        llm_tokens=user_report.llm_tokens,
        exa_searches=user_report.exa_searches,
        error=user_report.error,
    )


def _emit_sweep_event(session: Session, run_id: int, report) -> None:
    # Rows deleted because Plex could not hide them. This is a SERVER-wide sweep, so it
    # can touch users who were not in this run at all (paused, disabled) — those have no
    # RunUser row to carry the audit, and deleting someone's row is the most destructive
    # thing a run does. It gets its own event (plex-safety rule 10).
    if not report.swept_rows:
        return
    _add_event(
        session,
        "run.sweep",
        "warning",
        run_id,
        dry_run=report.dry_run,
        reason="row was broken beyond repair-in-place — no share filter could hide it (wrong "
        "type for its library, or no shortlist label at all — an orphan from an interrupted "
        "run), or it shared a collection tag with other users' rows and held their picks",
        deleted=report.swept_rows,
    )


def _emit_privacy_sync_events(session: Session, run_id: int, report) -> None:
    # Share-filter writes. Most of these accounts are NOT in this run's user list — they
    # are simply people the server is shared with — so they have no RunUser row to carry
    # the audit. Changing someone's Plex share permissions is the most sensitive thing
    # Shortlist does; "what changed on whose share at 03:31" has to be answerable for every
    # one of them (plex-safety rule 10).
    for account_id, write in report.filter_writes.items():
        _add_event(
            session,
            "run.privacy_sync",
            "info",
            run_id,
            dry_run=report.dry_run,
            plex_account_id=account_id,
            username=write["username"],
            fields={field: {"before": before, "after": after} for field, (before, after) in write["fields"].items()},
        )


def _emit_hub_ordering_events(session: Session, run_id: int, report) -> None:
    # Recommended-shelf reorders. Moving a managed hub shifts every collection's position on a
    # server-wide shelf that a co-managing tool (Kometa) also cares about, so each library we
    # actually moved rows in is audited — "what changed on the shelf at 03:31" (plex-safety rule 10).
    for entry in report.hub_orderings:
        # `verified` is the whole point of the record. "We asked" and "it happened" are different
        # facts — a co-managing tool (agregarr, Kometa) reorders the same shelf on its own clock — and an
        # audit that only ever said the first is how a shelf owned by another tool was reported as a
        # successful reorder for weeks (SFLIX 2026-08-12). A dry run asked for nothing, so it is neither
        # verified nor a warning.
        verified = entry.get("verified")
        _add_event(
            session,
            "run.hub_order",
            "warning" if verified is False else "info",
            run_id,
            dry_run=report.dry_run,
            library=entry.get("library"),
            anchor=entry.get("anchor"),
            moved=entry.get("moved", []),
            verified=verified,
        )
    _emit_agregarr_mirror_events(session, run_id, report)


def _emit_agregarr_mirror_events(session: Session, run_id: int, report) -> None:
    # What we stored in a co-managing agregarr so its own sync stops undoing our shelf. Emitted from
    # HERE as well as `jobs._audit_agregarr_mirrors`, because the two paths that audit are not the
    # same: `sync.check`/`privacy.sync` persist no run and audit themselves, while the nightly run —
    # the one that actually does this every night — persists a run and emits its shelf events only
    # through this function. Auditing in one place would have left the main path silent.
    for entry in report.agregarr_mirrors:
        _add_event(
            session,
            "run.agregarr_order",
            "warning" if not entry.get("ok") else "info",
            run_id,
            dry_run=report.dry_run or bool(entry.get("dry_run")),
            library=entry.get("library"),
            changed=entry.get("changed"),
            items=entry.get("items"),
            moved=entry.get("moved"),
            rows_placed=entry.get("rows_placed"),
            rows_contiguous=entry.get("rows_contiguous"),
            unknown_to_agregarr=entry.get("unknown_to_agregarr"),
            unjoinable=entry.get("unjoinable"),
            # The before/after sequences, present only on a run that changed something. There is no
            # snapshot of agregarr's ordering and uninstall does not restore it, so this event is
            # the only record of what its order was before we renumbered it.
            order_before=entry.get("order_before"),
            order_after=entry.get("order_after"),
            summary=entry.get("summary"),
            error=entry.get("error"),
        )


def _emit_request_events(session: Session, run_id: int, report) -> None:
    # Sonarr/Radarr requests. Adding a title to a download app is a real outward-facing
    # write (it consumes disk and bandwidth), so every request — and every skip — is audited
    # with the app's own outcome message, dry-run included (plex-safety rule 10 spirit).
    # A separate, always-checked signal (independent of whether any title was sent): MDBList ran
    # out of quota mid-run, so ratings fell back to TMDB. Drives the owner's quota notification.
    if report.requests is not None and report.requests.warnings:
        for msg in report.requests.warnings:
            _add_event(session, "requests.incomplete_config", "warning", run_id, dry_run=report.dry_run, detail=msg)
    if report.requests is not None and report.requests.ratings_rate_limited:
        _add_event(session, "requests.rate_limited", "warning", run_id, dry_run=report.dry_run)
    if report.requests is None or not report.requests.outcomes:
        return
    _add_event(
        session,
        "run.requests",
        "info",
        run_id,
        dry_run=report.dry_run,
        considered=report.requests.considered,
        outcomes=[
            {
                "tmdb_id": o.tmdb_id,
                "title": o.title,
                "media_type": o.media_type.value,
                "status": o.status,
                "detail": o.detail,
            }
            for o in report.requests.outcomes
        ],
    )


def persist_request_queue(session: Session, run_id: int, report) -> None:
    """Save the titles a run wanted but did not auto-send, for the owner to approve by hand.

    Real runs only — a dry run is a preview and must not mutate the inbox. One row per
    (tmdb_id, media_type): a re-surfaced title refreshes the live facts of a still-pending row;
    a title already sent or rejected is left alone, so a download-in-progress isn't re-queued and
    a dismissed suggestion can't reappear every night.

    A pending title that has since ARRIVED in the library (grabbed elsewhere) is dropped, so the
    inbox never lingers on titles the owner already has. Same for one an ARR now tracks (added
    by hand, by another tool, or before the sent-ledger existed): while it downloads — or
    forever, if unaired — it's absent from Plex, so only the arr-presence prune can catch it.
    """
    if report.requests is None or report.dry_run:
        return
    existing = {(r.tmdb_id, r.media_type): r for r in session.query(RequestCandidate).all()}
    # Drop pending candidates the library now holds; leave sent/rejected alone (owner-actioned).
    present = {(tid, mt.value) for tid, mt in report.library_present}
    present |= report.requests.arr_present  # best-effort; empty when a check was skipped/failed
    for key in [k for k, r in existing.items() if r.status == "pending" and k in present]:
        session.delete(existing.pop(key))
    for m in report.requests.queued:
        row = existing.get((m.tmdb_id, m.media_type.value))
        if row is None:
            session.add(_candidate_row(m, run_id, status="pending"))
        elif row.status == "pending":
            _refresh_pending(row, m)

    # The titles this run AUTO-SENT are filed as `sent` too. Without this the ledger only knew
    # about titles the owner sent by hand, so an auto-sent title still downloading was "missing"
    # again tomorrow: it out-ranked everything by demand, re-consumed one of `max_per_run` every
    # single night, and the queue starved on the same few titles forever.
    # The Arr's answer per auto-sent title, so the sent log records the outcome ("requested",
    # "already in Radarr", …), not just that it went.
    auto_outcomes = {(o.tmdb_id, o.media_type.value): o for o in report.requests.outcomes}
    for m in report.requests.sent:
        row = existing.get((m.tmdb_id, m.media_type.value))
        outcome = auto_outcomes.get((m.tmdb_id, m.media_type.value))
        if row is None:
            new_row = _candidate_row(m, run_id, status="sent")
            new_row.sent_at = datetime.now(UTC)
            if outcome is not None:
                new_row.detail = outcome.detail
            session.add(new_row)
        else:
            # Only on the TRANSITION: a title re-surfaced by a later run is not a second send,
            # and re-stamping would keep sliding it into the current window for ever.
            if row.status != "sent":
                row.sent_at = datetime.now(UTC)
            row.status = "sent"
            if outcome is not None:
                row.detail = outcome.detail
            if m.arr_slug:  # keep an existing slug if this pass somehow didn't resolve one
                row.arr_slug = m.arr_slug


def _finalize_run(
    run: Run, report, status: str | None, error: str | None, ok: int, errors: int, skipped: int = 0
) -> None:
    # `report.ok` — not `errors == 0`. A run-level failure (the sweep could not run, so we
    # refused to write) has no per-user error to count, and must never report success.
    run.status = status or ("ok" if report.ok else "error")
    run.finished_at = datetime.now(UTC)
    # Run-total AI cost, summed from every user (real + shared). by_step merges each user's
    # {llm_web: n} so the run header can show WHERE the tokens went. (Since the curate step was
    # removed, llm_web — web-search title discovery — is the only paid AI path left.)
    tokens_by_step: dict[str, int] = {}
    for user_report in report.users:
        for step, n in user_report.llm_tokens_by_step.items():
            tokens_by_step[step] = tokens_by_step.get(step, 0) + n
    # Titles added to / rotated out of everyone's rows this run (summed across users' diffs), so
    # the runs list can show at a glance how much actually changed on Plex.
    titles_added = sum(len(u.diff.added) for u in report.users if u.diff)
    titles_removed = sum(len(u.diff.removed) for u in report.users if u.diff)
    stats = {
        "users_ok": ok,
        "users_error": errors,
        # Built nothing, but nothing went wrong — see RunUser.reason for which case it was.
        "users_skipped": skipped,
        "dry_run": report.dry_run,
        "rows_swept": sum(len(titles) for titles in report.swept_rows.values()),
        "shares_updated": len(report.filter_writes),
        "titles_added": titles_added,
        "titles_removed": titles_removed,
        "titles_requested": report.requests.requested if report.requests else 0,
        "requests_warnings": report.requests.warnings if report.requests else [],
        "llm_tokens": sum(u.llm_tokens for u in report.users),
        "llm_tokens_by_step": tokens_by_step,
        "exa_searches": sum(u.exa_searches for u in report.users),
        # Cache hits served from the shared 14-day web-search cache. Reported so the UI can read
        # "1 searched · N from cache" — without it a fully-cached run shows a bare exa_searches:1
        # and looks like the source did nothing (it didn't: the cache did the work).
        "exa_cache_hits": sum(u.exa_cache_hits for u in report.users),
        "error": error or report.error,
        # Every account whose share filter Plex refused this run. These are the reason nothing
        # was promoted, so the UI can say so instead of leaving "Failed" unexplained (issue #1).
        "promotion_blockers": list(report.promotion_blockers),
    }
    # Accounts Plex refuses a hide-list for that can nonetheless SEE other people's rows. Not a
    # blocker — nothing we do hides them — so the run succeeds and this is how the owner finds out.
    #
    # Present ONLY when the run reached the privacy phase and actually looked. Both readers pick the
    # latest run carrying this key and treat it as the truth, so writing it unconditionally meant a
    # run that died in the sweep phase published an empty finding and silently cleared a live alert
    # and every badge. Absent now means "this run did not measure", which is what they already
    # assumed it meant.
    if report.unhideable_measured:
        stats["unhideable_rows"] = {name: list(keys) for name, keys in report.unhideable_rows.items()}
    # Assigned whole rather than mutated in place: `stats` is a JSON column, and an in-place edit
    # after assignment would not reliably mark it dirty.
    run.stats = stats
