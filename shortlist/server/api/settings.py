"""Settings API: typed settings + connection tests (all re-testable in place)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from types import SimpleNamespace
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel

from shortlist.engine.clients.arr import ArrError
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.clients.search import EXA_SEARCH_TYPES
from shortlist.engine.clients.seerr import SeerrError
from shortlist.engine.models import (
    LANGUAGE_MODES,
    MAX_REFRESH_DAYS,
    MAX_ROW_SIZE,
    MIN_ROW_SIZE,
    REQUEST_TARGETS,
    SONARR_MONITOR_MODES,
)
from shortlist.engine.placeholders import refusal
from shortlist.engine.web_guidance import MAX_INSTRUCTIONS_CHARS
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Server
from shortlist.server.net_guard import BlockedUrl, check_url
from shortlist.server.services import jobs
from shortlist.server.services.audit import actor_of, add_audit
from shortlist.server.services.connection_choices import (
    ArrNotConfigured,
    configured_arr_connection,
    read_arr_choices,
    read_curator_models,
)
from shortlist.server.services.plex_reachability import error_text
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

# A few settings hold whole objects (`candidates.sources`). The audit wants the
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


def _non_blank_row_template(value: object) -> str | None:
    """The default row's title may not be blank — a blank one collapses onto every other row.

    `render_row_name` returns "" for a blank or whitespace-only template (issue #84 — it no longer
    substitutes a name), so storing one here leaves the default row with no title at all and it stops
    being delivered to anybody. Before that change it silently retitled the row to "✨ Picked for You"
    in every library instead. Either way nothing refused it, which made the failure this guard exists
    to prevent reachable in two ordinary requests.
    """
    if not str(value or "").strip():
        return "cannot be empty — it is the title of your default row"
    return refusal(str(value), "global_name")


def _one_of(*allowed: str):
    def check(value: object) -> str | None:
        return None if str(value) in allowed else f"must be one of {', '.join(allowed)}"

    return check


def _text_at_most(limit: int) -> Callable[[object], str | None]:
    """Free text up to ``limit`` characters (no other free-text setting caps its length yet)."""

    def check(value: object) -> str | None:
        if not isinstance(value, str):
            return "must be text"
        if len(value) > limit:
            return f"must be at most {limit} characters"
        return None

    return check


def _is_bool(value: object) -> str | None:
    # A non-empty STRING is truthy in Python, so "false" would have switched paused_all ON while the
    # UI read it as off. Only real booleans are accepted.
    return None if isinstance(value, bool) else "must be true or false"


def _int_list(value: object) -> str | None:
    """A list of TMDB ids. Reached only by API/config today (there is no UI for it), which is exactly
    why it needs validating — an untyped blob here would reach the engine as a set of whatever."""
    if not isinstance(value, list) or not all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return "must be a list of whole numbers (TMDB ids)"
    return None


_MAX_PREFERRED_LANGUAGES = 50


def _language_codes(value: object) -> str | None:
    """A list of ISO 639-1 language codes, as TMDB reports `original_language`.

    Two letters is the whole shape TMDB uses, so anything else is a typo that would silently classify
    every title as "other" and quietly raise the bar on the entire library. An EMPTY list is legal and
    meaningful — in "only" mode it means "request nothing" — so this checks the shape, not the length.
    """
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return "must be a list of language codes"
    # Capped to match the per-row column's own `max_length=50`. Unbounded, this is a setting an owner
    # can store megabytes into, and it is read on every run — the ceiling is cheaper than the audit.
    if len(value) > _MAX_PREFERRED_LANGUAGES:
        return f"too many languages (max {_MAX_PREFERRED_LANGUAGES})"
    # `[a-z]{2}`, not `.isalpha()`: str.isalpha() is Unicode-aware, so a two-character CJK string
    # passes — and worse, so does a Cyrillic homoglyph pair, which renders identically to its Latin
    # spelling in the error message the owner reads back. Either matches no TMDB `original_language`,
    # so "only" mode would silently stop requesting anything. (Ruff's RUF003 flags the homoglyph if
    # you try to write one here, which is the same hazard from the other direction.)
    bad = [v for v in value if not re.fullmatch(r"[a-z]{2}", v.strip().lower())]
    # Only the first few are echoed: the message goes back in an error body, and repeating a large
    # rejected list there just amplifies whatever was sent. The remainder is COUNTED, or an owner
    # fixes the five they were shown and is rejected again for values the message implied were fine.
    if not bad:
        return None
    more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
    return f"not ISO 639-1 language codes: {bad[:5]}{more} (two letters, e.g. 'en', 'ja')"


def _optional_bounded_float(low: float, high: float):
    """A number in range, or None — where None is a MEANING, not an omission.

    `requests.min_rating_other` uses this: None means "follow min_rating + 1.5", which is the shipped
    default precisely so no fixed number of ours is imposed on anyone's server.
    """
    inner = _bounded_float(low, high)

    def check(value: object) -> str | None:
        return None if value is None else inner(value)

    return check


_MAX_HOLD_TAGS = 50
_MAX_TAG_NAME = 200


def _movie_genre_ids(value: object) -> str | None:
    """TMDB movie genre ids, each once: the settings page offers only TMDB's fixed list."""
    from shortlist.engine.themes import MOVIE_GENRE_IDS

    if not isinstance(value, list) or not all(type(v) is int for v in value):
        return "must be a list of TMDB movie genre ids"
    if len(set(value)) != len(value):
        return "lists a genre twice"
    unknown = [v for v in value if v not in MOVIE_GENRE_IDS]
    return f"unknown TMDB movie genre id(s) {unknown}" if unknown else None


def _tmdb_tags(value: object) -> str | None:
    """``{"<TMDB tag id>": "<its name>"}``, as the settings page's TMDB tag search returns them.

    The name is stored beside the id so the page can show it without asking TMDB again; the run matches
    on the id alone.
    """
    if not isinstance(value, dict):
        return "must map TMDB tag ids to their names"
    if len(value) > _MAX_HOLD_TAGS:
        return f"too many tags (max {_MAX_HOLD_TAGS})"
    for tag_id, name in value.items():
        if not (isinstance(tag_id, str) and tag_id.isascii() and tag_id.isdigit() and int(tag_id) > 0):
            return f"{tag_id!r} is not a TMDB tag id"
        if not isinstance(name, str) or not name.strip() or len(name) > _MAX_TAG_NAME:
            return f"tag {tag_id} needs a name of 1-{_MAX_TAG_NAME} characters"
    return None


def _known_sources(value: object) -> str | None:
    from shortlist.engine.candidates import KNOWN_SOURCES

    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return "must be a list of source names"
    unknown = [v for v in value if v not in KNOWN_SOURCES]
    return f"unknown source(s) {unknown}; valid: {sorted(KNOWN_SOURCES)}" if unknown else None


# Values the UI already constrains — but the API accepted anything, so a bad value from any other
# client reached the engine (`row.size: "abc"` crashed every run and 500'd two endpoints).
def _notify_events(value: object) -> str | None:
    from shortlist.server.services.notify import EVENTS

    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return "must be a list of event names"
    unknown = [v for v in value if v not in EVENTS]
    return f"unknown event(s) {unknown}; valid: {list(EVENTS)}" if unknown else None


#: RFC 7230 `token`: the characters an HTTP header name may contain.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def _header_name(value: object) -> str | None:
    # Blank means "send no header", and is how the owner stops sending one without removing the webhook.
    if not isinstance(value, str) or (value and not _HEADER_NAME.fullmatch(value)):
        return "must be a header name such as Authorization or X-Api-Key (letters, digits and dashes)"
    return None


#: h11's own rule for a header value: printable ASCII, with spaces or tabs only between words.
_HEADER_VALUE = re.compile(r"[\x21-\x7e]+(?:[ \t]+[\x21-\x7e]+)*")


def _header_value(value: object) -> str | None:
    # Anything h11 refuses fails every send at 3am, and its error quotes the value back in an escaped form.
    # Empty clears it. The redacted placeholder is left to the write loop, which keeps the stored value.
    if isinstance(value, str) and (value in ("", REDACTED_PLACEHOLDER) or _HEADER_VALUE.fullmatch(value)):
        return None
    return "must be printable text with no leading or trailing spaces, e.g. Bearer abc123"


VALIDATORS = {
    "notify.webhook.events": _notify_events,
    "notify.webhook.auth_header_name": _header_name,
    "notify.webhook.auth_header_value": _header_value,
    # `candidates_pre_rank` is derived from this ceiling (2x), so the pool always clears the largest
    # legal row — it used to be a flat 40 restated here, which met the ceiling and left no headroom.
    "row.size": _bounded_int(MIN_ROW_SIZE, MAX_ROW_SIZE),
    "runs.retention": _bounded_int(0, 24),  # months; 0 = keep forever
    "events.retention": _bounded_int(0, 24),  # months; 0 = keep forever (the default)
    "sync.watch_full_days": _bounded_int(1, 90),
    # The FLOOR (minimum seconds) between plex.tv writes. 0 = fire as fast as plex.tv accepts; the
    # client backs off adaptively on 429 (rule 6), so 0 is safe, not an "off switch" like it once was.
    "plextv.throttle_s": _bounded_float(0.0, 60.0),
    "plex.timeout_s": _bounded_int(5, 300),  # per-PMS-call timeout; read unguarded in build_context
    "run.concurrency": _bounded_int(1, 16),  # 1 = sequential; writes stay serial regardless
    "paused_all": _is_bool,
    "requests.enabled": _is_bool,
    "requests.target": _one_of(*REQUEST_TARGETS),
    "requests.auto_send": _is_bool,
    "requests.hold_genres": _movie_genre_ids,
    "requests.hold_tags": _tmdb_tags,
    "candidates.sources": _known_sources,
    "llm_web.search_provider": _one_of("native", "exa", "searxng"),
    "llm_web.instructions": _text_at_most(MAX_INSTRUCTIONS_CHARS),
    # Validated here as well as clamped in the client: a typo saved through the API would otherwise
    # be a 400 from Exa on every seed of every run, and the owner would see an empty row, not a bad
    # setting. The client's fallback is the second line of defence, for a value written before this.
    "exa.search_type": _one_of(*EXA_SEARCH_TYPES),
    "searxng.url": _url_without_credentials,
    "recommendations.watched_pct": _bounded_float(0.0, 1.0),
    "recommendations.genre_avoidance": _bounded_float(0.0, 1.0),
    "recommendations.franchise": _bounded_float(0.0, 1.0),
    "recommendations.cast": _bounded_float(0.0, 1.0),
    # Bounded, and not only for tidiness. `ContextBuilder.build` consumes this with a bare
    # `float()`, so an unvalidated "30s" wedges every run and every context-building job with
    # no way back except editing the DB — and the sweep sleeps this PER CANDIDATE while holding
    # the Plex writer lock, so a large value stalls the run and everything queued behind it.
    "plex.orphan_confirm_delay_s": _bounded_float(0.0, 300.0),
    # Refresh cadence in days. 0 = frozen; the ceiling is a validation bound, not a behaviour cap —
    # the old 0..1 fraction could not express anything slower than a fortnight, and a monthly or
    # quarterly row is a legitimate thing to want.
    "recommendations.refresh_days": _bounded_int(0, MAX_REFRESH_DAYS),
    "recommendations.idle_hold_days": _bounded_int(0, MAX_REFRESH_DAYS),
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
    "requests.language_mode": _one_of(*LANGUAGE_MODES),
    "requests.preferred_languages": _language_codes,
    # Optional on purpose: None is "follow min_rating + 1.5", not "unset". See `_optional_bounded_float`.
    "requests.min_rating_other": _optional_bounded_float(0.0, 10.0),
    "requests.min_votes": _bounded_int(0, 1_000_000),
    "requests.min_demand": _bounded_int(1, 1000),
    "requests.auto_min_demand": _bounded_int(1, 1000),
    "requests.min_year": _bounded_int(0, 2100),
    "requests.max_year": _bounded_int(0, 2100),
    "requests.max_per_run": _bounded_int(0, 100),
    "requests.overseerr.request_as_user_id": _bounded_int(0, 1_000_000),
    "requests.radarr.quality_profile_id": _bounded_int(0, 1_000_000),
    "requests.sonarr.quality_profile_id": _bounded_int(0, 1_000_000),
    # Sonarr 400s the whole add on a value outside its enum, so the typo is refused here rather than
    # discovered a fortnight later as a request that never arrived.
    "requests.sonarr.monitor": _one_of(*SONARR_MONITOR_MODES),
    "row.name_template": _non_blank_row_template,
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
    "requests.overseerr.url",
    "requests.radarr.url",
    "requests.sonarr.url",
    "curator.ollama_url",
    "curator.openai_base_url",
    "searxng.url",  # fetched by the Test button and by the llm_web source on every run
    # POSTed to by `notify.send` for every event the owner chose, and by the Send-a-test button. Being an
    # outbound alert rather than an integration does not change what it is: a URL the server fetches
    # because the owner typed it.
    "notify.webhook.url",
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
        # `notify.webhook.url` is the first key that is BOTH a fetched URL and a secret, so the
        # redacted sentinel now reaches this guard. It means "leave the stored value alone", exactly
        # as it does in the write loop and in `_re_points_plex` — checking it as an address would
        # 422 the whole settings save every time anyone pressed Save with a webhook configured.
        if key in SECRET_KEYS and value == REDACTED_PLACEHOLDER:
            continue
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
    _validate_values(update.values)
    _reject_blocked_urls(update.values)
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
                # Taken from EXA_SEARCH_TYPES rather than named literally. It used to hardcode
                # "fast"; when that mode was dropped for returning no titles, `ExaClient` clamped the
                # unknown value to the DEFAULT — so every auto-test on the Settings page silently ran
                # `deep-lite` at 1.7x the price and logged a warning nobody had asked for.
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

                return notify.deliver(SettingsStore(session, state.secrets), notify.test_item())
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
    choices = await asyncio.get_running_loop().run_in_executor(None, read_curator_models, provider, kwargs)
    return {"provider": provider, "models": choices.models}
