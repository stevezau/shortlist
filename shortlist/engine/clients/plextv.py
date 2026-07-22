"""plex.tv client (raw httpx) — the share-filter and Home-user surfaces plexapi doesn't cover.

plex.tv quirks encoded here (all live-verified in Phase 0, 2026-07-12):
- Share filters are attributes of ``<User>`` in ``GET /api/users``; ``PUT /api/users/{id}``
  persists them verbatim.
- Writes are ADAPTIVELY throttled (plex-safety rule 6): fire at a floor pace (default 0 = as fast
  as plex.tv accepts), and on a 429 grow the spacing (to >=1s, then x2, capped 30s), easing back
  toward the floor on each clean write.
- A Home-user switch token gets 401 on the PMS; it must be exchanged for the server-scoped
  ``accessToken`` via ``GET /api/v2/resources`` as the switched user.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx
from loguru import logger

from shortlist.engine.clients import http_retry
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.models import UserType

PLEXTV = "https://plex.tv"
CLIENT_ID = "shortlist"


@dataclass(frozen=True)
class PlexTvUser:
    """One row from plex.tv /api/users (shared or Home user)."""

    id: int
    username: str
    user_type: UserType
    home: bool
    restricted: bool
    protected: bool
    avatar_url: str = ""
    filters: dict[str, str] = field(default_factory=dict)


class PlexTvClient:
    """Thin plex.tv API client for the surfaces plexapi doesn't cover well."""

    # Adaptive write throttle (rule 6): plex.tv is shared infra, but a fixed 1/s is needlessly slow on
    # a healthy server. Instead fire at the FLOOR pace, and only slow down when plex.tv actually pushes
    # back — a 429 grows the spacing (to at least _SLOW_TO, then exponentially, capped at _MAX_PACE),
    # and clean writes decay it back toward the floor. Fast in the common case, self-limiting under load.
    _SLOW_TO = 1.0  # the first 429 jumps the pace to at least this (the old conservative rate)
    _MAX_PACE = 30.0  # never space writes further apart than this, even after repeated 429s

    def __init__(self, token: str, machine_id: str, *, min_write_interval: float = 0.0, timeout: float = 30.0):
        self._token = token
        self._machine_id = machine_id
        self._floor = max(0.0, min_write_interval)  # fastest allowed spacing between writes
        self._pace = self._floor  # current adaptive spacing; grows on 429, decays on success
        self._timeout = timeout
        self._last_write = 0.0

    def _slow_down(self) -> None:
        """A 429 means we're going too fast — widen the spacing for the writes that follow."""
        self._pace = min(self._MAX_PACE, max(self._SLOW_TO, self._pace * 2))

    def _speed_up(self) -> None:
        """A clean write means there's headroom — ease the spacing back toward the floor."""
        if self._pace > self._floor:
            self._pace = max(self._floor, self._pace / 2)

    def _headers(self, token: str | None = None, json: bool = False) -> dict[str, str]:
        h = {"X-Plex-Token": token or self._token, "X-Plex-Client-Identifier": CLIENT_ID}
        if json:
            h["Accept"] = "application/json"
        return h

    def _throttle(self) -> None:
        # Space writes by the CURRENT adaptive pace (0 when healthy). Surfacing the wait makes
        # "why did the sync take W seconds" answerable from the log.
        self._last_write = http_retry.throttle(
            self._last_write,
            self._pace,
            on_wait=lambda w: logger.debug("plex.tv throttle: waiting {:.2f}s before next write", w),
        )

    def list_users(self) -> list[PlexTvUser]:
        """All shared + Home users with their share filters (the owner is not in this list).

        A rate-limited or timed-out READ that raised would abort the privacy sync, and the accounts
        we hadn't reached yet would keep seeing rows that aren't theirs until some later run got
        luckier (rule 6) — so it retries transient failures (429, 5xx, timeouts).
        """
        r = http_retry.get(f"{PLEXTV}/api/users", headers=self._headers(), timeout=self._timeout)
        r.raise_for_status()
        users = []
        for el in ET.fromstring(r.text):
            home = el.get("home") == "1"
            restricted = el.get("restricted") == "1"
            users.append(
                PlexTvUser(
                    id=int(el.get("id", "0")),
                    username=el.get("username") or el.get("title") or "",
                    user_type=UserType.MANAGED if restricted else UserType.SHARED,
                    home=home,
                    restricted=restricted,
                    protected=el.get("protected") == "1",
                    avatar_url=el.get("thumb") or "",
                    filters={
                        f: el.get(f) or ""
                        for f in ("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos")
                    },
                )
            )
        return users

    def get_user(self, plex_account_id: int) -> PlexTvUser:
        for user in self.list_users():
            if user.id == plex_account_id:
                return user
        raise LookupError(f"plex.tv account {plex_account_id} not found in users list")

    def update_user_filters(self, plex_account_id: int, fields: dict[str, str]) -> None:
        """PUT only the given filter fields (never rebuilds), adaptively throttled with 429 backoff.

        Fires at the current pace (0 when healthy). A 429 widens the pace via ``_slow_down`` and
        retries — the next ``_throttle`` at the loop top enforces the new, larger spacing, so repeated
        429s back off exponentially. A clean write eases the pace back toward the floor.
        """
        url = f"{PLEXTV}/api/users/{plex_account_id}"
        net_backoff = 2.0
        for attempt in range(6):
            self._throttle()
            try:
                r = httpx.put(url, params=fields, headers=self._headers(), timeout=self._timeout)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
                # The request provably never reached plex.tv, so re-sending the SAME pre-merged filter
                # is safe (it's a full-value PUT, not a delta — rule 3's merge already happened). A
                # read timeout is deliberately NOT retried here: the write may have applied.
                logger.warning("plex.tv unreachable on filter write (attempt {}): {}", attempt + 1, type(e).__name__)
                time.sleep(net_backoff)
                net_backoff = min(net_backoff * 2, 30.0)
                continue
            if r.status_code in (200, 201):
                logger.debug("PUT {} {} -> {}", url, sorted(fields), r.status_code)
                self._speed_up()
                return
            if r.status_code == 429:
                self._slow_down()
                logger.warning(
                    "plex.tv 429 on filter write (attempt {}); slowing to {:.1f}s/write", attempt + 1, self._pace
                )
                continue  # the loop-top _throttle now waits the new, larger pace before retrying
            # Carry plex.tv's own words. "HTTP 400" alone leaves the operator guessing which of
            # their accounts plex.tv won't accept a filter for, and why (issue #1). The body is
            # short XML/JSON; truncate it and redact in case it ever echoes the token back.
            detail = redact(" ".join((r.text or "").split()))[:300]
            raise RuntimeError(
                f"plex.tv rejected the share-filter update for account {plex_account_id}: "
                f"HTTP {r.status_code}{f' — {detail}' if detail else ''}"
            )
        raise RuntimeError(f"plex.tv still throttling filter update for {plex_account_id} after retries")

    def home_users(self) -> list[dict]:
        r = http_retry.get(f"{PLEXTV}/api/v2/home/users", headers=self._headers(json=True), timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("users", data if isinstance(data, list) else [])

    def canary_server_token(self, plex_account_id: int) -> str:
        """Mint a server-scoped access token for a (non-PIN) Home user — lets a test view the server
        as that user to confirm each account's Home shows only its own rows.

        Switch to the Home user, then exchange the plex.tv token for this server's
        ``accessToken`` via the resources listing (the switch token alone 401s on the PMS).
        """
        me = next((u for u in self.home_users() if int(u.get("id", 0)) == plex_account_id), None)
        if me is None:
            raise LookupError(f"account {plex_account_id} is not a Home user — cannot borrow their server token")
        if me.get("protected"):
            raise PermissionError(f"Home user {me.get('title')} is PIN-protected — cannot switch automatically")
        r = http_retry.request(
            "POST",
            f"{PLEXTV}/api/v2/home/users/{me['uuid']}/switch",
            headers=self._headers(json=True),
            timeout=self._timeout,
        )
        r.raise_for_status()
        switch_token = r.json()["authToken"]
        r = http_retry.get(
            f"{PLEXTV}/api/v2/resources?includeHttps=1",
            headers=self._headers(token=switch_token, json=True),
            timeout=self._timeout,
        )
        r.raise_for_status()
        resource = next((x for x in r.json() if x.get("clientIdentifier") == self._machine_id), None)
        if resource is None or not resource.get("accessToken"):
            raise LookupError(f"no server access token for canary {plex_account_id} on {self._machine_id}")
        return resource["accessToken"]
