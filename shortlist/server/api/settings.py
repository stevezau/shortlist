"""Settings API: typed settings + connection tests (all re-testable in place)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from shortlist.engine.clients.arr import ArrError
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.clients.search import EXA_SEARCH_TYPES
from shortlist.engine.clients.seerr import SeerrError
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Server
from shortlist.server.net_guard import BlockedUrl, check_url
from shortlist.server.schema_base import PassthroughModel
from shortlist.server.services import jobs
from shortlist.server.services.audit import actor_of, add_audit
from shortlist.server.services.connection_choices import (
    ArrNotConfigured,
    configured_arr_connection,
    read_arr_choices,
    read_curator_models,
)
from shortlist.server.services.plex_reachability import error_text
from shortlist.server.services.settings_validation import (
    KNOWN_KEYS,
    reject_blocked_urls,
    validate_values,
)
from shortlist.server.settings_store import DEFAULTS, PRIVATE_KEYS, SECRET_KEYS, SettingsStore

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_owner)])


class SettingsUpdate(BaseModel):
    values: dict[str, object]


class CuratorModelsRequest(BaseModel):
    """Optional live overrides from the settings form so the model picker can list the provider being
    edited BEFORE it's saved. Any blank field falls back to the saved setting; a redacted api key
    ('•••••') means 'use the saved key'. The key is used only to build the client in memory — never
    logged (only the exception class is)."""

    provider: str | None = None
    api_key: str | None = None
    ollama_url: str | None = None


async def _reject_a_different_server(state, values: dict[str, object]) -> None:
    """Refuse a `plex.url`/`plex.token` edit that points at a DIFFERENT Plex server.

    Everything Shortlist knows is scoped to one machine: which collection is whose (the delivery
    ledger), whose share filters were snapshotted before we touched them, which account is the owner.
    Silently repointing at another server leaves all of that describing a machine nobody is talking to
    — and the next reconcile would go looking for those collections on a server that never had them.

    Changing servers is a re-link (setup), not a settings edit, so this says so instead of guessing.
    A read failure is NOT a rejection: the box may simply be down or the URL not reachable yet, and
    refusing to save a URL because it does not answer would make a broken connection unfixable.
    """
    if not (set(values) & {"plex.url", "plex.token"}):
        return
    with state.sessions() as session:
        server = session.query(Server).first()
        if server is None:
            return  # not linked yet — this IS the link, and setup owns that path
        store = SettingsStore(session, state.secrets)
        url = str(values.get("plex.url") or store.get("plex.url") or "")
        token = values.get("plex.token")
        token = str(store.get("plex.token") or "") if token in (None, "•••••") else str(token)
    if not url or not token:
        return

    def probe() -> str | None:
        from shortlist.engine.clients.plex_pms import PlexClient

        try:
            return PlexClient(url, token).machine_id
        except Exception as e:
            logger.info("could not read the machine id while saving Plex settings ({})", type(e).__name__)
            return None

    machine_id = await asyncio.get_running_loop().run_in_executor(None, probe)
    if machine_id and machine_id != server.machine_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "That points at a different Plex server. Shortlist's rows, share-filter snapshots and "
                "user list all belong to the server it is linked to, so switching is a re-link rather "
                "than a settings change — uninstall from Settings → Danger Zone first, then set up again."
            ),
        )


class SettingsOut(PassthroughModel):
    """The whole settings store, flat: `{"row.size": 15, "plex.url": "…", …}`.

    Deliberately declares NO fields. The key set is genuinely dynamic — `settings_store.DEFAULTS`
    plus whatever rows the database holds, minus `PRIVATE_KEYS` — so enumerating it here would be a
    second copy of `DEFAULTS` that silently goes stale, and a strict model would DROP every key it
    had not caught up with. ``extra="allow"`` passes all of them through untouched, which is the
    honest description of this endpoint: an open map, with secrets already redacted to "•••••" by
    `all_public()`.
    """


@router.get("", response_model=SettingsOut)
async def get_settings(request: Request) -> dict:
    with request.app.state.sessions() as session:
        return SettingsStore(session, request.app.state.secrets).all_public()


@router.get("/defaults", response_model=SettingsOut)
async def get_setting_defaults() -> dict:
    """Every setting's built-in default, so the page can mark one the owner has changed.

    Secrets and private keys are left out: a default carries no credential, and what is stored under
    those keys is never this endpoint's to say.
    """
    return {key: value for key, value in DEFAULTS.items() if key not in PRIVATE_KEYS and key not in SECRET_KEYS}


@router.put("", response_model=SettingsOut)
async def put_settings(
    update: SettingsUpdate,
    request: Request,
    # `Annotated`, not `= Depends(...)`: ruff's B008 refuses a call in a default. Declaring the
    # router's own dependency again is free — FastAPI caches it per request, so `require_owner`
    # authenticates once and this just receives what it returned.
    auth: Annotated[dict, Depends(require_owner)],
) -> dict:
    unknown = set(update.values) - KNOWN_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown settings: {sorted(unknown)}")
    validate_values(update.values)
    reject_blocked_urls(update.values)
    await _reject_a_different_server(request.app.state, update.values)
    from shortlist.server.assistant.row_effects import queue_convergence_in_session
    from shortlist.server.services.settings_mutations import apply_settings_in_session, prepare_settings_in_session

    state = request.app.state
    with state.sessions() as session:
        mutation = prepare_settings_in_session(session, state.secrets, update.values)
        apply_settings_in_session(session, state.secrets, mutation)
        if mutation.changed:
            add_audit(session, "settings.change", "info", changed=mutation.changed, actor=actor_of(auth, request))
        if mutation.steps:
            queue_convergence_in_session(session, list(mutation.steps), domain="settings")
        session.commit()
        result = SettingsStore(session, state.secrets).all_public()
    if mutation.steps:
        await jobs.drain_now(state, "settings changed")
    return result


# A throwaway profile for the `native_search` probe: `build_web_prompt` reads only `.history` (via
# `taste_summary`), and an empty one asks for "well-reviewed titles to watch right now" — enough to
# prove the provider's web-search tool actually runs, without needing a real user.
_PROBE_PROFILE = SimpleNamespace(history=[])

_TESTABLE_SERVICES = frozenset(
    {
        "plex",
        "tautulli",
        "tmdb",
        "radarr",
        "sonarr",
        "overseerr",
        "mdblist",
        "trakt",
        "exa",
        "searxng",
        "native_search",
        "notify",
        "llm",
    }
)


class ConnectionTestOut(PassthroughModel):
    """`message` is plain English either way — the success line, or a redacted failure (rule 9)."""

    ok: bool
    message: str


@router.post("/test/{service}", response_model=ConnectionTestOut)
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
            from shortlist.server.services.connection_checks import READ_ONLY_PROBES, probe_read_only_connection

            if service in READ_ONLY_PROBES:
                return probe_read_only_connection(service, get)
            if service == "exa":
                from shortlist.engine.clients.search import ExaClient

                api_key = get("exa.apikey") or ""
                if not api_key:
                    raise RuntimeError("An Exa API key is required for AI web search")
                # Ping on the CHEAPEST OFFERED mode, whatever the configured one: Test should answer
                # in a couple of seconds and cost as little as possible, and proving the key is the
                # only thing this button claims to do.
                #
                # Taken from EXA_SEARCH_TYPES rather than named literally: a literal for a dropped mode
                # is clamped by `ExaClient` to the DEFAULT, so every auto-test on the Settings page
                # would silently run `deep-lite` at 1.7x the price.
                return ExaClient(api_key, search_type=EXA_SEARCH_TYPES[0]).ping()
            if service == "native_search":
                # A REAL web search, not a capability lookup. `supports_native_web_search` says the
                # provider offers the tool; it cannot say this account's plan or model may use it.
                # When it may not, the call fails at run time, logs a warning and returns no titles —
                # so the source silently contributes nothing every night and nothing in the UI says
                # so. One small live call at setup is what turns that into an answer.
                from shortlist.engine.curator import make_curator
                from shortlist.server.services.context_builder import curator_kwargs

                curator = make_curator(get("curator.provider"), **curator_kwargs(get))
                if not getattr(curator, "supports_native_web_search", False):
                    raise RuntimeError(
                        "This AI provider cannot search the web on its own — only Claude, GPT and "
                        "Gemini can. Choose Exa or SearXNG as the search backend, or change provider."
                    )
                found = curator.recommend_web(_PROBE_PROFILE, [], 3)
                if not found:
                    # Every native curator catches provider errors and returns `[]`, so an empty list
                    # means EITHER "found nothing" OR "the call failed" — indistinguishable here. A
                    # plain completion tells them apart: if it raises, the fault is the provider (a
                    # revoked key, a bad model, no outbound route) and THAT is what to report.
                    # Blaming the web-search tool would send someone with an expired key off to sign
                    # up for a paid search vendor, on the one button meant to diagnose them.
                    curator.ping()
                    raise RuntimeError(
                        "The provider answered, but its web search returned no titles. That usually "
                        "means the account's plan or model can't use the web-search tool. Choose Exa "
                        "or SearXNG as the search backend instead, or switch to a model that can."
                    )
                return f"ok — the provider's own web search returned {len(found)} titles"
            if service == "notify":
                # The one test on this page that is not a ping: it really posts a message, because a
                # test button that exercised its own private send path would prove nothing about the
                # 3am one. Same `deliver`, same body builder, same settings — only the trigger differs.
                from shortlist.server.services import notify

                return notify.deliver(SettingsStore(session, state.secrets), notify.sample_item())
            if service == "searxng":
                from shortlist.engine.clients.search import SearxngClient

                url = (get("searxng.url") or "").strip()
                if not url:
                    raise RuntimeError("A SearXNG address is required for local AI web search")
                return SearxngClient(
                    url, username=get("searxng.username") or "", password=get("searxng.password") or ""
                ).ping()
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
        # plexapi/PMS exceptions can embed the tokened request URL — `error_text` redacts before it
        # reaches the API response (plex-safety rule 9: tokens never leave the box, even in an error string).
        return {"ok": False, "message": error_text(e)}


class QualityProfileOut(PassthroughModel):
    id: int
    name: str


class RootFolderOut(PassthroughModel):
    id: int
    path: str


class ArrOptionsOut(PassthroughModel):
    quality_profiles: list[QualityProfileOut]
    root_folders: list[RootFolderOut]


def _origin_of(url: str) -> str:
    """``scheme://host[:port]`` of a configured address: no credentials, path or query string."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}" if parsed.scheme and host else "the address you entered"


def _service_error_detail(app: str, url: str, error: Exception) -> str:
    """Plain-English reason a Sonarr/Radarr/Overseerr call failed, safe to show in the UI.

    The clients' own messages ("Radarr rejected the API key") already read well and carry no secret,
    so they pass through; an "unreachable (ConnectError)" and anything unexpected become a sentence
    naming the address. The exception type and text go to the server log only.
    """
    logger.warning("{} request failed ({}): {}", app, type(error).__name__, redact(str(error)))
    text = redact(str(error))
    if isinstance(error, ArrError | SeerrError) and "unreachable" not in text:
        return text
    return f"{app} didn't answer at {_origin_of(url)}. Check the address and that it's running."


@router.get("/arr/{service}/options", response_model=ArrOptionsOut)
async def arr_options(service: str, request: Request) -> dict:
    """Quality profiles + root folders for a connected Sonarr/Radarr, so the UI offers dropdowns
    rather than asking a non-technical owner to hunt down numeric profile ids and server paths."""
    if service not in ("radarr", "sonarr"):
        raise HTTPException(status_code=404, detail=f"unknown service {service!r}")
    state = request.app.state
    try:
        connection = configured_arr_connection(state, service)
    except ArrNotConfigured as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    try:
        return await asyncio.get_running_loop().run_in_executor(None, read_arr_choices, service, connection)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_service_error_detail(service.title(), connection.url, e)) from e


class SeerrUserOut(PassthroughModel):
    id: int
    name: str
    # Whether this account's requests skip Overseerr's own approval queue. The screen needs it to say
    # what picking the account will actually DO, rather than leaving the owner to find out later.
    auto_approve_movies: bool = False
    auto_approve_tv: bool = False
    # True for a real person on the server, false for a local account made inside Overseerr. Drives
    # the grouping in the picker — see the note on `is_plex_user` in the client.
    is_plex_user: bool = False


class SeerrOptionsOut(PassthroughModel):
    users: list[SeerrUserOut]
    # Which of those accounts the API key itself is, so the UI can resolve "Server default" to a real
    # row and say whether it approves. None when the instance would not say.
    default_user_id: int | None = None


@router.get("/overseerr/options", response_model=SeerrOptionsOut)
async def overseerr_options(request: Request) -> dict:
    """The instance's accounts, so the UI can offer a "request as" dropdown.

    The *seerr equivalent of ``arr_options``, and deliberately much smaller: quality profiles and
    root folders are Overseerr's business on this route, so the only choice left to Shortlist is
    whose name the request goes out under.
    """
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        url = (store.get("requests.overseerr.url") or "").strip()
        api_key = store.get("requests.overseerr.apikey") or ""
    if not url or not api_key:
        raise HTTPException(status_code=409, detail="Overseerr isn't connected yet")

    def fetch() -> dict:
        from shortlist.engine.clients.seerr import SeerrClient
        from shortlist.engine.models import SeerrTarget

        client = SeerrClient(SeerrTarget(url=url, api_key=api_key))
        return {"users": client.users(), "default_user_id": client.whoami()}

    try:
        return await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        raise HTTPException(status_code=502, detail=_service_error_detail("Overseerr", url, e)) from e


class CuratorModelsOut(PassthroughModel):
    """The provider the listing was made for (so a stale reply can be told apart from a live one),
    and its model ids. Best-effort: `models` is empty when the provider cannot be asked."""

    provider: str
    models: list[str]


@router.post("/curator/models", response_model=CuratorModelsOut)
async def curator_models(request: Request, body: CuratorModelsRequest | None = None) -> dict:
    """Model ids an AI provider offers, for the model picker.

    Lists the provider being edited: the request may carry the (unsaved) provider + key/URL from the
    settings form, so switching provider or typing a new key updates the dropdown live. Blank fields
    fall back to the SAVED settings, and a redacted key means 'use the saved key'. The key builds the
    client in memory only — never logged (only the exception CLASS is, since an SDK can embed the key
    in error text). Best-effort: no key yet, an offline Ollama, or a provider without a models
    endpoint returns an empty list, and the UI falls back to the free-text override.
    """

    from shortlist.server.services.context_builder import curator_kwargs

    body = body or CuratorModelsRequest()
    # The SSRF guard runs when these URLs are SAVED, and this endpoint fetches one WITHOUT saving it
    # — so the "one place to keep right" that `FETCHED_URL_KEYS` documents had a second door. Owner
    # -gated, so not a drive-by, but it defeated a control this codebase deliberately built.
    if body.ollama_url:
        try:
            check_url(body.ollama_url, what="The AI server URL")
        except BlockedUrl as e:
            raise HTTPException(422, str(e)) from e
    overrides = {
        "curator.provider": body.provider,
        "curator.api_key": body.api_key,
        # The picker sends a local server's URL under the pre-merge field name; it feeds the one
        # local/OpenAI-compatible provider's base URL, so set both keys from it.
        "curator.ollama_url": body.ollama_url,
        "curator.openai_base_url": body.ollama_url,
    }
    state = request.app.state
    with state.sessions() as session:
        saved = SettingsStore(session, state.secrets).get

        def get(key: str) -> object:
            # A supplied override wins, except the redacted placeholder which means "the saved key".
            override = overrides.get(key)
            if override and override != "•••••":
                return override
            return saved(key)

        provider = (get("curator.provider") or "none").lower()
        kwargs = curator_kwargs(get)
    choices = await asyncio.get_running_loop().run_in_executor(None, read_curator_models, provider, kwargs)
    return {"provider": provider, "models": choices.models}
