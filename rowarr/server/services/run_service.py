"""Run service — the server's adapter over the engine.

Builds an EngineContext from DB settings, executes runs in a worker thread (the engine is
sync), persists runs/run_users/picks/events rows, and emits SSE progress. A `runs` row is
inserted BEFORE execution so a container restart can see and abort orphaned runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from rowarr.engine.clients.plex import PlexClient, PlexTvClient
from rowarr.engine.clients.tautulli import TautulliClient
from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.curator import make_curator
from rowarr.engine.history import FallbackHistorySource, PlexHistorySource, TautulliSource
from rowarr.engine.models import EngineConfig, FilterSnapshot, UserProfile, UserType
from rowarr.engine.pipeline import EngineContext
from rowarr.engine.pipeline import run as engine_run
from rowarr.server.db.models import CacheRow, Event, PickRow, RestrictionSnapshotRow, Run, RunUser, User
from rowarr.server.services.sse import EventBus
from rowarr.server.settings_store import SettingsStore


class DbSnapshotStore:
    """Engine SnapshotStore over the restriction_snapshots table."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def get(self, plex_account_id: int) -> FilterSnapshot | None:
        with self._sessions() as session:
            user = session.query(User).filter_by(plex_account_id=plex_account_id).one_or_none()
            if user is None:
                return None
            row = (
                session.query(RestrictionSnapshotRow)
                .filter_by(user_id=user.id, reason="initial")
                .order_by(RestrictionSnapshotRow.id)
                .first()
            )
            if row is None:
                return None
            return FilterSnapshot(
                plex_account_id=plex_account_id,
                username=user.username,
                taken_at=row.taken_at,
                filters=row.filters_before,
            )

    def save(self, snapshot: FilterSnapshot) -> None:
        with self._sessions() as session:
            user = session.query(User).filter_by(plex_account_id=snapshot.plex_account_id).one()
            session.add(
                RestrictionSnapshotRow(
                    user_id=user.id,
                    taken_at=snapshot.taken_at,
                    reason="initial",
                    filters_before=snapshot.filters,
                    filters_after={},
                )
            )
            session.commit()


class DbCache:
    """Engine TMDB cache over the caches table."""

    def __init__(self, session_factory: sessionmaker[Session], kind: str = "tmdb"):
        self._sessions = session_factory
        self._kind = kind

    def get(self, key: str) -> str | None:
        with self._sessions() as session:
            row = session.get(CacheRow, (self._kind, key))
            if row and row.expires_at > time.time():
                return json.dumps(row.value)
            return None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        with self._sessions() as session:
            row = session.get(CacheRow, (self._kind, key))
            payload = json.loads(value)
            if row is None:
                session.add(CacheRow(kind=self._kind, key=key, value=payload, expires_at=time.time() + ttl_s))
            else:
                row.value = payload
                row.expires_at = time.time() + ttl_s
            session.commit()


class RunService:
    def __init__(self, session_factory: sessionmaker[Session], bus: EventBus, config_dir: Path, secret_box):
        self._sessions = session_factory
        self._bus = bus
        self._config_dir = config_dir
        self._secrets = secret_box
        self._lock = asyncio.Lock()  # one run at a time; nightly + manual runs must not overlap
        self._tasks: set[asyncio.Task] = set()  # strong refs so in-flight runs aren't GC'd

    # -- context assembly ----------------------------------------------------------------

    def build_context(self, *, dry_run: bool, loop: asyncio.AbstractEventLoop | None = None) -> EngineContext:
        with self._sessions() as session:
            store = SettingsStore(session, self._secrets)
            plex_url = store.get("plex.url")
            plex_token = store.get("plex.token")
            if not plex_url or not plex_token:
                raise RuntimeError("Plex connection is not configured yet — finish setup first")
            plex = PlexClient(plex_url, plex_token)
            plextv = PlexTvClient(plex_token, plex.machine_id, min_write_interval=float(store.get("plextv.throttle_s")))
            tmdb = TmdbClient(store.get("tmdb.apikey"), cache=DbCache(self._sessions))
            if store.get("tautulli.url"):
                history = FallbackHistorySource(
                    TautulliSource(TautulliClient(store.get("tautulli.url"), store.get("tautulli.apikey"))),
                    PlexHistorySource(plex),
                )
            else:
                history = PlexHistorySource(plex)
            curator_kwargs = {}
            if store.get("curator.api_key"):
                curator_kwargs["api_key"] = store.get("curator.api_key")
            if store.get("curator.model"):
                curator_kwargs["model"] = store.get("curator.model")
            curator = make_curator(store.get("curator.provider"), **curator_kwargs)
            config = EngineConfig(
                row_size=int(store.get("row.size")),
                row_name_template=store.get("row.name_template"),
                staleness_runs=int(store.get("staleness_runs")),
                dry_run=dry_run,
            )
            recent = self._recent_picks(session, config)

        def progress(slug: str, stage: str, counts: dict) -> None:
            if loop is not None:
                loop.call_soon_threadsafe(
                    self._bus.publish, "run.user.stage", {"user": slug, "stage": stage, "counts": counts}
                )

        return EngineContext(
            config=config,
            plex=plex,
            plextv=plextv,
            tmdb=tmdb,
            history_source=history,
            curator=curator,
            snapshots=DbSnapshotStore(self._sessions),
            recent_picks=recent,
            progress=progress,
        )

    def _recent_picks(self, session: Session, config: EngineConfig) -> dict[str, set[int]]:
        recent: dict[str, set[int]] = {}
        window = config.staleness_runs * config.row_size
        for user in session.query(User).filter_by(enabled=True).all():
            rows = (
                session.query(PickRow.tmdb_id)
                .filter_by(user_id=user.id)
                .order_by(PickRow.id.desc())
                .limit(window)
                .all()
            )
            recent[user.slug] = {r.tmdb_id for r in rows}
        return recent

    def enabled_profiles(self, session: Session, user_ids: list[int] | None = None) -> list[UserProfile]:
        """Enabled users, optionally narrowed to user_ids — never widened past enabled=True."""
        query = session.query(User).filter_by(enabled=True)
        if user_ids is not None:
            if not user_ids:
                return []
            query = query.filter(User.id.in_(user_ids))
        profiles = []
        for user in query.all():
            prefs = user.prefs or {}
            if prefs.get("paused"):
                continue
            profiles.append(
                UserProfile(
                    username=user.username,
                    plex_account_id=user.plex_account_id,
                    user_type=UserType(user.user_type),
                    slug=user.slug,
                    excluded_genres=set(prefs.get("excluded_genres") or []),
                    max_rating=prefs.get("max_rating"),
                    row_size=prefs.get("row_size"),
                    row_name_template=prefs.get("row_name_tpl"),
                )
            )
        return profiles

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
        from rowarr.server.db.models import Server
        from rowarr.server.services.privacy_state import gate_error

        with self._sessions() as session:
            server = session.query(Server).first()
            return gate_error(session, server.version if server else None)

    async def _execute(self, run_id: int, dry_run: bool, user_ids: list[int] | None) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            if not dry_run and (gate_error := self._privacy_gate_error()):
                logger.warning("run {} refused by privacy gate: {}", run_id, gate_error)
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    run.status = "error"
                    run.finished_at = datetime.now(UTC)
                    run.stats = {"error": f"privacy gate: {gate_error}"}
                    session.commit()
                self._bus.publish("run.finished", {"run_id": run_id, "status": "error"})
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
                with self._sessions() as session:
                    run = session.get(Run, run_id)
                    run.status = "error"
                    run.finished_at = datetime.now(UTC)
                    run.stats = {"error": f"{type(e).__name__}: {e}"}
                    session.commit()
                self._bus.publish("run.finished", {"run_id": run_id, "status": "error"})
                return
            self._bus.publish("run.finished", {"run_id": run_id, "status": status})

    def _persist_report(self, run_id: int, report) -> None:
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
                if not report.dry_run:
                    for pick in user_report.picks:
                        session.add(
                            PickRow(
                                run_id=run_id,
                                user_id=user.id,
                                tmdb_id=pick.tmdb_id,
                                rating_key=pick.rating_key,
                                rank=pick.rank,
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
                            "dry_run": report.dry_run,
                            "diff": user_report.diff.__dict__ if user_report.diff else {},
                            "privacy_synced": user_report.privacy_synced,
                            "error": user_report.error,
                        },
                    )
                )
            run.status = "ok" if errors == 0 else "error"
            run.finished_at = datetime.now(UTC)
            run.stats = {"users_ok": ok, "users_error": errors, "dry_run": report.dry_run}
            session.commit()
