"""Settings API: typed settings + connection tests (all re-testable in place)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from shortlist.engine.clients.http_retry import redact
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import DEFAULT_SLUG, Server
from shortlist.server.net_guard import BlockedUrl, check_url
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services import jobs
from shortlist.server.services.audit import add_audit
from shortlist.server.settings_store import DEFAULTS, PRIVATE_KEYS, SECRET_KEYS, SettingsStore

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_owner)])

# Private keys (e.g. the API token) are managed only via their own endpoints — never settable here,
# even though the token is a SECRET_KEY (which would otherwise make it PUT-able).
KNOWN_KEYS = (set(DEFAULTS) | SECRET_KEYS) - PRIVATE_KEYS


# The UI round-trips this in place of a secret it never received, so it means "leave it alone".
REDACTED_PLACEHOLDER = "•••••"

# What a secret's before/after reads as in the audit trail. The FACT of the change is auditable
# (rule 10); the value never is, in either direction (rule 9).
_AUDIT_SECRET = "<redacted>"

# A few settings hold whole objects (`rows.hub_anchor`, `candidates.sources`). The audit wants the
# fact and the shape of a change, not a second copy of the config, so long values are summarised.
_MAX_AUDIT_VALUE_CHARS = 200


def _audit_value(key: str, value: object) -> object:
    """One settings value as it may be written to the audit log."""
    if key in SECRET_KEYS:
        return _AUDIT_SECRET
    text = repr(value)
    return value if len(text) <= _MAX_AUDIT_VALUE_CHARS else f"{text[:_MAX_AUDIT_VALUE_CHARS]}… ({len(text)} chars)"


def _settings_diff(store: SettingsStore, values: dict[str, object]) -> dict[str, dict[str, object]]:
    """Old -> new for the keys this PUT actually CHANGES, ready for the audit log.

    The settings form PUTs the whole object, so most keys arrive unchanged — recording those would
    bury the one that moved. Secrets are compared (so a key rotation still registers as a change)
    but never recorded: `_audit_value` replaces both sides before anything reaches the event.

    Must be called BEFORE the writes — afterwards the old value is gone.
    """
    from shortlist.server.scheduler import DEFAULT_CRONS

    changed: dict[str, dict[str, object]] = {}
    for key, new in values.items():
        if key in SECRET_KEYS and new == REDACTED_PLACEHOLDER:
            continue  # the placeholder is not a new value; the write loop skips it too
        old = store.get(key)
        # For an off-able cron, `store.get` folds the DEFAULT in, so an ABSENT row and a STORED
        # blank both read as "" — switching the drift check off for the first time therefore looked
        # like no change at all and audited nothing. That is the one unattended job that writes
        # corrections to Plex and can delete a collection, so "who turned it off, and when" has to
        # be answerable (plex-safety rule 10).
        first_switch_off = key in DEFAULT_CRONS and new == "" and not store.has_row(key)
        # `null` means "back to the built-in default" (the write loop deletes the row). When there is
        # no row it changes nothing, so it must not audit — otherwise every save of a form that sends
        # the whole object logs a change that did not happen.
        if key in DEFAULT_CRONS and new is None and not store.has_row(key):
            continue
        if old == new and not first_switch_off:
            continue
        changed[key] = {"from": _audit_value(key, old), "to": _audit_value(key, new)}
    return changed


class SettingsUpdate(BaseModel):
    values: dict[str, object]


def _re_points_plex(values: dict[str, object]) -> bool:
    """Whether this write actually changes which Plex server we talk to.

    Mirrors the write loop's own skip: a redacted token round-tripped from the UI is not a new value,
    so it must not count. Treating it as a re-point would throw the cached library list away every
    time anyone saved the Settings page, putting Plex back on the next page load for no reason.
    """
    for key in ("plex.url", "plex.token"):
        if key not in values:
            continue
        if key in SECRET_KEYS and values[key] == REDACTED_PLACEHOLDER:
            continue
        return True
    return False


class CuratorModelsRequest(BaseModel):
    """Optional live overrides from the settings form so the model picker can list the provider being
    edited BEFORE it's saved. Any blank field falls back to the saved setting; a redacted api key
    ('•••••') means 'use the saved key'. The key is used only to build the client in memory — never
    logged (only the exception class is)."""

    provider: str | None = None
    api_key: str | None = None
    ollama_url: str | None = None


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


def _url_without_credentials(value: object) -> str | None:
    """Refuse a URL carrying `user:pass@` — the value must stay safe to store and to publish.

    `searxng.url` is deliberately not a SECRET_KEY: it is returned in the clear so the owner can read
    it back, and it is recorded verbatim in the `settings.change` audit event, which is immutable and
    is exported by the support bundle. A credential in there is unrecoverable. Stripping it inside
    `SearxngClient` protects that client's error strings only — far too late for the stored value.
    """
    # Parse the value the CONSUMERS use, which is the trimmed one (`test_connection` and
    # `make_search_client` both strip). Parsing the raw string instead let a single leading space
    # smuggle a password straight through: `httpx.URL(" http://u:p@h")` sees no authority at all and
    # reports empty credentials, so the check passed and the connection still worked perfectly.
    try:
        parsed = httpx.URL(str(value or "").strip())
    except Exception:
        return "must be a valid URL"
    if parsed.username or parsed.password:
        return (
            "must not contain a username or password — put those in the SearXNG username and "
            "password fields, where they are encrypted"
        )
    return None


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


def _int_list(value: object) -> str | None:
    """A list of TMDB ids. Reached only by API/config today (there is no UI for it), which is exactly
    why it needs validating — an untyped blob here would reach the engine as a set of whatever."""
    if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return "must be a list of whole numbers (TMDB ids)"
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
    "runs.retention": _bounded_int(0, 24),  # months; 0 = keep forever
    "events.retention": _bounded_int(0, 24),  # months; 0 = keep forever (the default)
    "sync.watch_incremental": _is_bool,
    "sync.watch_full_days": _bounded_int(1, 90),
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
    "llm_web.search_provider": _one_of("native", "exa", "searxng"),
    "searxng.url": _url_without_credentials,
    "recommendations.watched_pct": _bounded_float(0.0, 1.0),
    "recommendations.freshness": _bounded_float(0.0, 1.0),
    "recommendations.recency": _bounded_float(0.0, 1.0),
    "recommendations.recent_count": _bounded_int(1, 25),
    "recommendations.max_seeds": _bounded_int(5, 100),
    "recommendations.rating_source": _one_of("tmdb", "imdb", "trakt", "tomatoes", "metacritic"),
    # Floor of 1, not 0: at 0 nobody is ever cold, which silently disables the whole cold-start path
    # (and with it the "skip" setting below) in a way no owner would connect to this number.
    "recommendations.min_history": _bounded_int(1, 100),
    "recommendations.cold_start": _one_of("popular", "skip"),
    "recommendations.blocked_shared_seeds": _int_list,
    "recommendations.use_plex_ratings": _is_bool,
    # Ceiling of 6 (three stars), not 10: at 10 every rated title counts as disliked and every rating
    # anyone has ever given stops seeding, which is a setting whose only use is to break the feature.
    "recommendations.dislike_threshold": _bounded_float(0.0, 6.0),
    # Above 1 only affects READ-ONLY jobs — Plex writers stay exclusive whatever this says.
    "jobs.max_parallel_readonly": _bounded_int(1, 8),
    "log.level": _one_of("TRACE", "DEBUG", "INFO", "WARNING", "ERROR"),
    # "ollama" stays accepted: it is the pre-merge name for openai_compatible, and an instance
    # configured before the merge still has it stored.
    "curator.provider": _one_of("anthropic", "openai", "openai_compatible", "google", "ollama", "none", ""),
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


# Settings whose value the SERVER later fetches. Guarded as they are SAVED rather than at each
# consumer: one place to keep right, and a blocked address never reaches the store.
_FETCHED_URL_KEYS = (
    "plex.url",
    "tautulli.url",
    "requests.radarr.url",
    "requests.sonarr.url",
    "curator.ollama_url",
    "curator.openai_base_url",
    "searxng.url",  # fetched by the Test button and by the llm_web source on every run
    # NB: `curator_models` fetches an ollama_url WITHOUT saving it, so it checks the URL itself.
    # Anything else that fetches a caller-supplied URL without going through `PUT /settings` must
    # do the same — this tuple is not the only door.
)


def _reject_blocked_urls(values: dict[str, object]) -> None:
    """Refuse a URL the server must not fetch on the owner's behalf (SSRF — see `net_guard`).

    Narrow on purpose: private and loopback addresses stay ALLOWED, because `192.168.1.50:32400`,
    `http://plex:32400` and `http://localhost:11434` are the normal configuration for a self-hosted
    app. Only non-HTTP schemes and the cloud metadata addresses are refused.
    """
    for key in _FETCHED_URL_KEYS:
        value = values.get(key)
        if not value or not isinstance(value, str) or not value.strip():
            continue  # blank clears the setting — nothing to fetch
        try:
            check_url(value, what=f"{key}")
        except BlockedUrl as e:
            raise HTTPException(status_code=422, detail=str(e)) from e


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


@router.put("", response_model=SettingsOut)
async def put_settings(update: SettingsUpdate, request: Request) -> dict:
    unknown = set(update.values) - KNOWN_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown settings: {sorted(unknown)}")
    _validate_values(update.values)
    _reject_blocked_urls(update.values)
    await _reject_a_different_server(request.app.state, update.values)
    from shortlist.server.api.system import invalidate_plex_reads
    from shortlist.server.scheduler import DEFAULT_CRONS

    with request.app.state.sessions() as session:
        store = SettingsStore(session, request.app.state.secrets)
        # Two settings do real work on Plex, so their OLD values are read before the write. Storing
        # them used to be the whole of it: the toggle flipped, the page said "saved", and nothing on
        # the server changed until the next nightly run — or, for the row name, ever.
        was_hiding = bool(store.get("privacy.hide_shared_from_disabled"))
        old_row_name = (store.get("row.name_template") or "") if "row.name_template" in update.values else ""
        # Read the diff BEFORE the writes: afterwards there is no record of what the value was. Every
        # threshold here is owner-tunable and silently changeable, so "the run used different settings
        # than the ones you are reading" is invisible without this — reconstructing one such change
        # took a full forensic pass over settings timestamps vs run times (2026-08-01).
        changed = _settings_diff(store, update.values)
        for key, value in update.values.items():
            if key in SECRET_KEYS and value == REDACTED_PLACEHOLDER:
                continue  # redacted placeholder round-tripped from the UI — no change
            if value is None and key in DEFAULT_CRONS:
                # `null` on a schedulable cron means "go back to the built-in default", and the only
                # way to say that is to REMOVE the row: for `sync.check_cron` a stored blank means
                # OFF (scheduler._OFF_ABLE), so writing "" would switch the job off, and writing the
                # default expression would pin a copy of it rather than inherit it.
                store.unset(key)
                continue
            if key in _FETCHED_URL_KEYS and isinstance(value, str):
                # Store what the consumers actually fetch. A stored value that differs from the
                # parsed one is the seam a credential smuggled itself through once already.
                value = value.strip()
            store.set(key, value)
        if changed:
            add_audit(session, "settings.change", "info", changed=changed)
            session.commit()
        if "log.level" in update.values:
            # Apply immediately so a live "turn on DEBUG to watch this run" takes effect without a
            # container restart. The file sink is preserved from boot.
            from shortlist.logging_config import configure_logging

            configure_logging(str(update.values["log.level"]))
        # Derived from DEFAULT_CRONS, never a hand-written list: a hardcoded four-key set covered the
        # watch/user/backup crons only, so editing `privacy.sync_cron`, `sync.check_cron` or
        # `maintenance.prune_cron` saved the setting and left the live trigger alone until the next
        # container restart — and the drift check is the one schedule the UI offers to switch OFF,
        # so its off switch silently did nothing for the rest of the night.
        if set(update.values) & (set(DEFAULT_CRONS) | {"backup.max_keep"}):
            from shortlist.server.scheduler import rebuild_schedule

            rebuild_schedule(request.app)
        now_hiding = bool(store.get("privacy.hide_shared_from_disabled"))
        new_row_name = (store.get("row.name_template") or "") if "row.name_template" in update.values else old_row_name
        result = store.all_public()

    # A new URL or token may point at a different server, and the library list is cached by READ
    # rather than by server — so without this the picker would offer the previous server's libraries
    # for up to the cache TTL. Cheap, and only on a settings write.
    #
    # AFTER the write commits, never before it: `/libraries` runs its read on an executor thread, so
    # it genuinely interleaves with this handler. A read landing between an early drop and the commit
    # re-populates the cache from the OLD url/token and pins it for the whole TTL — precisely the
    # staleness this exists to prevent.
    if _re_points_plex(update.values):
        invalidate_plex_reads(request.app.state)

    # Both of these change what is on somebody's Plex server, so they act NOW rather than waiting for
    # a run. Outside the session block: each queues a job that opens its own.
    if now_hiding != was_hiding:
        # This toggle decides whether an opted-out account still sees the public shared rows. Flipping
        # it on owes every disabled account a `label!=` exclude; flipping it off owes them its removal.
        await jobs.queue_privacy_sync(request.app.state, "the 'hide shared rows from disabled users' setting changed")
    if new_row_name != old_row_name:
        # The default row's title IS this template, so changing it here renames every user's collection
        # — exactly what the Rows page already does through its own rename dialog. Without it, the next
        # run built a SECOND collection under the new name and left the old one labelled and promoted
        # for ever, because nothing addresses a collection by a title no run will write again.
        await reconcile.run_row_rename_from_plex(
            request.app.state,
            slug=DEFAULT_SLUG,
            new_template=new_row_name,
            old_template=old_row_name,
            scope="settings.rename",
        )
    return result


# A throwaway profile for the `native_search` probe: `build_web_prompt` reads only `.history` (via
# `taste_summary`), and an empty one asks for "well-reviewed titles to watch right now" — enough to
# prove the provider's web-search tool actually runs, without needing a real user.
_PROBE_PROFILE = SimpleNamespace(history=[])

_TESTABLE_SERVICES = frozenset(
    {"plex", "tautulli", "tmdb", "radarr", "sonarr", "mdblist", "trakt", "exa", "searxng", "native_search", "llm"}
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
                    raise RuntimeError(
                        "The provider answered, but its web search returned no titles. That usually "
                        "means the account's plan or model can't use the web-search tool. Choose Exa "
                        "or SearXNG as the search backend instead, or switch to a model that can."
                    )
                return f"ok — the provider's own web search returned {len(found)} titles"
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
        # plexapi/PMS exceptions can embed the tokened request URL — redact before it reaches the
        # API response (plex-safety rule 9: tokens never leave the box, even in an error string).
        return {"ok": False, "message": redact(f"{type(e).__name__}: {e}")}


class QualityProfileOut(PassthroughModel):
    id: int
    name: str


class RootFolderOut(PassthroughModel):
    id: int
    path: str


class ArrOptionsOut(PassthroughModel):
    quality_profiles: list[QualityProfileOut]
    root_folders: list[RootFolderOut]


@router.get("/arr/{service}/options", response_model=ArrOptionsOut)
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
    from loguru import logger

    from shortlist.server.services.context_builder import curator_kwargs

    body = body or CuratorModelsRequest()
    # The SSRF guard runs when these URLs are SAVED, and this endpoint fetches one WITHOUT saving it
    # — so the "one place to keep right" that `_FETCHED_URL_KEYS` documents had a second door. Owner
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
