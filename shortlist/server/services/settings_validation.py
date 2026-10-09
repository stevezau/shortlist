"""Settings validation shared by the browser API and the assistant: which keys exist, what each may hold, and
the audit diff of a change. No request handling here, so services can use it without importing from `api`."""

from __future__ import annotations

import re
from collections.abc import Callable

import httpx
from fastapi import HTTPException

from shortlist.engine.clients.search import EXA_SEARCH_TYPES
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
from shortlist.server.net_guard import BlockedUrl, check_url
from shortlist.server.settings_store import DEFAULTS, PRIVATE_KEYS, SECRET_KEYS, SettingsStore

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


def settings_diff(store: SettingsStore, values: dict[str, object]) -> dict[str, dict[str, object]]:
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


def validate_values(values: dict[str, object]) -> None:
    problems = [f"{key}: {problem}" for key, value in values.items() if (problem := _check(key, value))]
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(sorted(problems)))


def _check(key: str, value: object) -> str | None:
    validator = VALIDATORS.get(key)
    return validator(value) if validator else None


# Settings whose value the SERVER later fetches. Guarded as they are SAVED rather than at each
# consumer: one place to keep right, and a blocked address never reaches the store.
FETCHED_URL_KEYS = (
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


def reject_blocked_urls(values: dict[str, object]) -> None:
    """Refuse a URL the server must not fetch on the owner's behalf (SSRF — see `net_guard`).

    Narrow on purpose: private and loopback addresses stay ALLOWED, because `192.168.1.50:32400`,
    `http://plex:32400` and `http://localhost:11434` are the normal configuration for a self-hosted
    app. Only non-HTTP schemes and the cloud metadata addresses are refused.
    """
    for key in FETCHED_URL_KEYS:
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
