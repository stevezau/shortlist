"""Run service — the server's run orchestrator.

Executes runs in a worker thread (the engine is sync), persists
runs/run_users/picks/events rows, and emits SSE progress. A `runs` row is inserted BEFORE
execution so a container restart can see and abort orphaned runs. Context assembly (clients,
config, profiles) lives in ``context_builder.ContextBuilder``; this module is only orchestration.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.models import SHARED_SLUG_PREFIX
from shortlist.engine.pipeline import EngineContext
from shortlist.engine.pipeline import run as engine_run
from shortlist.server.db.models import Event, PickRow, RequestCandidate, Run, RunUser, User
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.sse import EventBus

HIT_WINDOW_DAYS = 30  # a pick counts as a hit if it is watched within 30 days of being recommended


def _why_json(why) -> list[dict]:
    """Serialize a missing title's provenance for storage + the API: [{user, row, seed, source}]."""
    return [{"user": w.user, "row": w.row, "seed": w.seed, "source": w.source} for w in why]


def _candidate_row(m, run_id: int, *, status: str) -> RequestCandidate:
    """One inbox row for a missing title, in whichever state the run left it."""
    return RequestCandidate(
        tmdb_id=m.tmdb_id,
        media_type=m.media_type.value,
        title=m.title,
        year=m.year,
        imdb_id=m.imdb_id,
        rating=m.rating,
        vote_count=m.vote_count,
        demand=m.demand,
        tags=sorted(m.tags),
        wanters=sorted(m.wanters),
        why=_why_json(m.why),
        status=status,
        detail=m.detail,  # a failed auto-send carries WHY it didn't land, shown as "Last attempt: …"
        excluded=m.excluded,  # on a Sonarr/Radarr exclusion list — flagged in the inbox
        first_seen_run_id=run_id,
    )


class RunService:
    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus, config_dir: Path, secret_box):
        self._sessions = session_factory
        self._bus = bus
        self._config_dir = config_dir
        self._secrets = secret_box
        self._ctx = ContextBuilder(session_factory, secret_box, bus)
        self._lock = asyncio.Lock()  # one run at a time; nightly + manual runs must not overlap
        # SQLite is single-writer, but the engine finishes users on a thread POOL — so the live
        # per-user persist (below) must serialize its commits across those worker threads.
        self._persist_lock = threading.Lock()
        # run_id -> cancel flag for the one in-flight run, so the /cancel endpoint can ask the engine
        # to stop. The engine checks it before each user (cooperative), so an in-flight user finishes.
        self._cancels: dict[int, threading.Event] = {}
        self._tasks: set[asyncio.Task] = set()  # strong refs so in-flight runs aren't GC'd
        # run_id -> the run's stage activity log, in memory so a page reload can replay it. Bounded
        # per run and to the last few runs (the per-user RESULTS are the durable record; this is the
        # live/recent debugging feed). Lost on restart, which is fine — it's not an audit trail.
        self._run_logs: OrderedDict[int, deque[dict]] = OrderedDict()
        self._run_log_runs = 10  # keep the activity log for this many most-recent runs

    def _new_run_log(self, run_id: int) -> Callable[[dict], None]:
        """Start (or reset) a run's activity buffer and return an append sink for the progress hook."""
        log: deque[dict] = deque(maxlen=2000)
        self._run_logs[run_id] = log
        self._run_logs.move_to_end(run_id)
        while len(self._run_logs) > self._run_log_runs:
            self._run_logs.popitem(last=False)
        return log.append

    def run_log(self, run_id: int) -> list[dict]:
        """The in-memory stage activity log for a run (empty if evicted or never run this process)."""
        return list(self._run_logs.get(run_id, ()))

    # -- context assembly (delegated to ContextBuilder) ----------------------------------

    def build_context(
        self,
        *,
        dry_run: bool,
        loop: asyncio.AbstractEventLoop | None = None,
        run_id: int | None = None,
        log_sink: Callable[[dict], None] | None = None,
        collection_ids: list[int] | None = None,
    ) -> EngineContext:
        return self._ctx.build(
            dry_run=dry_run, loop=loop, run_id=run_id, log_sink=log_sink, collection_ids=collection_ids
        )

    def build_requests_context(self):
        """Requests config + TMDB client for the approval inbox's manual send — no Plex/LLM I/O."""
        return self._ctx.build_requests_only()

    def enabled_profiles(self, session: Session, user_ids: list[int] | None = None):
        return self._ctx.enabled_profiles(session, user_ids)

    def user_history(self, user_id: int, *, limit: int = 25) -> list[dict] | None:
        return self._ctx.user_history(user_id, limit=limit)

    def sync_watched_background(self) -> None:
        """Fire sync_watched as a tracked background task (the reference is kept so it isn't GC'd) —
        for the dashboard's manual 'Sync now'."""
        task = asyncio.create_task(self.sync_watched())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def sync_watched(self) -> None:
        """Refresh every enabled user's ``watched_at`` from their current watch history WITHOUT
        rebuilding rows or writing to Plex — a read-only reconcile so the effectiveness report stays
        fresh daily even when a row's own cron is weekly (or a user has no scheduled row at all).

        Skips quietly if Plex isn't configured (build_context raises), and a per-user history-fetch
        failure is logged and skipped rather than aborting the sweep. Serialized against runs by the
        same lock, so it never overlaps a live run's per-user writes."""
        loop = asyncio.get_running_loop()

        def work() -> int:
            from shortlist.server.settings_store import SettingsStore

            ctx = self.build_context(dry_run=True)  # dry: builds clients, writes nothing to Plex
            with self._sessions() as session:
                profiles = self.enabled_profiles(session)
            for profile in profiles:
                try:
                    profile.history = ctx.history_source.fetch(profile, min_completion=ctx.config.min_completion)
                except Exception as e:
                    logger.warning("watch-sync: history fetch failed for {}: {}", profile.slug, type(e).__name__)
            self._reconcile_watched(profiles)
            with self._sessions() as session:
                # Stamp the sync so the dashboard can show "watch status synced N ago".
                SettingsStore(session).set("report.watch_synced_at", datetime.now(UTC).isoformat())
            return len(profiles)

        async with self._lock:
            try:
                count = await loop.run_in_executor(None, work)
                logger.info("watch-sync: refreshed watch status for {} user(s)", count)
            except Exception as e:  # e.g. Plex not configured yet — never crash the scheduler
                logger.info("watch-sync skipped: {}", type(e).__name__)

    # -- execution -----------------------------------------------------------------------

    async def start_run(
        self,
        *,
        trigger: str,
        dry_run: bool,
        user_ids: list[int] | None = None,
        collection_ids: list[int] | None = None,
    ) -> int:
        """Insert the runs row and launch execution as a background task; returns run id.

        ``collection_ids`` scopes the run to specific rows (a per-row scheduled run builds only its
        own rows); ``None`` builds every enabled row. The leak-safe privacy sync always covers every
        account regardless, so rows not built this run stay hidden.
        """
        with self._sessions() as session:
            run = Run(trigger=trigger, dry_run=dry_run, status="queued", stats={})
            session.add(run)
            session.commit()
            run_id = run.id
        self._cancels[run_id] = threading.Event()  # armed here so /cancel works the instant it's queued
        task = asyncio.create_task(self._execute(run_id, dry_run, user_ids, collection_ids))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _execute(
        self, run_id: int, dry_run: bool, user_ids: list[int] | None, collection_ids: list[int] | None = None
    ) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            self._bus.publish("run.progress", {"run_id": run_id, "status": "running"})
            cancel = self._cancels.get(run_id)
            try:
                # Inside the try so a failure here (e.g. reading users) still marks the run errored
                # AND runs the finally that frees the cancel Event — never leaves a run stuck "running".
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    run.status = "running"
                    session.commit()
                    profiles = self.enabled_profiles(session, user_ids)
                ctx = self.build_context(
                    dry_run=dry_run,
                    loop=loop,
                    run_id=run_id,
                    log_sink=self._new_run_log(run_id),
                    collection_ids=collection_ids,
                )
                # Cooperative cancel: the engine checks this before each user and skips the rest. An
                # in-flight user still finishes (per-user transactional), and the privacy merge +
                # promote still run for who was delivered, so a cancel leaves a consistent server.
                if cancel is not None:
                    ctx.cancelled = cancel.is_set
                # Persist each user's results the moment they finish, so the run page fills in person by
                # person instead of staying empty until the whole run ends (the end-of-run persist below
                # is the backstop + reconciler).
                ctx.on_user_done = lambda profile, user_report: self._persist_user_live(
                    run_id, profile, user_report, dry_run
                )
                report = await loop.run_in_executor(None, engine_run, ctx, profiles)
                aborted = cancel is not None and cancel.is_set()
                self._persist_report(run_id, report, status="aborted" if aborted else None)
                # The engine filled each profile's history in place, so this is the one moment we hold
                # both "what we recommended" and "what they have since watched". A dry run is a
                # preview and mutates nothing, matching the rest of persistence.
                if not dry_run:
                    self._reconcile_watched(profiles)
                status = "aborted" if aborted else ("ok" if report.ok else "error")
            except Exception as e:
                logger.exception("run {} failed", run_id)
                self._mark_run_error(run_id, {"error": f"{type(e).__name__}: {e}"})
                self._bus.publish(
                    "run.finished", {"run_id": run_id, "status": "error", "error": f"{type(e).__name__}: {e}"}
                )
                return
            finally:
                self._cancels.pop(run_id, None)
            # Carry the reason on failure so the UI (e.g. the wizard's first run) can show it inline
            # rather than only pointing at the Runs page.
            self._bus.publish(
                "run.finished",
                {"run_id": run_id, "status": status, "error": None if report.ok else report.error},
            )

    def cancel_run(self, run_id: int) -> bool:
        """Ask the in-flight run to stop. Returns True if a running run was signalled, False if that
        run isn't currently executing here (already finished, never ran, or a stale id).

        Cooperative: the engine stops before the next user, so a user already being built finishes and
        the privacy merge + promote still run for everyone delivered — the server stays consistent.
        """
        event = self._cancels.get(run_id)
        if event is None or event.is_set():
            return False
        event.set()
        self._bus.publish("run.progress", {"run_id": run_id, "status": "cancelling"})
        logger.info("run {} cancel requested", run_id)
        return True

    def _mark_run_error(self, run_id: int, stats: dict) -> None:
        """Force a run to a finished error state with the given stats (a build failure)."""
        with self._sessions() as session:
            run = session.get(Run, run_id)
            run.status = "error"
            run.finished_at = datetime.now(UTC)
            run.stats = stats
            session.commit()

    def _merge_run_stats(self, run_id: int, extra: dict) -> None:
        with self._sessions() as session:
            run = session.get(Run, run_id)
            run.stats = {**(run.stats or {}), **extra}
            session.commit()

    # -- persistence ---------------------------------------------------------------------

    def _reconcile_watched(self, profiles) -> None:
        """Mark the picks a person actually watched — the hit rate, and the whole point of the app.

        `picks.watched_at` was declared, migrated and read by the hit-rate query, but never WRITTEN:
        every user's hit rate was structurally 0%, while the docs promised "expect 20-40%".

        A pick counts as a hit only when the watch happened AFTER we recommended it (the run that
        produced it) and within 30 days — recommending something they had already seen isn't a hit,
        and neither is a watch a year later. `history_depth` is refreshed here too; it was likewise
        surfaced in the UI and written nowhere, so every user read "0 titles watched".
        """
        with self._sessions() as session:
            for profile in profiles:
                user = session.query(User).filter_by(slug=profile.slug).first()
                if user is None:
                    continue
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
                # that grows without bound.
                cutoff = datetime.now(UTC) - timedelta(days=HIT_WINDOW_DAYS)
                unwatched = (
                    session.query(PickRow, Run.started_at)
                    .join(Run, PickRow.run_id == Run.id)
                    .filter(
                        PickRow.user_id == user.id,
                        PickRow.watched_at.is_(None),
                        Run.started_at >= cutoff,
                    )
                    .all()
                )
                for pick, recommended_at in unwatched:
                    watched = latest_watch.get((pick.tmdb_id, pick.media_type))
                    if watched is None:
                        continue
                    since = recommended_at if recommended_at.tzinfo else recommended_at.replace(tzinfo=UTC)
                    if since <= watched <= since + timedelta(days=HIT_WINDOW_DAYS):
                        pick.watched_at = watched
            session.commit()

    def _persist_user_live(self, run_id: int, profile, user_report, dry_run: bool) -> None:
        """Persist ONE user's results as they finish (called from the engine's worker threads), so the
        run page shows each person on completion rather than the whole roster only at run's end. Its
        commits are serialized (SQLite single-writer) and it never re-writes a user already stored, so
        the end-of-run `_persist_report` stays a safe backstop + reconciler. A shared-row/unknown slug
        has no user row here and is handled only at run end."""
        with self._persist_lock, self._sessions() as session:
            user = session.query(User).filter_by(slug=profile.slug).first()
            if user is None:
                return
            if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is not None:
                return
            self._persist_user_report(session, run_id, user, user_report, dry_run)
            session.commit()

    def _persist_report(self, run_id: int, report, *, status: str | None = None, error: str | None = None) -> None:
        """Persist a run's outcome. `status`/`error` override what the report says — the gated
        path uses them so a refused run is never even momentarily written as a success."""
        with self._sessions() as session:
            run = session.get(Run, run_id)
            users_by_slug = {u.slug: u for u in session.query(User).all()}
            ok = errors = 0
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
                        self._emit_shared_row_event(session, run_id, user_report, report.dry_run)
                    continue
                if user_report.status == "error":
                    errors += 1
                else:
                    ok += 1
                # Skip anyone already written by the live per-user persist — still counted above for
                # the finalize stats. This backstops users the live path missed (e.g. it errored).
                if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is None:
                    self._persist_user_report(session, run_id, user, user_report, report.dry_run)
            self._emit_sweep_event(session, run_id, report)
            self._emit_privacy_sync_events(session, run_id, report)
            self._emit_hub_ordering_events(session, run_id, report)
            self._emit_request_events(session, run_id, report)
            self._persist_request_queue(session, run_id, report)
            if report.error:
                self._add_event(session, "run", "error", run_id, error=report.error)
            self._finalize_run(run, report, status, error, ok, errors)
            from shortlist.server.settings_store import SettingsStore

            self._prune_runs(session, int(SettingsStore(session).get("runs.retention")))
            session.commit()

    @staticmethod
    def _prune_runs(session: Session, keep: int) -> None:
        """Keep the newest `keep` runs (and their picks + per-user rows), deleting the rest. 0 = keep
        everything. The just-finalized run has the highest id, so it's always kept.

        A run beyond `keep` is ONLY pruned once it's also older than the hit-window: `_reconcile_watched`
        credits a watch to a pick whose run started within `HIT_WINDOW_DAYS`, so deleting such a run
        early would silently drop hits the report still owes. The scheduler fires one run per row-cron,
        so a low `keep` can span far less than the window — the time floor closes that gap. Picks and
        run_users aren't ORM-cascaded off Run (and a bulk delete bypasses the cascade anyway), so both
        are deleted explicitly."""
        if keep <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=HIT_WINDOW_DAYS)

        def _older_than_window(started_at: datetime | None) -> bool:
            if started_at is None:
                return True
            aware = started_at if started_at.tzinfo else started_at.replace(tzinfo=UTC)
            return aware < cutoff

        beyond_keep = session.query(Run.id, Run.started_at).order_by(Run.id.desc()).offset(keep).all()
        old_ids = [rid for rid, started_at in beyond_keep if _older_than_window(started_at)]
        if not old_ids:
            return
        session.query(PickRow).filter(PickRow.run_id.in_(old_ids)).delete(synchronize_session=False)
        session.query(RunUser).filter(RunUser.run_id.in_(old_ids)).delete(synchronize_session=False)
        session.query(Run).filter(Run.id.in_(old_ids)).delete(synchronize_session=False)

    @staticmethod
    def _add_event(session: Session, scope: str, level: str, run_id: int, *, dry_run: bool | None = None, **fields):
        """Append one audit Event, injecting the run_id (and dry_run, where relevant) that every
        emitter shares (plex-safety rule 10). Callers pass only their distinctive message fields."""
        message: dict = {"run_id": run_id}
        if dry_run is not None:
            message["dry_run"] = dry_run
        message.update(fields)
        session.add(Event(scope=scope, level=level, message=message))

    @classmethod
    def _emit_shared_row_event(cls, session: Session, run_id: int, user_report, dry_run: bool) -> None:
        """The audit record for a shared row — it has no user, so it gets no RunUser row.

        Rule 10: every write, real or dry-run, leaves a structured event with its diff. "What changed
        on the shared row at 03:31" must be answerable from the UI.
        """
        cls._add_event(
            session,
            "run.shared",
            "error" if user_report.status == "error" else "info",
            run_id,
            dry_run=dry_run,
            row=user_report.slug,
            status=user_report.status,
            picks=len(user_report.picks),
            error=user_report.error,
            diff=user_report.diff.__dict__ if user_report.diff else {},
        )

    @staticmethod
    def _persist_user_report(session: Session, run_id: int, user: User, user_report, dry_run: bool) -> None:
        """One user's RunUser row, their picks (non-dry-run only), and their run.user audit event."""
        user.cold_start = user_report.status == "cold_start"
        session.add(
            RunUser(
                run_id=run_id,
                user_id=user.id,
                status=user_report.status,
                error=user_report.error,
                duration_ms=int(user_report.duration_s * 1000),
                llm_tokens=user_report.llm_tokens,
                llm_tokens_by_step=dict(user_report.llm_tokens_by_step),
                exa_searches=user_report.exa_searches,
                diff=user_report.diff.__dict__ if user_report.diff else {},
                breakdown=user_report.breakdown,
            )
        )
        if not dry_run:
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
                        seed_tmdb_id=pick.seed_tmdb_id,
                        seed_title=pick.seed_title,
                    )
                )
        session.add(
            Event(
                scope="run.user",
                level="error" if user_report.status == "error" else "info",
                message={
                    "run_id": run_id,
                    "user": user_report.slug,
                    "status": user_report.status,
                    "dry_run": dry_run,
                    "diff": user_report.diff.__dict__ if user_report.diff else {},
                    "privacy_synced": user_report.privacy_synced,
                    "llm_tokens": user_report.llm_tokens,
                    "exa_searches": user_report.exa_searches,
                    "error": user_report.error,
                },
            )
        )

    @classmethod
    def _emit_sweep_event(cls, session: Session, run_id: int, report) -> None:
        # Rows deleted because Plex could not hide them. This is a SERVER-wide sweep, so it
        # can touch users who were not in this run at all (paused, disabled) — those have no
        # RunUser row to carry the audit, and deleting someone's row is the most destructive
        # thing a run does. It gets its own event (plex-safety rule 10).
        if not report.swept_rows:
            return
        cls._add_event(
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

    @classmethod
    def _emit_privacy_sync_events(cls, session: Session, run_id: int, report) -> None:
        # Share-filter writes. Most of these accounts are NOT in this run's user list — they
        # are simply people the server is shared with — so they have no RunUser row to carry
        # the audit. Changing someone's Plex share permissions is the most sensitive thing
        # Shortlist does; "what changed on whose share at 03:31" has to be answerable for every
        # one of them (plex-safety rule 10).
        for account_id, write in report.filter_writes.items():
            cls._add_event(
                session,
                "run.privacy_sync",
                "info",
                run_id,
                dry_run=report.dry_run,
                plex_account_id=account_id,
                username=write["username"],
                fields={
                    field: {"before": before, "after": after} for field, (before, after) in write["fields"].items()
                },
            )

    @classmethod
    def _emit_hub_ordering_events(cls, session: Session, run_id: int, report) -> None:
        # Recommended-shelf reorders. Moving a managed hub shifts every collection's position on a
        # server-wide shelf that a co-managing tool (Kometa) also cares about, so each library we
        # actually moved rows in is audited — "what changed on the shelf at 03:31" (plex-safety rule 10).
        for entry in report.hub_orderings:
            cls._add_event(
                session,
                "run.hub_order",
                "info",
                run_id,
                dry_run=report.dry_run,
                library=entry.get("library"),
                anchor=entry.get("anchor"),
                moved=entry.get("moved", []),
            )

    @classmethod
    def _emit_request_events(cls, session: Session, run_id: int, report) -> None:
        # Sonarr/Radarr requests. Adding a title to a download app is a real outward-facing
        # write (it consumes disk and bandwidth), so every request — and every skip — is audited
        # with the app's own outcome message, dry-run included (plex-safety rule 10 spirit).
        if report.requests is None or not report.requests.outcomes:
            return
        cls._add_event(
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

    @staticmethod
    def _persist_request_queue(session: Session, run_id: int, report) -> None:
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
                (
                    row.title,
                    row.year,
                    row.imdb_id,
                    row.rating,
                    row.vote_count,
                    row.demand,
                    row.tags,
                    row.wanters,
                    row.why,
                    row.detail,
                    row.excluded,
                ) = (
                    m.title,
                    m.year,
                    m.imdb_id or row.imdb_id,  # keep a known id if a later run couldn't re-fetch it
                    m.rating,
                    m.vote_count,
                    m.demand,
                    sorted(m.tags),
                    sorted(m.wanters),
                    _why_json(m.why),
                    m.detail or row.detail,  # keep the last failure reason if this pass didn't set one
                    m.excluded,  # refresh the exclusion flag each run (a removed exclusion clears it)
                )

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
                if outcome is not None:
                    new_row.detail = outcome.detail
                session.add(new_row)
            else:
                row.status = "sent"
                if outcome is not None:
                    row.detail = outcome.detail

    @staticmethod
    def _finalize_run(run: Run, report, status: str | None, error: str | None, ok: int, errors: int) -> None:
        # `report.ok` — not `errors == 0`. A run-level failure (the sweep could not run, so we
        # refused to write) has no per-user error to count, and must never report success.
        run.status = status or ("ok" if report.ok else "error")
        run.finished_at = datetime.now(UTC)
        # Run-total AI cost, summed from every user (real + shared). by_step merges each user's
        # {curate/llm_web/llm_library: n} so the run header can show WHERE the tokens went.
        tokens_by_step: dict[str, int] = {}
        for user_report in report.users:
            for step, n in user_report.llm_tokens_by_step.items():
                tokens_by_step[step] = tokens_by_step.get(step, 0) + n
        # Titles added to / rotated out of everyone's rows this run (summed across users' diffs), so
        # the runs list can show at a glance how much actually changed on Plex.
        titles_added = sum(len(u.diff.added) for u in report.users if u.diff)
        titles_removed = sum(len(u.diff.removed) for u in report.users if u.diff)
        run.stats = {
            "users_ok": ok,
            "users_error": errors,
            "dry_run": report.dry_run,
            "rows_swept": sum(len(titles) for titles in report.swept_rows.values()),
            "shares_updated": len(report.filter_writes),
            "titles_added": titles_added,
            "titles_removed": titles_removed,
            "titles_requested": report.requests.requested if report.requests else 0,
            "llm_tokens": sum(u.llm_tokens for u in report.users),
            "llm_tokens_by_step": tokens_by_step,
            "exa_searches": sum(u.exa_searches for u in report.users),
            "error": error or report.error,
        }
