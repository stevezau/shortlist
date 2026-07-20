"""Settings API: typed settings + connection tests (all re-testable in place)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from shortlist.engine.clients.http_retry import redact
from shortlist.server.auth import require_owner
from shortlist.server.settings_store import DEFAULTS, PRIVATE_KEYS, SECRET_KEYS, SettingsStore

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_owner)])

# Private keys (e.g. the API token) are managed only via their own endpoints — never settable here,
# even though the token is a SECRET_KEY (which would otherwise make it PUT-able).
KNOWN_KEYS = (set(DEFAULTS) | SECRET_KEYS) - PRIVATE_KEYS


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
# client reached the engine (`row.size: "abc"` crashed every run and 500'd two endpoints).
VALIDATORS = {
    "row.size": _bounded_int(5, 40),  # ceiling = candidates_pre_rank (per-media pool cap)
    "runs.retention": _bounded_int(0, 10000),  # 0 = keep every run
    # The FLOOR (minimum seconds) between plex.tv writes. 0 = fire as fast as plex.tv accepts; the
    # client backs off adaptively on 429 (rule 6), so 0 is safe, not an "off switch" like it once was.
    "plextv.throttle_s": _bounded_float(0.0, 60.0),
    "plex.timeout_s": _bounded_int(5, 300),  # per-PMS-call timeout; read unguarded in build_context
    "run.concurrency": _bounded_int(1, 16),  # 1 = sequential; writes stay serial regardless
    "paused_all": _is_bool,
    "requests.enabled": _is_bool,
    "requests.auto_send": _is_bool,
    "candidates.sources": _known_sources,
    "rows.hub_anchor": _hub_anchors,
    "llm_web.search_provider": _one_of("auto", "native", "exa"),
    "recommendations.watched_pct": _bounded_float(0.0, 1.0),
    "recommendations.freshness": _bounded_float(0.0, 1.0),
    "recommendations.recent_count": _bounded_int(1, 25),
    "log.level": _one_of("TRACE", "DEBUG", "INFO", "WARNING", "ERROR"),
    "curator.provider": _one_of("anthropic", "openai", "google", "ollama", "none"),
    "curator.prompt_tone": _one_of("balanced", "warm", "concise", "cinephile", "playful"),
    "requests.rating_source": _one_of("tmdb", "imdb", "trakt", "tomatoes", "metacritic"),
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


@router.get("/prompt-default")
async def prompt_default(shared: bool = False) -> dict:
    """The built-in curation prompt as an editable template, so the 'write the whole prompt yourself'
    box can start from the default rather than a blank slate. ``shared`` picks the group-row wording."""
    from shortlist.engine.curator.base import default_prompt_template

    return {"template": default_prompt_template(shared)}


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
    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        for key, value in update.values.items():
            if key in SECRET_KEYS and value == "•••••":
                continue  # redacted placeholder round-tripped from the UI — no change
            store.set(key, value)
        if "log.level" in update.values:
            # Apply immediately so a live "turn on DEBUG to watch this run" takes effect without a
            # container restart. The file sink is preserved from boot.
            from shortlist.logging_config import configure_logging

            configure_logging(str(update.values["log.level"]))
        return store.all_public()


_TESTABLE_SERVICES = frozenset({"plex", "tautulli", "tmdb", "radarr", "sonarr", "mdblist", "trakt", "exa", "llm"})


@router.post("/test/{service}")
async def test_connection(service: str, request: Request) -> dict:
    """One tiny call per service; returns plain-English ok/error (design: everything re-testable)."""
    state = request.app.state
    if service not in _TESTABLE_SERVICES:
        raise HTTPException(status_code=404, detail=f"unknown service {service!r}")

    def probe() -> str:
        # Own session in the executor thread, and only the tested service's secret is decrypted — no
        # reason to Fernet-decrypt every stored key just to ping one connection.
        with state.sessions() as session:
            get = SettingsStore(session, state.secrets).get
            if service == "plex":
                from shortlist.engine.clients.plex_pms import PlexClient

                plex = PlexClient(get("plex.url"), get("plex.token"))
                return f"Connected to {plex.server_name} (PMS {plex.version})"
            if service == "tautulli":
                from shortlist.engine.clients.tautulli import TautulliClient

                TautulliClient(get("tautulli.url"), get("tautulli.apikey")).ping()
                return "Tautulli responded"
            if service == "tmdb":
                from shortlist.engine.clients.tmdb import TmdbClient

                if not TmdbClient(get("tmdb.apikey")).ping():
                    raise RuntimeError("TMDB rejected the key")
                return "TMDB key works"
            if service in ("radarr", "sonarr"):
                from shortlist.engine.clients.arr import make_arr_client
                from shortlist.engine.models import ArrTarget

                prefix = f"requests.{service}"
                url = (get(f"{prefix}.url") or "").strip()
                api_key = get(f"{prefix}.apikey") or ""
                if not url or not api_key:
                    raise RuntimeError(f"{service.title()} URL and API key are both required")
                target = ArrTarget(url=url, api_key=api_key, quality_profile_id=0, root_folder="")
                return make_arr_client(service, target).ping()
            if service == "mdblist":
                from shortlist.engine.clients.mdblist import MdbListClient

                api_key = get("requests.mdblist.apikey") or ""
                if not api_key:
                    raise RuntimeError("An MDBList API key is required for IMDb/Trakt/RT/Metacritic ratings")
                return MdbListClient(api_key).ping()
            if service == "trakt":
                from shortlist.engine.clients.trakt import TraktClient

                client_id = get("trakt.client_id") or ""
                if not client_id:
                    raise RuntimeError("A Trakt API key (client id) is required")
                return TraktClient(client_id).ping()
            if service == "exa":
                from shortlist.engine.clients.search import ExaClient

                api_key = get("exa.apikey") or ""
                if not api_key:
                    raise RuntimeError("An Exa API key is required for AI web search")
                return ExaClient(api_key).ping()
            # service == "llm"
            from shortlist.engine.curator import make_curator
            from shortlist.server.services.context_builder import curator_kwargs

            curator = make_curator(get("curator.provider"), **curator_kwargs(get))
            if hasattr(curator, "ping"):
                return f"Curator replied: {curator.ping()!r}"
            return "Built-in picker — no AI, nothing to test, always works"

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
        from shortlist.engine.clients.arr import make_arr_client
        from shortlist.engine.models import ArrTarget

        target = ArrTarget(url=url, api_key=api_key, quality_profile_id=0, root_folder="")
        client = make_arr_client(service, target)
        return {"quality_profiles": client.quality_profiles(), "root_folders": client.root_folders()}

    try:
        return await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        raise HTTPException(status_code=502, detail=redact(f"{type(e).__name__}: {e}")) from e


@router.get("/curator/models")
async def curator_models(request: Request) -> dict:
    """Model ids the configured AI provider offers, for the setup model picker.

    Reads the SAVED provider + key server-side (a key is never accepted in the request). Best-effort:
    a provider that can't list — no key on file yet, an offline Ollama, or a provider/proxy without a
    models endpoint — returns an empty list, and the UI falls back to the free-text model field.
    """
    from loguru import logger

    from shortlist.server.services.context_builder import curator_kwargs

    state = request.app.state
    with state.sessions() as session:
        get = SettingsStore(session, state.secrets).get
        provider = (get("curator.provider") or "none").lower()
        kwargs = curator_kwargs(get)
    if provider in ("none", "null", ""):
        return {"provider": provider, "models": []}

    def fetch() -> list[str]:
        from shortlist.engine.curator import make_curator

        lister = getattr(make_curator(provider, **kwargs), "list_models", None)
        return list(lister()) if callable(lister) else []

    try:
        models = await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        # A failed listing is expected (bad/absent key, offline server) — never fatal. Log ONLY the
        # exception class, never its message: an LLM SDK can embed the api_key in the error text in a
        # shape redact() doesn't cover (e.g. Google's `?key=AIza…`), so the safe move is to not render
        # it at all (rule 9). The UI just shows the free-text field.
        logger.info("curator model list unavailable ({})", type(e).__name__)
        models = []
    return {"provider": provider, "models": models}
