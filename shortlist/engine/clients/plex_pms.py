"""PMS client (plexapi) — collection/library operations, restricted to what Shortlist owns.

Plex quirks encoded here (all live-verified in Phase 0, 2026-07-12):
- Plex fixes a collection's subtype from the items it is CREATED with and never revises it, so a
  mistyped collection must be rebuilt, never edited (see ``matches_section``).
- Plex title-cases new labels (``shortlist_x`` -> ``Shortlist_x``); callers must use the label
  *as stored*, so collection helpers always read labels back after writing.
"""

from __future__ import annotations

import time

import requests
from loguru import logger
from plexapi.collection import Collection
from plexapi.library import LibrarySection
from plexapi.server import PlexServer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from shortlist.engine.clients import http_retry
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


def _retrying_session() -> requests.Session:
    """A requests session that retries transient PMS failures (read/connect timeouts, 429, 5xx).

    plexapi talks to the PMS over ``requests``; without this a single slow response fails the whole
    run (SFLIX run 3 died on one 30s read timeout). Only idempotent methods are retried, so a
    collection create/label (POST/PUT) is never repeated — just the reads that dominate a run.
    """
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1.5,  # waits ~0s, 1.5s, 3s between tries
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class PlexClient:
    """PMS operations, restricted to collections Shortlist owns (label-gated)."""

    def __init__(self, base_url: str, token: str, *, timeout: int = 30):
        self._server = PlexServer(base_url, token, session=_retrying_session(), timeout=timeout)
        # Per-run read caches. A PlexClient is built fresh for each run (the server adapter
        # constructs one per run), so these live exactly one run — no cross-run staleness. Library
        # sections don't change mid-run; a section's collection LIST changes only when WE create or
        # delete one, so it is busted on exactly those two operations. Item edits, label adds and
        # promotes mutate the cached Collection objects in place, so they need no busting.
        self._sections_cache: list[LibrarySection] | None = None
        self._collections_cache: dict[str, list[Collection]] = {}

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
        if self._sections_cache is None:
            self._sections_cache = self._server.library.sections()
        return [s for s in self._sections_cache if s.type in types]

    def _section_collections(self, section: LibrarySection) -> list[Collection]:
        """This section's collections, fetched once per run and reused (busted on create/delete).

        The full collection list of a section is otherwise re-pulled for every owned-collections
        scan, every delivery, and every promote — the biggest single source of repeated PMS reads.
        """
        if section.key not in self._collections_cache:
            self._collections_cache[section.key] = section.collections()
        return self._collections_cache[section.key]

    def _invalidate_collections(self) -> None:
        """Drop the collection-list cache after a create/delete, so the next read is authoritative.
        Wholesale (not per-section) so the privacy sync and sweep can never act on a stale list."""
        self._collections_cache.clear()

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

    def build_library_index(self, section: LibrarySection) -> tuple[dict[int, int], dict[int, int]]:
        """Scan a section once, returning ``(index, episodes)``.

        * ``index`` — ``tmdb_id -> ratingKey`` for every TMDB-identified item.
        * ``episodes`` — ``tmdb_id -> total episode count`` (``leafCount``); populated only for shows
          (movies have no leafCount). The watched-filter uses it to tell a finished show from one
          you've only sampled or that just got a new season (which grows the count).

        Returned rather than mutating a passed-in dict so the whole result is a single cacheable
        value (see the cross-run library-index cache in the pipeline).
        """
        index: dict[int, int] = {}
        episodes: dict[int, int] = {}
        for item in section.all():
            tmdb_id = _tmdb_guid(item)
            if tmdb_id is not None:
                index[tmdb_id] = item.ratingKey
                leaf = getattr(item, "leafCount", None)
                if leaf:
                    episodes[tmdb_id] = int(leaf)
        logger.debug(
            "library index for '{}': {} of {} items have TMDB ids", section.title, len(index), section.totalSize
        )
        return index, episodes

    def section_signature(self, section: LibrarySection) -> str | None:
        """A cheap fingerprint of a section's contents for the cross-run index cache — its item count
        plus last-updated stamp, both already loaded on the section (no extra PMS call). Returns None
        when neither is available, which tells the caller to scan rather than trust a cache."""
        total = getattr(section, "totalSize", None)
        updated = getattr(section, "updatedAt", None)
        if total is None and updated is None:
            return None
        stamp = int(updated.timestamp()) if hasattr(updated, "timestamp") else updated
        return f"{total}:{stamp}"

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
        the collection ids behind a user's rows. A user has one collection per library they get
        picks in, so ids accumulate — collapsing them to a single id once
        hid a real leak: only the last collection was seen while two other
        rows were visible to everyone.
        """
        prefix = f"{label_prefix}_".lower()
        owned: dict[str, OwnedRow] = {}
        for section in self.sections():
            for collection in self._section_collections(section):
                for label in collection.labels:
                    if label.tag.lower().startswith(prefix):
                        slug = label.tag[len(prefix) :].lower()
                        row = owned.setdefault(slug, OwnedRow(label=label.tag))
                        row.rating_keys.append(collection.ratingKey)
        return owned

    def list_owned_collections(self, label_prefix: str = "shortlist") -> list[dict]:
        """Every shortlist-owned collection currently on the server — one entry each (NOT collapsed by
        slug), for a cleanup audit. Read-only and label-based, so it lists rows even for users or rows
        no longer in the database (exactly the drift a cleanup needs to catch). Returns one
        ``{library, title, label, rating_key}`` per collection."""
        prefix = f"{label_prefix}_".lower()
        out: list[dict] = []
        for section in self.sections():
            for collection in self._section_collections(section):
                label = next((lbl.tag for lbl in collection.labels if lbl.tag.lower().startswith(prefix)), None)
                if label is None:
                    continue
                out.append(
                    {
                        "library": section.title,
                        "title": collection.title,  # raw (carries the invisible marker); caller strips it
                        "label": label,
                        "rating_key": collection.ratingKey,
                    }
                )
        return out

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
        return [c for c in self._section_collections(section) if any(label.tag.lower() == wl for label in c.labels)]

    def create_collection(self, section: LibrarySection, title: str, items: list) -> Collection:
        collection = self._server.createCollection(title=title, section=section, items=items)
        self._invalidate_collections()  # a new collection changes the section's list
        return collection

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

    def promote(
        self,
        collection: Collection,
        *,
        shared: bool = True,
        home: bool = True,
        recommended: bool = True,
        pin_top: bool = False,
    ) -> None:
        """Hide from library browsing but promote onto the chosen surfaces (Home / Library Recommended).

        ``modeUpdate(hide)`` is unconditional — it hides the collection from normal library BROWSE and
        is the leak-safe half of promotion, independent of where the row is shown. ``home``/``shared``/
        ``recommended`` pick the surfaces (a per-row placement). ``pin_top`` moves the managed hub to
        the top of the library's Recommended shelf (server-wide order, not per viewing-user).
        """
        start = time.monotonic()
        collection.modeUpdate(mode="hide")
        hub = collection.visibility()
        hub.updateVisibility(recommended=recommended, home=home, shared=shared)
        if pin_top:
            # after=None -> first position in this library's Managed Recommendations.
            hub.reload().move(after=None)
        logger.info(
            "{}: promoted (home={} library={} pin={}) in {:.1f}s",
            collection.title,
            home,
            recommended,
            pin_top,
            time.monotonic() - start,
        )

    def order_owned_hubs(
        self,
        section: LibrarySection,
        *,
        label_prefix: str,
        anchor_title: str = "",
        before: bool = False,
        dry_run: bool = False,
        only_titles: set[str] | None = None,
        to_top: bool = False,
    ) -> dict:
        """Place this section's Shortlist rows in Plex's Managed Recommendations shelf: at the very TOP
        (``to_top``) or right after/before the ``anchor_title`` collection, so a co-managing tool
        (Kometa) can't bury them.

        Only OUR hubs (``label_prefix``-labelled) are moved; the anchor is read-only. ``only_titles``
        restricts the move to that subset of our rows (used when different rows anchor to different
        collections) — ``None`` moves them all. Idempotent — if our rows already sit contiguously in
        the target slot, nothing is written (no nightly churn). Returns an audit dict:
        ``{anchor, moved: [titles], skipped: bool, reason?}``.
        """
        prefix = f"{label_prefix}_".lower()
        owned_all = {
            c.title
            for c in self._section_collections(section)
            if any(label.tag.lower().startswith(prefix) for label in c.labels)
        }
        # The subset to MOVE (restricted by only_titles); the anchor is never any of our OWN rows —
        # excluded via owned_all, not the subset, so a row can't be anchored to a sibling Shortlist row.
        owned_titles = owned_all & only_titles if only_titles is not None else set(owned_all)
        if not owned_titles:
            return {"anchor": anchor_title, "moved": [], "skipped": True, "reason": "no rows in this library"}

        order = list(section.managedHubs())  # the live shelf order
        ours = [h for h in order if (getattr(h, "title", "") or "") in owned_titles]
        if not ours:
            return {"anchor": anchor_title, "moved": [], "skipped": True, "reason": "rows not promoted yet"}

        if to_top:
            target = None  # move(after=None) -> the very top of the shelf
        else:
            anchor = next(
                (
                    h
                    for h in order
                    if (getattr(h, "title", "") or "") == anchor_title
                    and (getattr(h, "title", "") or "") not in owned_all
                ),
                None,
            )
            if anchor is None:
                logger.warning(
                    "hub order: anchor {!r} not found in {} — leaving the shelf order unchanged",
                    anchor_title,
                    section.title,
                )
                return {"anchor": anchor_title, "moved": [], "skipped": True, "reason": "anchor not found"}
            # 'after anchor' -> the anchor; 'before anchor' -> the hub just before it that isn't one of
            # ours (None -> the very top of the shelf).
            if before:
                anchor_idx = order.index(anchor)
                target = next(
                    (h for h in reversed(order[:anchor_idx]) if (getattr(h, "title", "") or "") not in owned_all),
                    None,
                )
            else:
                target = anchor

        idents = [h.identifier for h in order]
        our_idents = [h.identifier for h in ours]
        start = idents.index(target.identifier) + 1 if target is not None else 0
        if idents[start : start + len(our_idents)] == our_idents:
            return {"anchor": anchor_title, "moved": [], "skipped": True, "reason": "already in place"}

        where = "to the top" if to_top else f"{'before' if before else 'after'} {anchor_title!r}"
        moved_titles = [h.title for h in ours]
        if dry_run:
            logger.info("[dry-run] hub order: would move {} row(s) {} in {}", len(ours), where, section.title)
            return {
                "anchor": "top" if to_top else anchor_title,
                "moved": moved_titles,
                "skipped": False,
                "dry_run": True,
            }

        prev = target
        for hub in ours:
            hub.reload().move(after=prev)  # after=None -> top of the shelf
            prev = hub
        logger.info("hub order: moved {} row(s) {} in {}", len(ours), where, section.title)
        return {"anchor": "top" if to_top else anchor_title, "moved": moved_titles, "skipped": False}

    def set_items(self, collection: Collection, items: list) -> None:
        """Replace a collection's items and put them in the given (ranked) order via custom sort.

        Ordering is the expensive part: Plex's ``moveItem`` is one PMS round-trip PER item, so the old
        "move every item every time" cost N calls per row per library on every run. Here we move ONLY
        the items that are actually out of place (simulating the live order as we go), so a steady
        re-run — where most titles keep their rank — costs a handful of calls instead of ~20.
        """
        start = time.monotonic()
        existing = collection.items()  # one fetch; reused for the diff below
        current_keys = {i.ratingKey for i in existing}
        wanted_keys = [i.ratingKey for i in items]
        wanted_set = set(wanted_keys)
        to_remove = [i for i in existing if i.ratingKey not in wanted_set]
        to_add = [i for i in items if i.ratingKey not in current_keys]
        if to_add:
            collection.addItems(to_add)
        if to_remove:
            collection.removeItems(to_remove)
        collection.sortUpdate(sort="custom")
        collection.reload()
        now = collection.items()  # one fetch of the post-add/remove membership
        by_key = {i.ratingKey: i for i in now}
        order = [i.ratingKey for i in now]  # our model of the live order, kept in sync as we move
        target = [k for k in wanted_keys if k in by_key]
        moves = 0
        previous: int | None = None  # the ratingKey the current target item must sit right after
        for key in target:
            want_idx = 0 if previous is None else order.index(previous) + 1
            if order.index(key) != want_idx:
                collection.moveItem(by_key[key], after=(by_key[previous] if previous is not None else None))
                order.remove(key)
                order.insert(0 if previous is None else order.index(previous) + 1, key)
                moves += 1
            previous = key
        logger.info(
            "{}: items +{} -{}, reordered {}/{} in {:.1f}s",
            collection.title,
            len(to_add),
            len(to_remove),
            moves,
            len(target),
            time.monotonic() - start,
        )

    def delete_owned_collection(self, collection: Collection, label_prefix: str) -> None:
        """Delete a collection only if it carries a shortlist label (Kometa coexistence)."""
        if not any(label.tag.lower().startswith(f"{label_prefix}_") for label in collection.labels):
            raise PermissionError(f"refusing to delete {collection.title!r}: no {label_prefix}_* label — not ours")
        collection.visibility().updateVisibility(recommended=False, home=False, shared=False)
        collection.delete()
        self._invalidate_collections()  # a removed collection changes the section's list

    def fetch_items(self, rating_keys: list[int]) -> list:
        return self._server.fetchItems(rating_keys)

    def user_hubs(self, canary_token: str, path: str = "/hubs") -> list[dict]:
        """Fetch hubs AS another user (for visibility checks). Uses that user's server token, not the owner's."""
        r = http_retry.get(
            self._server.url(path, includeToken=False),
            headers={"X-Plex-Token": canary_token, "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json().get("MediaContainer", {}).get("Hub", []) or []

    def history_for_account(self, account_id: int, *, max_results: int = 200) -> list:
        """Plex-native watch history for one account (fallback when Tautulli is absent)."""
        return self._server.history(maxresults=max_results, accountID=account_id)
