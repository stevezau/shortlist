"""Settings API: typed settings + connection tests (all re-testable in place)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from rowarr.server.auth import require_owner
from rowarr.server.scheduler import reschedule
from rowarr.server.settings_store import DEFAULTS, SECRET_KEYS, SettingsStore

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_owner)])

KNOWN_KEYS = set(DEFAULTS) | SECRET_KEYS


class SettingsUpdate(BaseModel):
    values: dict[str, object]


@router.get("")
async def get_settings(request: Request) -> dict:
    with request.app.state.sessions() as session:
        return SettingsStore(session, request.app.state.secrets).all_public()


@router.put("")
async def put_settings(update: SettingsUpdate, request: Request) -> dict:
    unknown = set(update.values) - KNOWN_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown settings: {sorted(unknown)}")
    if "schedule.cron" in update.values:
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(str(update.values["schedule.cron"]))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=f"invalid cron expression: {e}") from e
    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        for key, value in update.values.items():
            if key in SECRET_KEYS and value == "•••••":
                continue  # redacted placeholder round-tripped from the UI — no change
            store.set(key, value)
        if "schedule.cron" in update.values:
            reschedule(request.app, str(update.values["schedule.cron"]))
        return store.all_public()


@router.post("/test/{service}")
async def test_connection(service: str, request: Request) -> dict:
    """One tiny call per service; returns plain-English ok/error (design: everything re-testable)."""
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        config = {key: store.get(key) for key in list(DEFAULTS) + list(SECRET_KEYS)}

    def probe() -> str:
        if service == "plex":
            from rowarr.engine.clients.plex import PlexClient

            plex = PlexClient(config["plex.url"], config["plex.token"])
            return f"Connected to {plex._server.friendlyName} (PMS {plex.version})"
        if service == "tautulli":
            from rowarr.engine.clients.tautulli import TautulliClient

            TautulliClient(config["tautulli.url"], config["tautulli.apikey"]).ping()
            return "Tautulli responded"
        if service == "tmdb":
            from rowarr.engine.clients.tmdb import TmdbClient

            TmdbClient(config["tmdb.apikey"]).ping()
            return "TMDB key works"
        if service == "llm":
            from rowarr.engine.curator import make_curator

            kwargs = {}
            if config["curator.api_key"]:
                kwargs["api_key"] = config["curator.api_key"]
            if config["curator.model"]:
                kwargs["model"] = config["curator.model"]
            curator = make_curator(config["curator.provider"], **kwargs)
            if hasattr(curator, "ping"):
                return f"Curator replied: {curator.ping()!r}"
            return "Heuristic mode — nothing to test, always works"
        raise HTTPException(status_code=404, detail=f"unknown service {service!r}")

    try:
        message = await asyncio.get_running_loop().run_in_executor(None, probe)
        return {"ok": True, "message": message}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
