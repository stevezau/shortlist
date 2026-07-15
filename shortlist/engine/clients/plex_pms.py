"""PMS client (plexapi) — collection/library operations, restricted to what Shortlist owns.

Plex quirks encoded here (all live-verified in Phase 0, 2026-07-12):
- Plex fixes a collection's subtype from the items it is CREATED with and never revises it, so a
  mistyped collection must be rebuilt, never edited (see ``matches_section``).
- Plex title-cases new labels (``shortlist_x`` -> ``Shortlist_x``); callers must use the label
  *as stored*, so collection helpers always read labels back after writing.
"""

from __future__ import annotations

import httpx
from loguru import logger
from plexapi.collection import Collection
from plexapi.library import LibrarySection
from plexapi.server import PlexServer

from shortlist.engine.models import MediaType, OwnedRow

# Label restrictions only apply on Home/Recommended/Related from this PMS build (PM-5174).
MIN_PMS_VERSION = (1, 43, 2, 10687)


def parse_pms_version(version: str) -> tuple[int, ...]:
    """'1.43.3.10793-cd55560bb' -> (1, 43, 3, 10793)."""
    numbers = version.split("-")[0].split(".")
    return tuple(int(n) for n in numbers if n.isdigit())


def _tmdb_guid(item) -> int | None:
    """The item's TMDB id, or None. The one place the ``tmdb://`` guid grammar lives."""
    for guid in getattr(item, "guids", []):
        if guid.id.startswith("tmdb://"):
            return int(guid.id.removeprefix("tmdb://"))
    return None


class PlexClient:
    """PMS operations, restricted to collections Shortlist owns (label-gated)."""

    def __init__(self, base_url: str, token: str, *, timeout: int = 30):
        self._server = PlexServer(base_url, token, timeout=timeout)

    @property
    def machine_id(self) -> str:
        return self._server.machineIdentifier

    @property
    def version(self) -> str:
        return self._server.version

    @property
    def server_name(self) -> str:
        """The server's friendly name — what setup/settings show the owner."""
        return self._server.friendlyName

    def sections(self, types: tuple[str, ...] = ("movie", "show")) -> list[LibrarySection]:
        return [s for s in self._server.library.sections() if s.type in types]

    def sections_by_type(self) -> dict[MediaType, LibrarySection]:
        """One representative library per media type — used for cold-start discovery and the
        AI-from-library catalog, NOT for choosing where rows are delivered.

        Row delivery targets ``library_keys`` (all libraries by default; see the delivery module),
        so a server with several libraries of a type builds rows in every one. This helper is only a
        stable single pick per type for the two callers that need just one: the lowest section key
        wins — deliberately NOT the order the PMS lists them in, so a reordering can't shift which
        library those callers read.
        """
        by_type: dict[MediaType, LibrarySection] = {}
        for section in sorted(self.sections(), key=lambda s: int(s.key)):
            kind = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
            by_type.setdefault(kind, section)
        return by_type

    def build_library_index(
        self, section: LibrarySection, episode_counts: dict[int, int] | None = None
    ) -> dict[int, int]:
        """Map tmdb_id -> ratingKey for every item in a section (once per run, cached upstream).

        When ``episode_counts`` is given, also record each show's total episode count (``leafCount``)
        keyed by tmdb_id — the watched-filter uses it to tell a finished show from one you've only
        sampled or that just got a new season (which grows the count).
        """
        index: dict[int, int] = {}
        for item in section.all():
            tmdb_id = _tmdb_guid(item)
            if tmdb_id is not None:
                index[tmdb_id] = item.ratingKey
                if episode_counts is not None:
                    leaf = getattr(item, "leafCount", None)
                    if leaf:
                        episode_counts[tmdb_id] = int(leaf)
        logger.debug(
            "library index for '{}': {} of {} items have TMDB ids", section.title, len(index), section.totalSize
        )
        return index

    def build_library_catalog(self, section: LibrarySection) -> list[dict]:
        """Every TMDB-identified item with the metadata the AI-from-library source reasons over.

        Same one scan shape as build_library_index, but keeps title/year/genres so the LLM can pick
        owned titles that fit a person's taste. Built at most once per run (only when that source is on).
        """
        catalog: list[dict] = []
        for item in section.all():
            tmdb_id = _tmdb_guid(item)
            if tmdb_id is None:
                continue
            catalog.append(
                {
                    "tmdb_id": tmdb_id,
                    "rating_key": item.ratingKey,
                    "title": item.title,
                    "year": getattr(item, "year", None),
                    "genres": [g.tag for g in (getattr(item, "genres", None) or [])],
                }
            )
        logger.debug("library catalog for '{}': {} titled items", section.title, len(catalog))
        return catalog

    def top_rated(self, section: LibrarySection, limit: int) -> list[tuple[int, object]]:
        """Highest audience-rated titles that carry a TMDB id — the cold-start 'popular' source.

        Returns ``(tmdb_id, item)`` pairs, up to ``limit``. Over-fetches (2x) because titles with
        no TMDB guid are skipped, so a library with sparse ids still fills the request. Owns the
        ``tmdb://`` guid grammar so cold start never has to parse a guid itself.
        """
        out: list[tuple[int, object]] = []
        for item in section.search(sort="audienceRating:desc", limit=limit * 2):
            tmdb_id = _tmdb_guid(item)
            if tmdb_id is None:
                continue
            out.append((tmdb_id, item))
            if len(out) == limit:
                break
        return out

    def owned_collections(self, label_prefix: str = "shortlist") -> dict[str, OwnedRow]:
        """Map slug -> OwnedRow for every shortlist-owned collection, across every library.

        The PMS is the source of truth for label casing (Plex title-cases new labels) and for
        the collection ids the T2 privacy check compares hubs against. A user has one collection
        per library they get picks in, so ids accumulate — collapsing them to a single id once
        hid a real leak: T2 only compared the last collection it saw and passed while two other
        rows were visible to everyone.
        """
        prefix = f"{label_prefix}_".lower()
        owned: dict[str, OwnedRow] = {}
        for section in self.sections():
            for collection in section.collections():
                for label in collection.labels:
                    if label.tag.lower().startswith(prefix):
                        slug = label.tag[len(prefix) :].lower()
                        row = owned.setdefault(slug, OwnedRow(label=label.tag))
                        row.rating_keys.append(collection.ratingKey)
        return owned

    def matches_section(self, collection: Collection, section: LibrarySection) -> bool:
        """Whether this collection's type matches the library it lives in.

        Plex fixes a collection's subtype from the items it is CREATED with and never revises it,
        so a collection built from shows keeps `subtype="show"` even after its contents are
        swapped for movies. A mismatched collection is matched by neither `filterMovies` nor
        `filterTelevision`, which makes it impossible to hide from anyone — so it must be
        deleted and recreated, never edited in place (SFLIX, 2026-07-12).

        The subtype is conclusive, so it answers on its own: falling through to the items would
        cost a PMS round-trip per user per library, every night, for rows that are already fine.
        The item check is only the fallback for a collection with no subtype at all — and an
        EMPTY one is deliberately treated as matching, because a collection with nothing in it
        shows nobody anything.
        """
        subtype = getattr(collection, "subtype", None)
        if subtype:
            return subtype == section.type
        return all(item.type == section.type for item in collection.items())

    def find_owned_collections(self, section: LibrarySection, wanted_label: str) -> list[Collection]:
        """Every collection in this section carrying `wanted_label` (case-insensitive).

        A user can have several rows, all sharing their label and told apart by title — so delivery
        picks the one with the matching title, and promotion promotes them all.
        """
        wl = wanted_label.lower()
        return [c for c in section.collections() if any(label.tag.lower() == wl for label in c.labels)]

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
        """Delete a collection only if it carries a shortlist label (Kometa coexistence)."""
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
