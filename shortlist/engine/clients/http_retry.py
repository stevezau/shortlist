"""Shared retry + backoff for the engine's HTTP service clients.

Every outbound call (TMDB, Tautulli, Trakt, Arr, OMDb, plex.tv reads) goes through here so a
transient blip — a read timeout, a dropped connection, an HTTP 429/5xx — is retried with exponential
backoff instead of failing the whole run. (Run 3 on SFLIX died on a single 30s PMS read timeout.)

Two entry points, split by HTTP safety:

* ``get`` — for idempotent reads. Retries the widest set: any timeout or transport error, plus 429
  and 5xx responses. A GET can always be safely repeated.
* ``request`` — for mutations (POST/PUT/DELETE). Retries ONLY when the request provably never
  reached the server (a connect error / connect timeout) or the server explicitly rate-limited it
  (429). Never on a read timeout or a 5xx, because the mutation may have already applied and a blind
  retry would double it (a second Radarr add, a second filter write).

A server's ``Retry-After`` header is honoured (capped) over the computed backoff.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable

import httpx
from loguru import logger

_SECRET_RE = re.compile(r"((?:X-Plex-Token|api_?key)=)[^&\s\"']+", re.IGNORECASE)

# Credentials that can appear in a string but NOT as a query parameter, so `_SECRET_RE` misses them —
# a header form, a JSON/dict body, or a bare provider key. Most of these were previously in
# `server/services/log_reader.scrub` alone; `redact()` is what guards API 502 details and `events`
# rows (plex-safety rule 9 applies equally there), so it must be at least as strong. `X-Api-Key` —
# the header `arr.py` sends — was missed by BOTH ladders before this: `api_?key` in the JSON pattern
# below only matches a quoted key of exactly "api_key"/"apikey", not "X-Api-Key". Each pattern keeps
# its label and replaces only the secret.
_EXTRA_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Header form: `X-Plex-Token: abc123`, `'X-Plex-Token': 'abc123'`, `X-Api-Key: abc123` (Sonarr/Radarr).
    # The value stops at `&` too, not just whitespace/quotes/brackets — this pattern runs AFTER
    # `_SECRET_RE` above, so on a query-string form it would otherwise re-match the just-redacted
    # `X-Plex-Token=REDACTED` and swallow the rest of the query string with it (e.g. `&foo=1`).
    (
        re.compile(r"((?:X-Plex-Token|X-Plex-Client-Identifier|X-Api-Key)['\"]?\s*[:=]\s*['\"]?)[^\s,&'\"}\]]+", re.I),
        r"\1REDACTED",
    ),
    # `Authorization: Bearer abc123` — our own API token, and any other bearer credential. `Basic`
    # too: a SearXNG instance behind a reverse proxy authenticates that way, and base64 of
    # `user:password` is a plaintext credential to anyone who reads the log. The value class carries
    # `+/=` for base64 as well as the bearer alphabet — over-redaction is the safe direction.
    (
        re.compile(r"((?:Authorization['\"]?\s*[:=]\s*['\"]?)?(?:Bearer|Basic)\s+)[A-Za-z0-9._+/\-]{8,}={0,2}", re.I),
        r"\1REDACTED",
    ),
    # JSON/dict form: `"token": "abc"`, `'apikey': 'abc'`, `"api_key": "abc"`.
    (re.compile(r"(['\"](?:token|api_?key|authToken|accessToken)['\"]\s*:\s*['\"])[^'\"]+", re.I), r"\1REDACTED"),
    # Provider key shapes, wherever they appear: Anthropic, OpenAI, Google, xAI, Groq.
    # The OpenAI pattern must allow `-` and `_` INSIDE the key, not just after `sk-`: every key
    # issued since 2024 is `sk-proj-…`, and OpenRouter — which this provider now supports — uses
    # `sk-or-v1-…`. An alnum-only class stops dead at the hyphen after `proj`/`or` and matches
    # neither. Over-redaction is the safe direction here.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}"), "REDACTED"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"), "REDACTED"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "REDACTED"),
    (re.compile(r"\bxai-[A-Za-z0-9_\-]{20,}"), "REDACTED"),
    (re.compile(r"\bgsk_[A-Za-z0-9_\-]{20,}"), "REDACTED"),
    # Credentials inline in a URL: `http://user:pass@host`. The settings boundary refuses this shape
    # for `searxng.url`, but a URL reaches logs and `events` rows from plenty of other places, so the
    # password is stripped here too. Only the secret half goes — the user and host stay readable, so
    # the line still says which service failed.
    (re.compile(r"\b(https?://[^/\s:@]+):[^/\s@]+@"), r"\1:REDACTED@"),
    # Plex tokens are 20-char alnum; catch the bare `token=`/`X-Plex-Token` path form too.
    (re.compile(r"(plex\.direct[^\s]*?token[=/])[A-Za-z0-9_\-]+", re.I), r"\1REDACTED"),
)


def redact(text: str) -> str:
    """Strip every credential shape we know of from a string before it is logged or persisted
    (plex-safety rule 9).

    plexapi/PMS error text (and other clients' errors) can embed the full request URL, credential and
    all — a Plex ``X-Plex-Token`` or ``X-Plex-Client-Identifier`` header, a Tautulli/TMDB/OMDb
    ``apikey``/``api_key`` query param, a bearer token, a JSON-shaped credential field, or a bare
    provider API key. Anything derived from an exception message must pass through here before it
    reaches a log line or an ``events`` row. Over-redaction is fine; a leaked key is not."""
    cleaned = _SECRET_RE.sub(r"\1REDACTED", text)
    for pattern, replacement in _EXTRA_SECRETS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


DEFAULT_ATTEMPTS = 3
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 20.0
MAX_RETRY_AFTER_S = 60.0  # cap an honoured Retry-After here — a longer server hint is clamped, not obeyed,
#                            so one slow endpoint can't stall the whole run on its own say-so.

# Shared ceiling for the engine's read APIs (TMDB/Trakt/Arr/Tautulli/plex.tv): long enough that a
# slow host doesn't fail a normal call, short enough that one dead endpoint doesn't stall a whole
# run. Each client may override with its own one-line reason (e.g. MDBList's shorter fail-fast).
DEFAULT_TIMEOUT_S = 30.0

# GET (idempotent): any transient network error is retriable, as is a rate-limit or server error.
_GET_RETRY_EXC: tuple[type[Exception], ...] = (httpx.TimeoutException, httpx.TransportError)
_GET_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Mutations: only errors that prove the request never landed, plus an explicit 429.
_WRITE_RETRY_EXC: tuple[type[Exception], ...] = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_WRITE_RETRY_STATUS = frozenset({429})


def get(url: str, *, attempts: int = DEFAULT_ATTEMPTS, **kwargs) -> httpx.Response:
    """GET with full transient-failure retry (timeouts, connection errors, 429, 5xx)."""
    return _send("GET", url, attempts=attempts, retry_exc=_GET_RETRY_EXC, retry_status=_GET_RETRY_STATUS, **kwargs)


def request(method: str, url: str, *, attempts: int = DEFAULT_ATTEMPTS, **kwargs) -> httpx.Response:
    """A mutating request. Retries only connect failures (request never sent) and 429 — never a read
    timeout or 5xx, which could mean the mutation already applied."""
    return _send(method, url, attempts=attempts, retry_exc=_WRITE_RETRY_EXC, retry_status=_WRITE_RETRY_STATUS, **kwargs)


def _send(
    method: str,
    url: str,
    *,
    attempts: int,
    retry_exc: tuple[type[Exception], ...],
    retry_status: frozenset[int],
    base_backoff: float = BASE_BACKOFF_S,
    max_backoff: float = MAX_BACKOFF_S,
    **kwargs,
) -> httpx.Response:
    host = _host(url)
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            response = httpx.request(method, url, **kwargs)
        except retry_exc as exc:
            if attempt >= attempts:
                raise
            _wait(_backoff(attempt, base_backoff, max_backoff), method, host, type(exc).__name__, attempt, attempts)
            continue
        # Host + status + latency only (never the URL — its query can carry an api_key, rule 9). This
        # is the per-call trail that answers "which service was slow tonight" at DEBUG.
        logger.debug("{} {} → {} in {:.2f}s", method, host, response.status_code, time.monotonic() - started)
        if response.status_code in retry_status and attempt < attempts:
            delay = _retry_after(response) or _backoff(attempt, base_backoff, max_backoff)
            _wait(delay, method, host, f"HTTP {response.status_code}", attempt, attempts)
            continue
        return response
    raise AssertionError("unreachable: the loop always returns or raises")  # pragma: no cover


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with ±20% jitter so retries from many users don't thundering-herd a service."""
    raw = min(cap, base * (2 ** (attempt - 1)))
    return raw * random.uniform(0.8, 1.2)


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds a server's Retry-After header asks us to wait (only the delta-seconds form), capped."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return min(MAX_RETRY_AFTER_S, max(0.0, float(value)))
    except ValueError:
        return None  # an HTTP-date form — fall back to computed backoff rather than parse dates


def throttle(last_write: float, min_interval: float, on_wait: Callable[[float], None] | None = None) -> float:
    """Space out writes so they're at least ``min_interval`` seconds apart (plex.tv/Arr rule-6
    politeness). Sleeps if needed and returns the new last-write monotonic time to store back.
    ``on_wait`` is called with the wait seconds before sleeping (plextv uses it to log the stall)."""
    wait = min_interval - (time.monotonic() - last_write)
    if wait > 0:
        if on_wait is not None:
            on_wait(wait)
        time.sleep(wait)
    return time.monotonic()


def _wait(delay: float, method: str, host: str, reason: str, attempt: int, attempts: int) -> None:
    logger.warning("{} {} failed ({}); retry {}/{} in {:.1f}s", method, host, reason, attempt, attempts, delay)
    time.sleep(delay)


def _host(url: str) -> str:
    """Host only — never the full URL, whose query string can carry an api_key (plex-safety rule 9)."""
    try:
        return httpx.URL(url).host
    except Exception:
        return "?"
