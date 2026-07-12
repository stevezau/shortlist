"""FastAPI app factory: /api routers + SSE + static SPA, one process, one container."""

from __future__ import annotations

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

import rowarr
from rowarr.server import auth
from rowarr.server.api import events, privacy, runs, setup, system, users
from rowarr.server.api import settings as settings_api
from rowarr.server.db.models import Run, Server
from rowarr.server.db.session import make_engine, make_session_factory, run_migrations
from rowarr.server.scheduler import build_scheduler
from rowarr.server.services.run_service import RunService
from rowarr.server.services.secrets import SecretBox
from rowarr.server.services.sse import EventBus
from rowarr.server.settings_store import SECRET_KEYS, SettingsStore

WEB_DIST = Path(__file__).parent.parent.parent / "web" / "dist"


def _instance_secret(config_dir: Path, name: str) -> str:
    path = config_dir / name
    if not path.exists():
        path.write_text(pysecrets.token_urlsafe(48))
        os.chmod(path, 0o600)
    return path.read_text().strip()


def create_app(config_dir: Path | None = None) -> FastAPI:
    config_dir = config_dir or Path(os.environ.get("ROWARR_CONFIG", "/config"))
    config_dir.mkdir(parents=True, exist_ok=True)

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
            only the second one may open the door — so this counts EVERY secret Rowarr stores, not
            just the token (a curator key has no env-seed today, but SECRET_KEYS is the right list
            to guard against, not a hand-picked subset that drifts).
            """
            with sessions() as session:
                if session.query(Server).first() is not None:
                    return True
                store = SettingsStore(session, secret_box)
                return any(store.get(key) for key in SECRET_KEYS)

        app.state.owner_account_id = owner_account_id
        app.state.holds_secrets = holds_secrets

        with sessions() as session:
            SettingsStore(session, secret_box).seed_from_env(dict(os.environ))
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
        logger.info("rowarr server up (config: {})", config_dir)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)

    app = FastAPI(
        title="Rowarr",
        version=rowarr.__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    app.include_router(auth.router, prefix="/api")
    for module in (setup, users, runs, privacy, settings_api, system, events):
        app.include_router(module.router, prefix="/api")

    if WEB_DIST.exists():
        app.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):  # SPA fallback: every non-API path serves the app shell
            file = WEB_DIST / path
            if path and file.is_file():
                return FileResponse(file)
            return FileResponse(WEB_DIST / "index.html")

    return app


app = create_app() if os.environ.get("ROWARR_CONFIG") else None  # uvicorn target in the container
