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
import time

import httpx
from loguru import logger

DEFAULT_ATTEMPTS = 3
BASE_BACKOFF_S = 1.0
MAX_BACKOFF_S = 20.0
MAX_RETRY_AFTER_S = 60.0  # cap an honoured Retry-After here — a longer server hint is clamped, not obeyed,
#                            so one slow endpoint can't stall the whole run on its own say-so.

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
        try:
            response = httpx.request(method, url, **kwargs)
        except retry_exc as exc:
            if attempt >= attempts:
                raise
            _wait(_backoff(attempt, base_backoff, max_backoff), method, host, type(exc).__name__, attempt, attempts)
            continue
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


def _wait(delay: float, method: str, host: str, reason: str, attempt: int, attempts: int) -> None:
    logger.warning("{} {} failed ({}); retry {}/{} in {:.1f}s", method, host, reason, attempt, attempts, delay)
    time.sleep(delay)


def _host(url: str) -> str:
    """Host only — never the full URL, whose query string can carry an api_key (plex-safety rule 9)."""
    try:
        return httpx.URL(url).host
    except Exception:
        return "?"
