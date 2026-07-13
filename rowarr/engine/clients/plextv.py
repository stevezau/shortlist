"""plex.tv client (raw httpx) — the share-filter and Home-user surfaces plexapi doesn't cover.

plex.tv quirks encoded here (all live-verified in Phase 0, 2026-07-12):
- Share filters are attributes of ``<User>`` in ``GET /api/users``; ``PUT /api/users/{id}``
  persists them verbatim.
- Writes are throttled to >=1/s with exponential backoff on 429 (plex-safety rule 6).
- A Home-user switch token gets 401 on the PMS; it must be exchanged for the server-scoped
  ``accessToken`` via ``GET /api/v2/resources`` as the switched user.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx
from loguru import logger

from rowarr.engine.models import UserType

PLEXTV = "https://plex.tv"
CLIENT_ID = "rowarr"


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

    def __init__(self, token: str, machine_id: str, *, min_write_interval: float = 1.0, timeout: float = 30.0):
        self._token = token
        self._machine_id = machine_id
        self._min_write_interval = min_write_interval
        self._timeout = timeout
        self._last_write = 0.0

    def _headers(self, token: str | None = None, json: bool = False) -> dict[str, str]:
        h = {"X-Plex-Token": token or self._token, "X-Plex-Client-Identifier": CLIENT_ID}
        if json:
            h["Accept"] = "application/json"
        return h

    def _throttle(self) -> None:
        wait = self._min_write_interval - (time.monotonic() - self._last_write)
        if wait > 0:
            time.sleep(wait)
        self._last_write = time.monotonic()

    def list_users(self) -> list[PlexTvUser]:
        """All shared + Home users with their share filters (the owner is not in this list).

        Retried with backoff on 429 like the write path: a rate-limited READ that raises would
        abort the privacy sync, and the accounts we hadn't reached yet would keep seeing rows
        that aren't theirs until some later run got luckier (rule 6).
        """
        url = f"{PLEXTV}/api/users"
        backoff = 5.0
        for attempt in range(4):
            r = httpx.get(url, headers=self._headers(), timeout=self._timeout)
            if r.status_code != 429:
                break
            logger.warning("plex.tv 429 on user list (attempt {}); backing off {}s", attempt + 1, backoff)
            time.sleep(backoff)
            backoff *= 2
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
        """PUT only the given filter fields, throttled, with 429 backoff (never rebuilds)."""
        url = f"{PLEXTV}/api/users/{plex_account_id}"
        backoff = 5.0
        for attempt in range(4):
            self._throttle()
            r = httpx.put(url, params=fields, headers=self._headers(), timeout=self._timeout)
            if r.status_code in (200, 201):
                logger.debug("PUT {} {} -> {}", url, sorted(fields), r.status_code)
                return
            if r.status_code == 429:
                logger.warning("plex.tv 429 on filter write (attempt {}); backing off {}s", attempt + 1, backoff)
                time.sleep(backoff)
                backoff *= 2
                continue
            raise RuntimeError(f"plex.tv rejected filter update for {plex_account_id}: HTTP {r.status_code}")
        raise RuntimeError(f"plex.tv still throttling filter update for {plex_account_id} after retries")

    def home_users(self) -> list[dict]:
        r = httpx.get(f"{PLEXTV}/api/v2/home/users", headers=self._headers(json=True), timeout=self._timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("users", data if isinstance(data, list) else [])

    def canary_server_token(self, plex_account_id: int) -> str:
        """Mint a server-scoped access token for a (non-PIN) Home user — the T2 mechanism.

        Switch to the Home user, then exchange the plex.tv token for this server's
        ``accessToken`` via the resources listing (the switch token alone 401s on the PMS).
        """
        me = next((u for u in self.home_users() if int(u.get("id", 0)) == plex_account_id), None)
        if me is None:
            raise LookupError(f"account {plex_account_id} is not a Home user — T2 needs a Home canary")
        if me.get("protected"):
            raise PermissionError(f"Home user {me.get('title')} is PIN-protected — cannot switch automatically")
        r = httpx.post(
            f"{PLEXTV}/api/v2/home/users/{me['uuid']}/switch",
            headers=self._headers(json=True),
            timeout=self._timeout,
        )
        r.raise_for_status()
        switch_token = r.json()["authToken"]
        r = httpx.get(
            f"{PLEXTV}/api/v2/resources?includeHttps=1",
            headers=self._headers(token=switch_token, json=True),
            timeout=self._timeout,
        )
        r.raise_for_status()
        resource = next((x for x in r.json() if x.get("clientIdentifier") == self._machine_id), None)
        if resource is None or not resource.get("accessToken"):
            raise LookupError(f"no server access token for canary {plex_account_id} on {self._machine_id}")
        return resource["accessToken"]
