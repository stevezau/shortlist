"""Run service — the server's run orchestrator.

Executes runs in a worker thread (the engine is sync), enforces the write gate, persists
runs/run_users/picks/events rows, and emits SSE progress. A `runs` row is inserted BEFORE
execution so a container restart can see and abort orphaned runs. Context assembly (clients,
config, profiles) lives in ``context_builder.ContextBuilder``; this module is only orchestration.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.pipeline import EngineContext
from shortlist.engine.pipeline import run as engine_run
from shortlist.server.db.models import Event, PickRow, RequestCandidate, Run, RunUser, Server, User
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.privacy_state import gate_error
from shortlist.server.services.sse import EventBus


class RunService:
    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus, config_dir: Path, secret_box):
        self._sessions = session_factory
        self._bus = bus
        self._config_dir = config_dir
        self._secrets = secret_box
        self._ctx = ContextBuilder(session_factory, secret_box, bus)
        self._lock = asyncio.Lock()  # one run at a time; nightly + manual runs must not overlap
        self._tasks: set[asyncio.Task] = set()  # strong refs so in-flight runs aren't GC'd

    # -- context assembly (delegated to ContextBuilder) ----------------------------------

    def build_context(self, *, dry_run: bool, loop: asyncio.AbstractEventLoop | None = None) -> EngineContext:
        return self._ctx.build(dry_run=dry_run, loop=loop)

    def build_requests_context(self):
        """Requests config + TMDB client for the approval inbox's manual send — no Plex/LLM I/O."""
        return self._ctx.build_requests_only()

    def enabled_profiles(self, session: Session, user_ids: list[int] | None = None):
        return self._ctx.enabled_profiles(session, user_ids)

    def user_history(self, user_id: int, *, limit: int = 25) -> list[dict] | None:
        return self._ctx.user_history(user_id, limit=limit)

    # -- execution -----------------------------------------------------------------------

    async def start_run(self, *, trigger: str, dry_run: bool, user_ids: list[int] | None = None) -> int:
        """Insert the runs row and launch execution as a background task; returns run id."""
        with self._sessions() as session:
            run = Run(trigger=trigger, dry_run=dry_run, status="queued", stats={})
            session.add(run)
            session.commit()
            run_id = run.id
        task = asyncio.create_task(self._execute(run_id, dry_run, user_ids))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    def _privacy_gate_error(self) -> str | None:
        """plex-safety rule 1, server-side: real writes need a fresh passing Privacy Check.

        Uses the same latest-per-tier definition as the dashboard's privacy badge, so the two
        can never disagree (a stale T2 failure must not be masked by a newer T1-only pass).
        """
        with self._sessions() as session:
            server = session.query(Server).first()
            return gate_error(session, server.version if server else None)

    def _remedy_only(self):
        """Everything that makes the server MORE private, and nothing else.

        Running the engine with no users does exactly that: it sweeps rows Plex cannot hide, then
        merges the excludes for every row that exists into every account's share filter. Nothing
        is created, nothing is promoted — deletion and merge-only excludes cannot expose anything.

        This is what a closed gate must still allow, or the gate deadlocks itself: a missing
        exclude FAILS the Privacy Check, the failed check closes the gate, and a closed gate that
        blocked the sync would stop the only thing that writes the exclude. The check could never
        pass again. (Live server, SFLIX, 2026-07-13: T1 failed for 45 accounts, and the run that
        would have fixed them was refused because T1 failed.)
        """
        return engine_run(self.build_context(dry_run=False), [])

    async def _execute(self, run_id: int, dry_run: bool, user_ids: list[int] | None) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if not dry_run and (gate := self._privacy_gate_error()):
                await self._run_remedy_pass(run_id, gate, loop)
                return
            self._bus.publish("run.progress", {"run_id": run_id, "status": "running"})
            with self._sessions() as session:
                run = session.get(Run, run_id)
                run.status = "running"
                session.commit()
                profiles = self.enabled_profiles(session, user_ids)
            try:
                ctx = self.build_context(dry_run=dry_run, loop=loop)
                report = await loop.run_in_executor(None, engine_run, ctx, profiles)
                self._persist_report(run_id, report)
                status = "ok" if report.ok else "error"
            except Exception as e:
                logger.exception("run {} failed", run_id)
                self._mark_run_error(run_id, {"error": f"{type(e).__name__}: {e}"})
                self._bus.publish("run.finished", {"run_id": run_id, "status": "error"})
                return
            self._bus.publish("run.finished", {"run_id": run_id, "status": status})

    async def _run_remedy_pass(self, run_id: int, gate: str, loop: asyncio.AbstractEventLoop) -> None:
        """The gate refused to BUILD rows — but the remedy must still run, or the gate deadlocks.

        A row of the wrong type for its library is already visible to every account right now;
        removing it (and merging the excludes for every row that exists) is the remedy, not a new
        risk. Gating it would be a trap: such a row FAILS the Privacy Check, a failed check closes
        the gate, and the closed gate would then block the very sweep that removes it — so the leak
        could never heal. That is precisely the state a live server was left in (SFLIX, 2026-07-12).
        """
        logger.warning("run {} refused by privacy gate: {}", run_id, gate)
        try:
            report = await loop.run_in_executor(None, self._remedy_only)
        except Exception as e:
            # A failing remedy must never leave the run stuck: the gate refusal is the headline,
            # and this is the footnote.
            logger.exception("the remedy pass failed while the privacy gate was closed")
            self._mark_run_error(run_id, {"error": f"privacy gate: {gate}", "remedy_error": f"{type(e).__name__}: {e}"})
        else:
            # `status` is forced: nothing was BUILT, so the run is an error whatever the remedy did.
            # Passing it here means the run is never momentarily recorded as a success — a restart in
            # that window would have left a refused run saying "ok".
            self._persist_report(run_id, report, status="error", error=f"privacy gate: {gate}")
            if report.error:  # the remedy can degrade without raising
                self._merge_run_stats(run_id, {"remedy_error": report.error})
        self._bus.publish("run.finished", {"run_id": run_id, "status": "error"})

    def _mark_run_error(self, run_id: int, stats: dict) -> None:
        """Force a run to a finished error state with the given stats (build/remedy failures)."""
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
                    continue
                if user_report.status == "error":
                    errors += 1
                else:
                    ok += 1
                self._persist_user_report(session, run_id, user, user_report, report.dry_run)
            self._emit_sweep_event(session, run_id, report)
            self._emit_privacy_sync_events(session, run_id, report)
            self._emit_request_events(session, run_id, report)
            self._persist_request_queue(session, run_id, report)
            if report.error:
                session.add(Event(scope="run", level="error", message={"run_id": run_id, "error": report.error}))
            self._finalize_run(run, report, status, error, ok, errors)
            session.commit()

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
                diff=user_report.diff.__dict__ if user_report.diff else {},
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
                    "error": user_report.error,
                },
            )
        )

    @staticmethod
    def _emit_sweep_event(session: Session, run_id: int, report) -> None:
        # Rows deleted because Plex could not hide them. This is a SERVER-wide sweep, so it
        # can touch users who were not in this run at all (paused, disabled) — those have no
        # RunUser row to carry the audit, and deleting someone's row is the most destructive
        # thing a run does. It gets its own event (plex-safety rule 10).
        if not report.swept_rows:
            return
        session.add(
            Event(
                scope="run.sweep",
                level="warning",
                message={
                    "run_id": run_id,
                    "dry_run": report.dry_run,
                    "reason": "row was broken beyond repair-in-place — either no share "
                    "filter could hide it (wrong type for its library), or it shared a "
                    "collection tag with other users' rows and held their picks",
                    "deleted": report.swept_rows,
                },
            )
        )

    @staticmethod
    def _emit_privacy_sync_events(session: Session, run_id: int, report) -> None:
        # Share-filter writes. Most of these accounts are NOT in this run's user list — they
        # are simply people the server is shared with — so they have no RunUser row to carry
        # the audit. Changing someone's Plex share permissions is the most sensitive thing
        # Shortlist does; "what changed on whose share at 03:31" has to be answerable for every
        # one of them (plex-safety rule 10).
        for account_id, write in report.filter_writes.items():
            session.add(
                Event(
                    scope="run.privacy_sync",
                    level="info",
                    message={
                        "run_id": run_id,
                        "dry_run": report.dry_run,
                        "plex_account_id": account_id,
                        "username": write["username"],
                        "fields": {
                            field: {"before": before, "after": after}
                            for field, (before, after) in write["fields"].items()
                        },
                    },
                )
            )

    @staticmethod
    def _emit_request_events(session: Session, run_id: int, report) -> None:
        # Sonarr/Radarr requests. Adding a title to a download app is a real outward-facing
        # write (it consumes disk and bandwidth), so every request — and every skip — is audited
        # with the app's own outcome message, dry-run included (plex-safety rule 10 spirit).
        if report.requests is None or not report.requests.outcomes:
            return
        session.add(
            Event(
                scope="run.requests",
                level="info",
                message={
                    "run_id": run_id,
                    "dry_run": report.dry_run,
                    "considered": report.requests.considered,
                    "outcomes": [
                        {
                            "tmdb_id": o.tmdb_id,
                            "title": o.title,
                            "media_type": o.media_type.value,
                            "status": o.status,
                            "detail": o.detail,
                        }
                        for o in report.requests.outcomes
                    ],
                },
            )
        )

    @staticmethod
    def _persist_request_queue(session: Session, run_id: int, report) -> None:
        """Save the titles a run wanted but did not auto-send, for the owner to approve by hand.

        Real runs only — a dry run is a preview and must not mutate the inbox. One row per
        (tmdb_id, media_type): a re-surfaced title refreshes the live facts of a still-pending row;
        a title already sent or rejected is left alone, so a download-in-progress isn't re-queued and
        a dismissed suggestion can't reappear every night.
        """
        if report.requests is None or report.dry_run or not report.requests.queued:
            return
        existing = {(r.tmdb_id, r.media_type): r for r in session.query(RequestCandidate).all()}
        for m in report.requests.queued:
            row = existing.get((m.tmdb_id, m.media_type.value))
            if row is None:
                session.add(
                    RequestCandidate(
                        tmdb_id=m.tmdb_id,
                        media_type=m.media_type.value,
                        title=m.title,
                        year=m.year,
                        rating=m.rating,
                        vote_count=m.vote_count,
                        demand=m.demand,
                        status="pending",
                        first_seen_run_id=run_id,
                    )
                )
            elif row.status == "pending":
                row.title, row.year, row.rating, row.vote_count, row.demand = (
                    m.title,
                    m.year,
                    m.rating,
                    m.vote_count,
                    m.demand,
                )

    @staticmethod
    def _finalize_run(run: Run, report, status: str | None, error: str | None, ok: int, errors: int) -> None:
        # `report.ok` — not `errors == 0`. A run-level failure (the sweep could not run, so we
        # refused to write) has no per-user error to count, and must never report success.
        run.status = status or ("ok" if report.ok else "error")
        run.finished_at = datetime.now(UTC)
        run.stats = {
            "users_ok": ok,
            "users_error": errors,
            "dry_run": report.dry_run,
            "rows_swept": sum(len(titles) for titles in report.swept_rows.values()),
            "shares_updated": len(report.filter_writes),
            "titles_requested": report.requests.requested if report.requests else 0,
            "error": error or report.error,
        }
