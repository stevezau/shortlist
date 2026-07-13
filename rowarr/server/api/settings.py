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


class PromptPreviewRequest(BaseModel):
    tone: str = "balanced"
    guidance: str = ""
    template: str = ""
    shared: bool = False


@router.post("/prompt-preview")
async def prompt_preview(body: PromptPreviewRequest, request: Request) -> dict:
    """Assemble the system+user prompt from a recipe against fixed sample data, so the owner can see
    the effect of a tone/guidance/template before saving. Uses the configured row size for k."""
    from rowarr.engine.curator.base import build_prompts
    from rowarr.engine.curator.preview import sample_preview_inputs
    from rowarr.engine.models import PromptConfig

    with request.app.state.sessions() as session:
        k = int(SettingsStore(session, request.app.state.secrets).get("row.size"))

    profile, candidates = sample_preview_inputs(
        PromptConfig(tone=body.tone, guidance=body.guidance, template=body.template, shared=body.shared)
    )
    system, user = build_prompts(profile, candidates, k)
    return {"system": system, "user": user}


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
            from rowarr.engine.clients.plex_pms import PlexClient

            plex = PlexClient(config["plex.url"], config["plex.token"])
            return f"Connected to {plex.server_name} (PMS {plex.version})"
        if service == "tautulli":
            from rowarr.engine.clients.tautulli import TautulliClient

            TautulliClient(config["tautulli.url"], config["tautulli.apikey"]).ping()
            return "Tautulli responded"
        if service == "tmdb":
            from rowarr.engine.clients.tmdb import TmdbClient

            if not TmdbClient(config["tmdb.apikey"]).ping():
                raise RuntimeError("TMDB rejected the key")
            return "TMDB key works"
        if service in ("radarr", "sonarr"):
            from rowarr.engine.clients.arr import RadarrClient, SonarrClient
            from rowarr.engine.models import ArrTarget

            prefix = f"requests.{service}"
            url = (config[f"{prefix}.url"] or "").strip()
            api_key = config[f"{prefix}.apikey"] or ""
            if not url or not api_key:
                raise RuntimeError(f"{service.title()} URL and API key are both required")
            target = ArrTarget(url=url, api_key=api_key, quality_profile_id=0, root_folder="")
            client = (RadarrClient if service == "radarr" else SonarrClient)(target)
            return client.ping()
        if service == "omdb":
            from rowarr.engine.clients.omdb import OmdbClient

            api_key = config["requests.omdb.apikey"] or ""
            if not api_key:
                raise RuntimeError("An OMDb API key is required for IMDb ratings")
            return OmdbClient(api_key).ping()
        if service == "llm":
            from rowarr.engine.curator import make_curator

            provider = config["curator.provider"]
            kwargs = {}
            if provider == "ollama":
                kwargs["base_url"] = config["curator.ollama_url"]
            elif config["curator.api_key"]:
                kwargs["api_key"] = config["curator.api_key"]
            if config["curator.model"]:
                kwargs["model"] = config["curator.model"]
            curator = make_curator(provider, **kwargs)
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


@router.get("/arr/{service}/options")
async def arr_options(service: str, request: Request) -> dict:
    """Quality profiles + root folders for a connected Sonarr/Radarr, so the UI offers dropdowns
    rather than asking a non-technical owner to hunt down numeric profile ids and server paths."""
    if service not in ("radarr", "sonarr"):
        raise HTTPException(status_code=404, detail=f"unknown service {service!r}")
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        url = (store.get(f"requests.{service}.url") or "").strip()
        api_key = store.get(f"requests.{service}.apikey") or ""
    if not url or not api_key:
        raise HTTPException(status_code=409, detail=f"{service.title()} isn't connected yet")

    def fetch() -> dict:
        from rowarr.engine.clients.arr import RadarrClient, SonarrClient
        from rowarr.engine.models import ArrTarget

        target = ArrTarget(url=url, api_key=api_key, quality_profile_id=0, root_folder="")
        client = (RadarrClient if service == "radarr" else SonarrClient)(target)
        return {"quality_profiles": client.quality_profiles(), "root_folders": client.root_folders()}

    try:
        return await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e
