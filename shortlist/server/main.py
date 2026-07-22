"""FastAPI app factory: /api routers + SSE + static SPA, one process, one container."""

from __future__ import annotations

import hmac
import logging
import os
import secrets as pysecrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.responses import FileResponse

import shortlist
from shortlist.logging_config import configure_logging, normalize_level
from shortlist.server import auth
from shortlist.server.api import (
    collections,
    events,
    notifications,
    report,
    requests,
    runs,
    setup,
    system,
    tools,
    user_rows,
    users,
)
from shortlist.server.api import settings as settings_api
from shortlist.server.db.models import Run, Server
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.scheduler import build_scheduler
from shortlist.server.services.run_service import RunService
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.settings_store import SECRET_KEYS, SettingsStore

WEB_DIST = Path(__file__).parent.parent.parent / "web" / "dist"


def _instance_secret(config_dir: Path, name: str) -> str:
    path = config_dir / name
    if not path.exists():
        path.write_text(pysecrets.token_urlsafe(48))
        os.chmod(path, 0o600)
    return path.read_text().strip()


class _AccessNoiseFilter(logging.Filter):
    """Drop the health-check + SSE access-log lines that otherwise flood `docker logs` every few
    seconds and bury the app's own run logs. Every other request is still logged."""

    _NOISY = ("/api/system/health", "/api/events")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(path in message for path in self._NOISY)


def create_app(config_dir: Path | None = None) -> FastAPI:
    config_dir = config_dir or Path(os.environ.get("SHORTLIST_CONFIG", "/config"))
    config_dir.mkdir(parents=True, exist_ok=True)
    # Quiet uvicorn's per-request access log for the noise endpoints, so a run's DEBUG narration is
    # actually readable in `docker logs`.
    logging.getLogger("uvicorn.access").addFilter(_AccessNoiseFilter())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        run_migrations(config_dir)
        engine = make_engine(config_dir)
        sessions = make_session_factory(engine)
        secret_box = SecretBox(config_dir)
        bus = EventBus()

        app.state.config_dir = config_dir
        app.state.sessions = sessions
        app.state.secrets = secret_box
        app.state.bus = bus
        app.state.session_secret = _instance_secret(config_dir, "session.secret")
        app.state.client_id = _instance_secret(config_dir, "client.id")[:32] or str(uuid.uuid4())
        app.state.run_service = RunService(sessions, bus, config_dir, secret_box)
        app.state.started_at = datetime.now(UTC)
        # Plex tokens minted during setup, held server-side only (account_id -> token).
        app.state.pending_plex_tokens = {}

        def owner_account_id() -> int | None:
            with sessions() as session:
                server = session.query(Server).first()
                return server.owner_account_id if server else None

        def holds_secrets() -> bool:
            """Is there anything on this instance worth protecting yet?

            A linked server is the obvious case. The subtle one — and the kind that made an earlier
            version of the open-wizard gate a secret-exfiltration hole — is a credential the
            environment seeds with no server row: `PLEX_TOKEN` or `TAUTULLI_APIKEY` (docker-compose
            ships these commented out). Either is a real, working secret an attacker would want.
            "Nobody has claimed it" and "there is nothing to steal" are NOT the same question, and
            only the second one may open the door — so this counts EVERY secret Shortlist stores, not
            just the token (a curator key has no env-seed today, but SECRET_KEYS is the right list
            to guard against, not a hand-picked subset that drifts).
            """
            with sessions() as session:
                if session.query(Server).first() is not None:
                    return True
                store = SettingsStore(session, secret_box)
                return any(store.get(key) for key in SECRET_KEYS)

        def verify_api_token(token: str) -> bool:
            """True iff ``token`` matches the stored owner API token (decrypted, constant-time compare).

            Fails closed on any error (e.g. a rotated/corrupt secret.key that can't decrypt) — a bad
            token must yield a clean 401, never a 500.
            """
            if not token:
                return False
            try:
                with sessions() as session:
                    stored = SettingsStore(session, secret_box).get(auth.API_TOKEN_KEY)
            except Exception:
                logger.exception("API-token verify failed to read/decrypt the stored token")
                return False
            return bool(stored) and hmac.compare_digest(str(stored), token)

        app.state.owner_account_id = owner_account_id
        app.state.holds_secrets = holds_secrets
        app.state.verify_api_token = verify_api_token

        with sessions() as session:
            store = SettingsStore(session, secret_box)
            store.purge_legacy()  # drop stale rows from removed settings (e.g. old API-token hash)
            store.seed_from_env(dict(os.environ))
            # Configure logging from the DB setting (seeded from LOG_LEVEL on first boot). The
            # rotating file sink under /config/logs always captures DEBUG, so a quiet console still
            # leaves a full on-disk trail to diagnose a run after the fact.
            (config_dir / "logs").mkdir(parents=True, exist_ok=True)
            configure_logging(store.get("log.level"), log_file=str(config_dir / "logs" / "shortlist.log"))
            # State the console level plainly at boot, so `docker logs` answers "is DEBUG on?" at a
            # glance (the file at /config/logs is always DEBUG regardless).
            logger.info(
                "logging ready — console at {} (docker logs), file always DEBUG at /config/logs/shortlist.log",
                normalize_level(store.get("log.level")),
            )
            stale = session.query(Run).filter(Run.status.in_(("queued", "running"))).all()
            for run in stale:
                run.status = "aborted"
                run.finished_at = datetime.now(UTC)
            if stale:
                logger.warning("aborted {} orphaned run(s) from a previous process", len(stale))
            session.commit()

        scheduler = build_scheduler(app)
        scheduler.start()
        app.state.scheduler = scheduler
        from shortlist.server.safe_mode import force_dry_run, misconfigured_dry_run

        if force_dry_run():
            logger.warning("SHORTLIST_DRY_RUN is ON — safe mode: nothing will be written to Plex/plex.tv")
        elif (bad := misconfigured_dry_run()) is not None:
            logger.warning(
                "SHORTLIST_DRY_RUN={!r} is not a recognized value (use 1/true/yes/on) — safe mode is OFF", bad
            )
        logger.info("shortlist server up (config: {})", config_dir)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)

    # The interactive API docs + schema disclose the whole API surface unauthenticated. They're off
    # by default (nothing sensitive, but no reason to advertise); set SHORTLIST_ENABLE_DOCS=1 to
    # re-enable them for local development.
    docs_enabled = os.environ.get("SHORTLIST_ENABLE_DOCS") == "1"
    app = FastAPI(
        title="Shortlist",
        version=shortlist.__version__,
        lifespan=lifespan,
        docs_url="/api/docs" if docs_enabled else None,
        openapi_url="/api/openapi.json" if docs_enabled else None,
    )

    app.include_router(auth.router, prefix="/api")
    for module in (
        setup,
        users,
        user_rows,
        runs,
        collections,
        requests,
        settings_api,
        system,
        events,
        report,
        tools,
        notifications,
    ):
        app.include_router(module.router, prefix="/api")

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")
        web_root = WEB_DIST.resolve()
        index = web_root / "index.html"

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):  # SPA fallback: every non-API path serves the app shell
            if path:
                # Containment guard: `path` is caller-controlled and uvicorn does NOT collapse
                # `..`/`%2e%2e`, so a crafted `../../config/secret.key` would otherwise escape the
                # bundle and leak the Fernet key + DB. Resolve and require the target stays inside
                # web_root before serving it as a file (plex-safety: secrets never leave the box).
                target = (web_root / path).resolve()
                if target.is_relative_to(web_root) and target.is_file():
                    return FileResponse(target)
            return FileResponse(index)

    return app


app = create_app() if os.environ.get("SHORTLIST_CONFIG") else None  # uvicorn target in the container
