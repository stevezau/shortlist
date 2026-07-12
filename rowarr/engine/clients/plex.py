"""PMS (plexapi) and plex.tv (raw httpx) clients.

plex.tv quirks encoded here (all live-verified in Phase 0, 2026-07-12):
- Share filters are attributes of ``<User>`` in ``GET /api/users``; ``PUT /api/users/{id}``
  persists them verbatim.
- Writes are throttled to >=1/s with exponential backoff on 429 (plex-safety rule 6).
- A Home-user switch token gets 401 on the PMS; it must be exchanged for the server-scoped
  ``accessToken`` via ``GET /api/v2/resources`` as the switched user.
- Plex title-cases new labels (``rowarr_x`` -> ``Rowarr_x``); callers must use the label
  *as stored*, so collection helpers always read labels back after writing.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx
from loguru import logger
from plexapi.collection import Collection
from plexapi.library import LibrarySection
from plexapi.server import PlexServer

from rowarr.engine.models import UserType

PLEXTV = "https://plex.tv"
CLIENT_ID = "rowarr"

# Label restrictions only apply on Home/Recommended/Related from this PMS build (PM-5174).
MIN_PMS_VERSION = (1, 43, 2, 10687)


def parse_pms_version(version: str) -> tuple[int, ...]:
    """'1.43.3.10793-cd55560bb' -> (1, 43, 3, 10793)."""
    numbers = version.split("-")[0].split(".")
    return tuple(int(n) for n in numbers if n.isdigit())


@dataclass(frozen=True)
class PlexTvUser:
    """One row from plex.tv /api/users (shared or Home user)."""

    id: int
    username: str
    user_type: UserType
    home: bool
    restricted: bool
    protected: bool
    uuid: str = ""
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
        """All shared + Home users with their share filters (the owner is not in this list)."""
        r = httpx.get(f"{PLEXTV}/api/users", headers=self._headers(), timeout=self._timeout)
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


class PlexClient:
    """PMS operations, restricted to collections Rowarr owns (label-gated)."""

    def __init__(self, base_url: str, token: str, *, timeout: int = 30):
        self._server = PlexServer(base_url, token, timeout=timeout)

    @property
    def machine_id(self) -> str:
        return self._server.machineIdentifier

    @property
    def version(self) -> str:
        return self._server.version

    @property
    def owner_username(self) -> str:
        return self._server.myPlexAccount().username

    def sections(self, types: tuple[str, ...] = ("movie", "show")) -> list[LibrarySection]:
        return [s for s in self._server.library.sections() if s.type in types]

    def build_library_index(self, section: LibrarySection) -> dict[int, int]:
        """Map tmdb_id -> ratingKey for every item in a section (once per run, cached upstream)."""
        index: dict[int, int] = {}
        for item in section.all():
            for guid in getattr(item, "guids", []):
                if guid.id.startswith("tmdb://"):
                    index[int(guid.id.removeprefix("tmdb://"))] = item.ratingKey
                    break
        logger.debug(
            "library index for '{}': {} of {} items have TMDB ids", section.title, len(index), section.totalSize
        )
        return index

    def owned_collections(self, label_prefix: str = "rowarr") -> dict[str, tuple[str, int]]:
        """Map slug -> (label as stored, collection ratingKey) for every rowarr-owned collection.

        The PMS is the source of truth for label casing (Plex title-cases new labels) and for
        the collection ids the T2 privacy check compares hubs against.
        """
        prefix = f"{label_prefix}_".lower()
        owned: dict[str, tuple[str, int]] = {}
        for section in self.sections():
            for collection in section.collections():
                for label in collection.labels:
                    if label.tag.lower().startswith(prefix):
                        owned[label.tag[len(prefix) :].lower()] = (label.tag, collection.ratingKey)
        return owned

    def find_owned_collection(self, section: LibrarySection, label_prefix: str, slug: str) -> Collection | None:
        """Find the collection Rowarr owns for this user, matching by label (never title).

        Labels are compared case-insensitively because Plex title-cases them on creation.
        """
        wanted = f"{label_prefix}_{slug}".lower()
        for collection in section.collections():
            if any(label.tag.lower() == wanted for label in collection.labels):
                return collection
        return None

    def create_collection(self, section: LibrarySection, title: str, items: list) -> Collection:
        return self._server.createCollection(title=title, section=section, items=items)

    def stored_label(self, collection: Collection, label: str) -> str:
        """Ensure `label` is on the collection and return it AS STORED (Plex title-cases it)."""
        existing = next((tag.tag for tag in collection.labels if tag.tag.lower() == label.lower()), None)
        if existing:
            return existing
        collection.addLabel(label)
        collection.reload()
        stored = next((tag.tag for tag in collection.labels if tag.tag.lower() == label.lower()), None)
        if stored is None:
            raise RuntimeError(f"label {label!r} did not persist on collection {collection.title!r}")
        if stored != label:
            logger.debug("Plex stored label {!r} as {!r}", label, stored)
        return stored

    def promote(self, collection: Collection, *, shared: bool = True) -> None:
        """Hide from library browsing but promote onto Home (owner + shared users)."""
        collection.modeUpdate(mode="hide")
        collection.visibility().updateVisibility(recommended=True, home=True, shared=shared)

    def set_items(self, collection: Collection, items: list) -> None:
        """Replace collection items, preserving the given order via custom sort."""
        current = {i.ratingKey for i in collection.items()}
        wanted_keys = [i.ratingKey for i in items]
        to_remove = [i for i in collection.items() if i.ratingKey not in set(wanted_keys)]
        to_add = [i for i in items if i.ratingKey not in current]
        if to_add:
            collection.addItems(to_add)
        if to_remove:
            collection.removeItems(to_remove)
        collection.sortUpdate(sort="custom")
        collection.reload()
        ordered = {i.ratingKey: i for i in collection.items()}
        previous = None
        for key in wanted_keys:
            item = ordered.get(key)
            if item is None:
                continue
            collection.moveItem(item, after=previous)
            previous = item

    def delete_owned_collection(self, collection: Collection, label_prefix: str) -> None:
        """Delete a collection only if it carries a rowarr label (Kometa coexistence)."""
        if not any(label.tag.lower().startswith(f"{label_prefix}_") for label in collection.labels):
            raise PermissionError(f"refusing to delete {collection.title!r}: no {label_prefix}_* label — not ours")
        collection.visibility().updateVisibility(recommended=False, home=False, shared=False)
        collection.delete()

    def fetch_items(self, rating_keys: list[int]) -> list:
        return self._server.fetchItems(rating_keys)

    def user_hubs(self, canary_token: str, path: str = "/hubs") -> list[dict]:
        """Fetch hubs AS another user (T2). Uses the canary's server token, not the owner's."""
        r = httpx.get(
            self._server.url(path, includeToken=False),
            headers={"X-Plex-Token": canary_token, "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("MediaContainer", {}).get("Hub", []) or []

    def history_for_account(self, account_id: int, *, max_results: int = 200) -> list:
        """Plex-native watch history for one account (fallback when Tautulli is absent)."""
        return self._server.history(maxresults=max_results, accountID=account_id)
