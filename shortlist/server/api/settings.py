"""Settings API: typed settings + connection tests (all re-testable in place)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shortlist.engine.clients.http_retry import redact
from shortlist.server.auth import require_owner
from shortlist.server.scheduler import reschedule
from shortlist.server.settings_store import DEFAULTS, SECRET_KEYS, SettingsStore

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_owner)])

KNOWN_KEYS = set(DEFAULTS) | SECRET_KEYS


class SettingsUpdate(BaseModel):
    values: dict[str, object]


def _bounded_int(low: int, high: int):
    def check(value: object) -> str | None:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return f"must be a whole number between {low} and {high}"
        return None if low <= number <= high else f"must be between {low} and {high}"

    return check


def _bounded_float(low: float, high: float):
    def check(value: object) -> str | None:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return f"must be a number between {low} and {high}"
        return None if low <= number <= high else f"must be between {low} and {high}"

    return check


def _one_of(*allowed: str):
    def check(value: object) -> str | None:
        return None if str(value) in allowed else f"must be one of {', '.join(allowed)}"

    return check


def _is_bool(value: object) -> str | None:
    # A non-empty STRING is truthy in Python, so "false" would have switched paused_all ON while the
    # UI read it as off. Only real booleans are accepted.
    return None if isinstance(value, bool) else "must be true or false"


def _hub_anchors(value: object) -> str | None:
    """`{sectionKey: {"top": true} | {"anchor": str, "before": bool}}` — the per-library
    Recommended-shelf placement. A `top` entry needs no anchor; otherwise `anchor` must be non-empty.
    An empty dict clears it. Bad shapes reached the engine and skipped ordering silently."""
    if not isinstance(value, dict):
        return "must be an object keyed by library id"
    for key, entry in value.items():
        if not isinstance(key, str):
            return "library ids must be strings"
        if not isinstance(entry, dict):
            return f"{key}: must be an object with 'top', or 'anchor' and 'before'"
        if entry.get("top"):
            continue  # top mode ignores anchor/before
        anchor = entry.get("anchor")
        if not isinstance(anchor, str) or not anchor.strip():
            return f"{key}: needs 'top', or a non-empty 'anchor' title"
        if not isinstance(entry.get("before", False), bool):
            return f"{key}: 'before' must be true or false"
    return None


def _known_sources(value: object) -> str | None:
    from shortlist.engine.candidates import KNOWN_SOURCES

    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return "must be a list of source names"
    unknown = [v for v in value if v not in KNOWN_SOURCES]
    return f"unknown source(s) {unknown}; valid: {sorted(KNOWN_SOURCES)}" if unknown else None


# Values the UI already constrains — but the API accepted anything, so a bad value from any other
# client reached the engine. `plextv.throttle_s: 0` silently REMOVED the <=1 write/s plex.tv throttle
# (plex-safety rule 6); `row.size: "abc"` crashed every run and 500'd two endpoints.
VALIDATORS = {
    "row.size": _bounded_int(5, 40),  # ceiling = candidates_pre_rank (per-media pool cap)
    "staleness_runs": _bounded_int(0, 50),
    "plextv.throttle_s": _bounded_float(1.0, 60.0),  # never below the 1 write/s rule
    "run.concurrency": _bounded_int(1, 16),  # 1 = sequential; writes stay serial regardless
    "paused_all": _is_bool,
    "requests.enabled": _is_bool,
    "requests.auto_send": _is_bool,
    "candidates.sources": _known_sources,
    "rows.hub_anchor": _hub_anchors,
    "llm_web.search_provider": _one_of("auto", "native", "exa"),
    "recommendations.watched_pct": _bounded_float(0.0, 1.0),
    "recommendations.freshness": _bounded_float(0.0, 1.0),
    "log.level": _one_of("TRACE", "DEBUG", "INFO", "WARNING", "ERROR"),
    "curator.provider": _one_of("anthropic", "openai", "google", "ollama", "none"),
    "curator.prompt_tone": _one_of("balanced", "warm", "concise", "cinephile", "playful"),
    "requests.rating_source": _one_of("tmdb", "imdb"),
    "requests.min_rating": _bounded_float(0.0, 10.0),
    "requests.auto_min_rating": _bounded_float(0.0, 10.0),
    "requests.min_votes": _bounded_int(0, 1_000_000),
    "requests.min_demand": _bounded_int(1, 1000),
    "requests.auto_min_demand": _bounded_int(1, 1000),
    "requests.min_year": _bounded_int(0, 2100),
    "requests.max_year": _bounded_int(0, 2100),
    "requests.max_per_run": _bounded_int(0, 100),
    "requests.radarr.quality_profile_id": _bounded_int(0, 1_000_000),
    "requests.sonarr.quality_profile_id": _bounded_int(0, 1_000_000),
}


def _validate_values(values: dict[str, object]) -> None:
    problems = [f"{key}: {problem}" for key, value in values.items() if (problem := _check(key, value))]
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(sorted(problems)))


def _check(key: str, value: object) -> str | None:
    validator = VALIDATORS.get(key)
    return validator(value) if validator else None


class PromptPreviewRequest(BaseModel):
    tone: str = "balanced"
    guidance: str = ""
    template: str = ""
    shared: bool = False


@router.post("/prompt-preview")
async def prompt_preview(body: PromptPreviewRequest, request: Request) -> dict:
    """Assemble the system+user prompt from a recipe against fixed sample data, so the owner can see
    the effect of a tone/guidance/template before saving. Uses the configured row size for k."""
    from shortlist.engine.curator.base import build_prompts
    from shortlist.engine.curator.preview import sample_preview_inputs
    from shortlist.engine.models import PromptConfig

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
    _validate_values(update.values)
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
        if "log.level" in update.values:
            # Apply immediately so a live "turn on DEBUG to watch this run" takes effect without a
            # container restart. The file sink is preserved from boot.
            from shortlist.logging_config import configure_logging

            configure_logging(str(update.values["log.level"]))
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
            from shortlist.engine.clients.plex_pms import PlexClient

            plex = PlexClient(config["plex.url"], config["plex.token"])
            return f"Connected to {plex.server_name} (PMS {plex.version})"
        if service == "tautulli":
            from shortlist.engine.clients.tautulli import TautulliClient

            TautulliClient(config["tautulli.url"], config["tautulli.apikey"]).ping()
            return "Tautulli responded"
        if service == "tmdb":
            from shortlist.engine.clients.tmdb import TmdbClient

            if not TmdbClient(config["tmdb.apikey"]).ping():
                raise RuntimeError("TMDB rejected the key")
            return "TMDB key works"
        if service in ("radarr", "sonarr"):
            from shortlist.engine.clients.arr import RadarrClient, SonarrClient
            from shortlist.engine.models import ArrTarget

            prefix = f"requests.{service}"
            url = (config[f"{prefix}.url"] or "").strip()
            api_key = config[f"{prefix}.apikey"] or ""
            if not url or not api_key:
                raise RuntimeError(f"{service.title()} URL and API key are both required")
            target = ArrTarget(url=url, api_key=api_key, quality_profile_id=0, root_folder="")
            client = (RadarrClient if service == "radarr" else SonarrClient)(target)
            return client.ping()
        if service == "omdb":
            from shortlist.engine.clients.omdb import OmdbClient

            api_key = config["requests.omdb.apikey"] or ""
            if not api_key:
                raise RuntimeError("An OMDb API key is required for IMDb ratings")
            return OmdbClient(api_key).ping()
        if service == "trakt":
            from shortlist.engine.clients.trakt import TraktClient

            client_id = config["trakt.client_id"] or ""
            if not client_id:
                raise RuntimeError("A Trakt API key (client id) is required")
            return TraktClient(client_id).ping()
        if service == "exa":
            from shortlist.engine.clients.search import ExaClient

            api_key = config["exa.apikey"] or ""
            if not api_key:
                raise RuntimeError("An Exa API key is required for AI web search")
            return ExaClient(api_key).ping()
        if service == "llm":
            from shortlist.engine.curator import make_curator

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
        # plexapi/PMS exceptions can embed the tokened request URL — redact before it reaches the
        # API response (plex-safety rule 9: tokens never leave the box, even in an error string).
        return {"ok": False, "message": redact(f"{type(e).__name__}: {e}")}


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
        from shortlist.engine.clients.arr import RadarrClient, SonarrClient
        from shortlist.engine.models import ArrTarget

        target = ArrTarget(url=url, api_key=api_key, quality_profile_id=0, root_folder="")
        client = (RadarrClient if service == "radarr" else SonarrClient)(target)
        return {"quality_profiles": client.quality_profiles(), "root_folders": client.root_folders()}

    try:
        return await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        raise HTTPException(status_code=502, detail=redact(f"{type(e).__name__}: {e}")) from e
