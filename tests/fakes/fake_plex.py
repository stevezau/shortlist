"""In-memory fakes of a Plex Media Server and plex.tv, served over real HTTP.

``make_fake_plex(state)`` builds a FastAPI app speaking just enough of the PMS wire protocol
(XML MediaContainer responses) for plexapi 4.x and the engine's raw ``/hubs`` calls to work
unmodified. ``make_fake_plextv(state)`` builds the plex.tv surface (``/api/users`` XML,
``/api/v2/*`` JSON) the engine's ``PlexTvClient`` talks to. Both share one ``FakePlexState``,
so tests can assert on server-side effects directly.

Fidelity notes (mirrors of real-Plex behavior the engine depends on):
- New labels are stored title-cased (``shortlist_x`` -> ``Shortlist_x``), like a real PMS.
- ``/hubs`` respects the requesting token: ``server-<accountID>`` tokens see only collections
  promoted to shared Home whose labels are NOT in that user's ``label!=`` share-filter excludes.
- plex.tv Home-user switch mints ``switch-<id>``; ``/api/v2/resources`` exchanges it for the
  server-scoped ``server-<id>`` token (the T2 canary mechanism).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

FILTER_FIELDS = ("filterAll", "filterMovies", "filterTelevision", "filterMusic", "filterPhotos")
_LABEL_PARAM = re.compile(r"^label\[\d+\]\.tag\.tag$")
_SORT_KEYS = {
    "addedAt": lambda m: m.added_at,
    "audienceRating": lambda m: m.audience_rating,
    "titleSort": lambda m: m.title,
}


@dataclass
class FakeMovie:
    """A library item. `media_type` decides how the PMS serves it (<Video> vs <Directory>).

    Which library it lives in is decided by the FakeSection that holds it — NOT by this field. The
    same title can exist in two libraries ("Movies" and "4K Movies") under different ratingKeys.
    """

    rating_key: int
    title: str
    year: int
    added_at: int  # epoch seconds
    tmdb_id: int
    audience_rating: float
    media_type: str = "movie"  # "movie" | "show"
    leaf_count: int = 0  # total episodes (shows only); the share-token watched read serves it as leafCount


@dataclass
class FakeSection:
    """One Plex library: its own key, type, title and items.

    A server can have SEVERAL libraries of one type — "Movies" + "4K Movies" is a very common
    layout. A fake that can only model one library per type cannot reproduce it, and that blind
    spot hid two production bugs: a row delivered to a non-lowest-keyed library was never promoted
    (so it stayed visible in library browse to everyone), and a row pinned to one library was
    curated against the union of all of them.
    """

    key: int
    type: str  # "movie" | "show"
    title: str
    items: dict[int, FakeMovie] = field(default_factory=dict)


def _default_sections() -> dict[int, FakeSection]:
    """The one-library-per-type layout every existing test assumes: Movies(1), TV Shows(2)."""
    return {
        1: FakeSection(key=1, type="movie", title="Movies"),
        2: FakeSection(key=2, type="show", title="TV Shows"),
    }


@dataclass
class FakeCollection:
    rating_key: int
    title: str
    section_id: int
    labels: list[str] = field(default_factory=list)  # stored casing, like the PMS keeps them
    item_keys: list[int] = field(default_factory=list)  # ordered item rating keys
    # Plex fixes a collection's subtype at CREATION from the items it is created with, and never
    # revises it — swapping the contents later does not re-type it. That stickiness is why a
    # mistyped collection cannot be repaired in place: it has to be deleted and recreated.
    subtype: str = "movie"
    mode: int = -1  # -1 default / 0 hide (plexapi collectionMode enum)
    sort: int = 0  # 0 release / 1 alpha / 2 custom
    promoted_recommended: bool = False
    promoted_own_home: bool = False
    promoted_shared_home: bool = False
    pinned_top: bool = False  # moved to the front of this library's Managed Recommendations


@dataclass
class FakeUser:
    id: int
    username: str
    home: bool = False
    restricted: bool = False
    protected: bool = False
    uuid: str = ""
    filters: dict[str, str] = field(default_factory=lambda: dict.fromkeys(FILTER_FIELDS, ""))


@dataclass
class FakeHistoryEntry:
    account_id: int
    rating_key: int
    viewed_at: int  # epoch seconds


@dataclass
class FakePlexState:
    """Shared in-memory truth for both fake servers; tests assert on it directly."""

    machine_id: str = "fake-machine-1"
    friendly_name: str = "FakePlex"
    version: str = "1.43.3.10793"
    owner_token: str = "owner-token"
    owner_account_id: int = 555000001  # the owner's plex.tv id
    owner_username: str = "steve"
    # PMS keeps its own account table and files the OWNER's watch history under a local id, not
    # their plex.tv one — so `accountID=<owner_account_id>` matches nothing. Shared users are listed
    # under their plex.tv id, which is why only the owner needs resolving.
    owner_pms_account_id: int = 1
    pms_url: str = "http://127.0.0.1:32400"  # set by the harness once the fake PMS has a port
    sections: dict[int, FakeSection] = field(default_factory=_default_sections)
    collections: dict[int, FakeCollection] = field(default_factory=dict)
    users: dict[int, FakeUser] = field(default_factory=dict)  # owner is NOT in this dict
    history: list[FakeHistoryEntry] = field(default_factory=list)
    next_rating_key: int = 5000

    @property
    def section_id(self) -> int:
        """The default (lowest-keyed) movie library — what a one-library-per-type test means."""
        return self.default_section("movie").key

    @property
    def show_section_id(self) -> int:
        return self.default_section("show").key

    @property
    def movies(self) -> dict[int, FakeMovie]:
        """Items of the DEFAULT movie library. A test with several movie libraries must address
        them through `sections`, since "the movie library" is no longer a single thing."""
        return self.default_section("movie").items

    @property
    def shows(self) -> dict[int, FakeMovie]:
        return self.default_section("show").items

    def default_section(self, kind: str) -> FakeSection:
        """The lowest-keyed library of a type — the one `sections_by_type()` picks."""
        return min((s for s in self.sections.values() if s.type == kind), key=lambda s: s.key)

    def add_section(self, key: int, kind: str, title: str) -> FakeSection:
        """Add a library. A second library of an existing type is the point: see FakeSection."""
        section = FakeSection(key=key, type=kind, title=title)
        self.sections[key] = section
        return section

    def sections_of(self, kind: str) -> list[FakeSection]:
        return sorted((s for s in self.sections.values() if s.type == kind), key=lambda s: s.key)

    def new_rating_key(self) -> int:
        self.next_rating_key += 1
        return self.next_rating_key

    def item(self, rating_key: int) -> FakeMovie | None:
        for section in self.sections.values():
            if rating_key in section.items:
                return section.items[rating_key]
        return None

    def section_of(self, rating_key: int) -> FakeSection | None:
        """Which library holds this item. RatingKeys are server-unique, so at most one does."""
        return next((s for s in self.sections.values() if rating_key in s.items), None)

    def section_type(self, section_id: int) -> str:
        return self.sections[section_id].type

    def items_in(self, section_id: int) -> dict[int, FakeMovie]:
        return self.sections[section_id].items

    def titles_of_type(self, kind: str) -> list[FakeMovie]:
        """Every distinct TMDB title of a type, across every library of that type.

        Deduped by tmdb_id: the same film in "Movies" and "4K Movies" is ONE title as far as
        anything upstream of Plex (TMDB, the curator) is concerned.
        """
        by_tmdb: dict[int, FakeMovie] = {}
        for section in self.sections_of(kind):
            for item in section.items.values():
                by_tmdb.setdefault(item.tmdb_id, item)
        return list(by_tmdb.values())

    def members(self, collection: FakeCollection) -> list[int]:
        """The items a collection actually contains, per Plex's real model.

        A Plex collection is a TAG on items, keyed by TITLE within a library — not an independent
        bag with its own membership. So two collections with the same title in the same library
        are ONE membership: each returns the union of both. Verified on a live server (SFLIX,
        2026-07-13): a film picked for one user alone appeared in another user's row, carrying a
        single collection tag.

        This is why every user's row must have a title no other row in that library uses. Modelling
        collections as independent objects is exactly what let the bug ship.
        """
        keys: list[int] = []
        for other in self.collections.values():
            if other.section_id != collection.section_id or other.title != collection.title:
                continue
            for key in other.item_keys:
                if key not in keys:
                    keys.append(key)
        return keys

    def filterable(self, collection: FakeCollection) -> bool:
        """Whether a real PMS could hide this collection with a `label!=` share filter.

        Share filters are applied per library: `filterMovies` to the movie libraries,
        `filterTelevision` to the TV ones. A collection whose SUBTYPE doesn't match the library
        it sits in (e.g. a show-subtype collection inside a movie library) is matched by NEITHER
        filter, so its label exclude does nothing and it stays visible to every user. That is not
        a hypothetical: it is exactly how two users' rows ended up on everyone's Home screen on a
        live server (SFLIX, 2026-07-12).

        Subtype is sticky — see FakeCollection.subtype — so swapping in items of the right type
        does NOT make a mistyped collection filterable again.
        """
        return collection.subtype == self.section_type(collection.section_id)

    @staticmethod
    def store_label(label: str) -> str:
        """Title-case a new label exactly like a real PMS does (``shortlist_x`` -> ``Shortlist_x``)."""
        return label[:1].upper() + label[1:] if label else label

    def user_for_token(self, token: str) -> FakeUser | None:
        """Resolve a server-scoped ``server-<accountID>`` token; None for unknown tokens."""
        if token.startswith("server-") and token.removeprefix("server-").isdigit():
            return self.users.get(int(token.removeprefix("server-")))
        return None

    def watched_account_id(self, token: str) -> int | None:
        """Which PMS account a share-token watched read (``unwatched=0``) speaks for.

        The share-token read is served AS the token's owner, so the watched set is theirs. The owner
        reads with the admin token and their history is filed under the local ``owner_pms_account_id``
        (never their plex.tv id — see the class notes); a shared/Home user reads with a
        ``server-<accountID>`` token filed under that same plex.tv id.
        """
        if token == self.owner_token:
            return self.owner_pms_account_id
        user = self.user_for_token(token)
        return user.id if user else None

    def watched_keys(self, account_id: int) -> set[int]:
        """The rating keys this account has watched (movies and shows), from ``history``."""
        return {h.rating_key for h in self.history if h.account_id == account_id}

    def last_viewed_at(self, account_id: int, rating_key: int) -> int:
        """The most recent watch time for one title, or 0 if never watched by this account."""
        times = [h.viewed_at for h in self.history if h.account_id == account_id and h.rating_key == rating_key]
        return max(times, default=0)

    @staticmethod
    def excluded_labels(user: FakeUser) -> set[str]:
        """Lowercased ``label!=`` values across the user's movie/TV share filters."""
        excludes: set[str] = set()
        for fieldname in ("filterMovies", "filterTelevision"):
            for condition in (user.filters.get(fieldname) or "").split("|"):
                if condition.startswith("label!="):
                    excludes.update(v.lower() for v in condition.removeprefix("label!=").split(",") if v)
        return excludes


def seed_state() -> FakePlexState:
    """Two libraries (30 movies, 30 shows), 3 users (one Home canary without a PIN), history.

    The TV library is not decoration: a server with only movies cannot exhibit the class of bug
    where a show is delivered into a movie collection, so every test would pass while the real
    thing leaked.
    """
    state = FakePlexState()
    base_added = 1_700_000_000
    for i in range(1, 31):
        state.movies[100 + i] = FakeMovie(
            rating_key=100 + i,
            title=f"Movie {i:02d}",
            year=1990 + i,
            added_at=base_added + i * 86_400,
            tmdb_id=9000 + i,
            audience_rating=5.0 + (i * 7) % 40 / 10,
        )
    # 30 shows, like the movie library: a TV catalog barely bigger than what a user has already
    # watched starves the candidate pool and makes row sizes a property of the fixture, not the
    # engine.
    for i in range(1, 31):
        state.shows[300 + i] = FakeMovie(
            rating_key=300 + i,
            title=f"Show {i:02d}",
            year=2000 + i,
            added_at=base_added + i * 86_400,
            tmdb_id=7000 + i,
            audience_rating=5.0 + (i * 3) % 40 / 10,
            media_type="show",
            leaf_count=10,  # 10 episodes; a seeded "watched" show is served fully watched (finished)
        )
    state.users[201] = FakeUser(id=201, username="sarah")
    state.users[202] = FakeUser(id=202, username="mike")
    state.users[203] = FakeUser(id=203, username="canary", home=True, uuid="uuid-203")
    base_viewed = 1_752_000_000
    # One run then covers the whole delivery matrix: sarah watches both types (two rows), mike
    # watches only TV (one row, in the TV library), the canary has no history (cold start).
    watched = {
        201: list(range(101, 109)) + list(range(301, 305)),
        202: list(range(305, 313)),
        # The owner, under the id PMS files THEM under — never their plex.tv id. A test that seeded
        # this under `owner_account_id` would pass while the real server returned nothing.
        state.owner_pms_account_id: list(range(109, 117)) + list(range(313, 317)),
    }
    for account, keys in watched.items():
        for offset, key in enumerate(keys):
            state.history.append(FakeHistoryEntry(account_id=account, rating_key=key, viewed_at=base_viewed + offset))
    return state


def _xml(root: Element) -> Response:
    return Response(content=tostring(root, encoding="unicode"), media_type="text/xml")


def _el(parent: Element, name: str, /, **attrs) -> Element:
    child = SubElement(parent, name)
    for key, value in attrs.items():
        child.set(key, str(value))
    return child


def _container(**attrs) -> Element:
    root = Element("MediaContainer")
    for key, value in attrs.items():
        root.set(key, str(value))
    return root


def _movie_xml(parent: Element, state: FakePlexState, movie: FakeMovie, *, watched_by: int | None = None) -> Element:
    """One library item. Plex serves movies as <Video> and shows as <Directory>.

    When ``watched_by`` is set, the element also carries that account's per-user watched counts the way
    a real ``unwatched=0`` read does: ``viewCount``/``lastViewedAt`` for a movie, and
    ``viewedLeafCount``/``leafCount`` for a show (the share-token watched read reads exactly these).
    """
    is_show = movie.media_type == "show"
    section = state.section_of(movie.rating_key)
    element = _el(
        parent,
        "Directory" if is_show else "Video",
        ratingKey=movie.rating_key,
        key=f"/library/metadata/{movie.rating_key}" + ("/children" if is_show else ""),
        type=movie.media_type,
        title=movie.title,
        year=movie.year,
        addedAt=movie.added_at,
        audienceRating=movie.audience_rating,
        # The library that actually holds it — never inferred from the type, or a second movie
        # library's items would all claim to live in the first one.
        librarySectionID=section.key if section else state.section_id,
    )
    if watched_by is not None:
        element.set("lastViewedAt", str(state.last_viewed_at(watched_by, movie.rating_key)))
        if is_show:
            # A watched show in the fixture is served fully watched (viewed == total) — the common
            # "finished the series" case, which the engine then treats as finished AND a seed.
            element.set("viewedLeafCount", str(movie.leaf_count))
            element.set("leafCount", str(movie.leaf_count))
        else:
            # viewCount = how many times this account has this title in history (>= 1, since it's watched).
            plays = sum(1 for h in state.history if h.account_id == watched_by and h.rating_key == movie.rating_key)
            element.set("viewCount", str(max(1, plays)))
    _el(element, "Guid", id=f"tmdb://{movie.tmdb_id}")
    return element


def _collection_xml(parent: Element, state: FakePlexState, collection: FakeCollection) -> Element:
    directory = _el(
        parent,
        "Directory",
        ratingKey=collection.rating_key,
        key=f"/library/metadata/{collection.rating_key}/children",
        type="collection",
        subtype=collection.subtype,
        title=collection.title,
        smart="0",
        collectionMode=collection.mode,
        collectionSort=collection.sort,
        librarySectionID=collection.section_id,
    )
    for i, tag in enumerate(collection.labels, start=1):
        _el(directory, "Label", id=i, tag=tag)
    # plexapi's editAdvanced (modeUpdate/sortUpdate) reads these to validate enum values.
    preferences = SubElement(directory, "Preferences")
    for setting_id, default, value, enums in (
        ("collectionMode", "-1", collection.mode, "-1:Library default|0:Hide collection|1:Hide items|2:Show items"),
        ("collectionSort", "0", collection.sort, "0:Release date|1:Alphabetical|2:Custom"),
    ):
        _el(preferences, "Setting", id=setting_id, type="int", default=default, value=value, enumValues=enums)
    return directory


def _managed_hub_xml(parent: Element, section_id: int, collection: FakeCollection) -> Element:
    # Identifier matches what plexapi synthesizes (custom.collection.<sectionID>.<ratingKey>)
    # so ManagedHub.reload() can find this hub again after updateVisibility.
    return _el(
        parent,
        "Hub",
        identifier=f"custom.collection.{section_id}.{collection.rating_key}",
        title=collection.title,
        deletable="1",
        promotedToRecommended=int(collection.promoted_recommended),
        promotedToOwnHome=int(collection.promoted_own_home),
        promotedToSharedHome=int(collection.promoted_shared_home),
        homeVisibility="all" if collection.promoted_shared_home else "none",
        recommendationsVisibility="all" if collection.promoted_recommended else "none",
    )


def _page(request: Request, total: int) -> tuple[int, int]:
    """Container paging: plexapi sends X-Plex-Container-Start/Size as headers OR query params."""
    query, headers = request.query_params, request.headers
    start = int(query.get("X-Plex-Container-Start") or headers.get("X-Plex-Container-Start") or 0)
    raw_size = query.get("X-Plex-Container-Size") or headers.get("X-Plex-Container-Size")
    size = int(raw_size) if raw_size is not None else total
    return start, size


def _meta_xml(state: FakePlexState, section_id: int, total: int) -> Element:
    """Filter metadata plexapi loads before validating any sort= argument."""
    root = _container(size=0, totalSize=total)
    meta = SubElement(root, "Meta")
    section = state.sections[section_id]
    kind = section.type
    item_type = _el(
        meta,
        "Type",
        key=f"/library/sections/{section_id}/all?type={1 if kind == 'movie' else 2}",
        type=kind,
        title=section.title,
    )
    for key, direction, title in (
        ("addedAt", "asc", "Date Added"),
        ("audienceRating", "desc", "Audience Rating"),
        ("titleSort", "asc", "Title"),
    ):
        _el(item_type, "Sort", key=key, defaultDirection=direction, title=title)
    collection_type = _el(
        meta, "Type", key=f"/library/sections/{section_id}/all?type=18", type="collection", title="Collections"
    )
    _el(collection_type, "Sort", key="titleSort", defaultDirection="asc", title="Title")
    return root


def _sorted_items(items: list[FakeMovie], sort: str | None) -> list[FakeMovie]:
    if not sort:
        return sorted(items, key=lambda m: m.rating_key)
    fieldname, _, direction = sort.split(",")[0].rsplit(".", 1)[-1].partition(":")  # 'movie.addedAt:asc' -> addedAt
    return sorted(items, key=_SORT_KEYS.get(fieldname, lambda m: m.rating_key), reverse=direction == "desc")


def make_fake_plex(state: FakePlexState) -> FastAPI:
    """PMS surface (path prefix '') — enough for plexapi + the engine's raw /hubs calls."""
    app = FastAPI()

    def _collection(rating_key: int) -> FakeCollection:
        collection = state.collections.get(rating_key)
        if collection is None:
            raise HTTPException(status_code=404, detail=f"collection {rating_key} not found")
        return collection

    @app.get("/")
    @app.get("/identity")
    def root() -> Response:
        attrs = {"friendlyName": state.friendly_name, "machineIdentifier": state.machine_id}
        return _xml(_container(size=0, version=state.version, **attrs))

    @app.get("/library")
    def library_root() -> Response:
        return _xml(_container(size=1, title1="Plex Library", identifier="com.plexapp.plugins.library"))

    @app.get("/library/sections")
    def sections() -> Response:
        root = _container(size=len(state.sections), allowSync="0", title1="Plex Library")
        for section in state.sections.values():
            _el(
                root,
                "Directory",
                key=section.key,
                type=section.type,
                title=section.title,
                uuid=f"section-uuid-{section.key}",
                filters="1",
            )
        return _xml(root)

    @app.get("/library/sections/{section_id}/all")
    @app.get("/library/sections/{section_id}/collections")
    def section_all(section_id: int, request: Request) -> Response:
        query = request.query_params
        items = state.items_in(section_id)
        if query.get("includeMeta") == "1":
            return _xml(_meta_xml(state, section_id, len(items)))
        if query.get("type") == "18" or request.url.path.endswith("/collections"):
            owned = [c for c in state.collections.values() if c.section_id == section_id]
            root = _container(size=len(owned), totalSize=len(owned), librarySectionID=section_id)
            for collection in owned:
                _collection_xml(root, state, collection)
            return _xml(root)
        # The share-token watched read (ShareTokenWatchSource): `unwatched=0` filters to what the
        # REQUESTING account has watched, served AS them with their own per-user viewCount/leaf counts.
        # The token is in the X-Plex-Token header (includeToken=False keeps the owner's out of the URL).
        if query.get("unwatched") == "0":
            account_id = state.watched_account_id(request.headers.get("X-Plex-Token", ""))
            watched = state.watched_keys(account_id) if account_id is not None else set()
            listing = [item for item in _sorted_items(list(items.values()), None) if item.rating_key in watched]
            start, size = _page(request, len(listing))
            page = listing[start : start + size]
            root = _container(size=len(page), totalSize=len(listing), librarySectionID=section_id)
            for item in page:
                _movie_xml(root, state, item, watched_by=account_id)
            return _xml(root)
        listing = _sorted_items(list(items.values()), query.get("sort"))
        if query.get("limit") is not None:
            listing = listing[: int(query["limit"])]
        start, size = _page(request, len(listing))
        page = listing[start : start + size]
        root = _container(size=len(page), totalSize=len(listing), librarySectionID=section_id)
        for item in page:
            _movie_xml(root, state, item)
        return _xml(root)

    @app.put("/library/sections/{section_id}/all")
    def section_edit(section_id: int, request: Request) -> Response:
        """plexapi's tag/field edit endpoint (addLabel, editTitle): type=18&id=...&label[0].tag.tag=..."""
        query = request.query_params
        labels = [value for key, value in query.multi_items() if _LABEL_PARAM.match(key)]
        for raw_id in (query.get("id") or "").split(","):
            collection = state.collections.get(int(raw_id)) if raw_id.isdigit() else None
            if collection is None:
                continue
            if labels:
                existing = {label.lower(): label for label in collection.labels}
                collection.labels = [existing.get(v.lower(), state.store_label(v)) for v in labels]
            if query.get("title.value"):
                collection.title = query["title.value"]
        return Response(status_code=200)

    @app.post("/library/collections")
    def create_collection(request: Request) -> Response:
        query = request.query_params
        item_keys = [int(k) for k in query["uri"].rsplit("/library/metadata/", 1)[-1].split(",")]
        kept = [k for k in item_keys if state.item(k)]
        types = {state.item(k).media_type for k in kept}
        collection = FakeCollection(
            rating_key=state.new_rating_key(),
            title=query.get("title", ""),
            section_id=int(query.get("sectionId") or state.section_id),
            # The PMS happily puts a collection of shows in a movie library — it only objects to
            # MIXING types in one collection (plexapi rejects that client-side). Refusing the
            # wrong-library case here would hide the very bug this fake exists to catch.
            item_keys=kept,
            # Subtype comes from the items, NOT from the library — that is how a movie library
            # ends up holding a show-subtype collection that no share filter can touch.
            subtype=types.pop() if len(types) == 1 else "movie",
        )
        state.collections[collection.rating_key] = collection
        root = _container(size=1)
        _collection_xml(root, state, collection)
        return _xml(root)

    @app.get("/library/metadata/{rating_key}/children")
    def collection_children(rating_key: int) -> Response:
        collection = _collection(rating_key)
        members = state.members(collection)  # shared with any same-titled collection in this library
        root = _container(size=len(members), totalSize=len(members))
        for key in members:
            if (item := state.item(key)) is not None:
                _movie_xml(root, state, item)
        return _xml(root)

    @app.put("/library/metadata/{rating_key}/items")
    def collection_add_items(rating_key: int, request: Request) -> Response:
        collection = _collection(rating_key)
        for raw in request.query_params["uri"].rsplit("/library/metadata/", 1)[-1].split(","):
            key = int(raw)
            if state.item(key) and key not in collection.item_keys:
                collection.item_keys.append(key)
        return Response(status_code=200)

    @app.delete("/library/metadata/{rating_key}/items/{item_key}")
    def collection_remove_item(rating_key: int, item_key: int) -> Response:
        collection = _collection(rating_key)
        collection.item_keys = [k for k in collection.item_keys if k != item_key]
        return Response(status_code=200)

    @app.put("/library/metadata/{rating_key}/items/{item_key}/move")
    def collection_move_item(rating_key: int, item_key: int, request: Request) -> Response:
        collection = _collection(rating_key)
        after = request.query_params.get("after")
        collection.item_keys.remove(item_key)
        position = collection.item_keys.index(int(after)) + 1 if after else 0
        collection.item_keys.insert(position, item_key)
        return Response(status_code=200)

    @app.put("/library/metadata/{rating_key}/prefs")
    def collection_prefs(rating_key: int, request: Request) -> Response:
        collection = _collection(rating_key)
        if request.query_params.get("collectionMode") is not None:
            collection.mode = int(request.query_params["collectionMode"])
        if request.query_params.get("collectionSort") is not None:
            collection.sort = int(request.query_params["collectionSort"])
        return Response(status_code=200)

    @app.delete("/library/metadata/{rating_key}")
    def delete_collection(rating_key: int) -> Response:
        collection = _collection(rating_key)
        # A collection is a TAG keyed by title. We have verified on a real PMS that same-titled
        # collections in one library SHARE their membership; we have NOT verified what deleting one
        # does to the others. So the fake assumes the worse of the two possibilities — the tag goes,
        # and every same-titled sibling empties — because code that is correct under that is correct
        # either way, and code that is only correct under the kinder assumption would fail live.
        for other in state.collections.values():
            if (
                other is not collection
                and other.section_id == collection.section_id
                and other.title == collection.title
            ):
                other.item_keys = [k for k in other.item_keys if k not in collection.item_keys]
        del state.collections[rating_key]
        return Response(status_code=200)

    @app.get("/library/metadata/{rating_keys}")
    def metadata(rating_keys: str) -> Response:
        root = _container(librarySectionID=state.section_id)
        found = 0
        for raw in rating_keys.split(","):
            key = int(raw)
            if (item := state.item(key)) is not None:
                _movie_xml(root, state, item)
                found += 1
            elif key in state.collections:
                _collection_xml(root, state, state.collections[key])
                found += 1
        if not found:
            raise HTTPException(status_code=404, detail=f"no items for {rating_keys}")
        root.set("size", str(found))
        return _xml(root)

    @app.get("/hubs/sections/{section_id}/manage")
    def manage_hubs(section_id: int, request: Request) -> Response:
        wanted = request.query_params.get("metadataItemId")
        root = _container()
        for collection in state.collections.values():
            if collection.section_id != section_id:
                continue
            if wanted is not None and collection.rating_key != int(wanted):
                continue
            _managed_hub_xml(root, section_id, collection)
        root.set("size", str(len(root)))
        return _xml(root)

    def _apply_hub_flags(collection: FakeCollection, query) -> None:
        collection.promoted_recommended = query.get("promotedToRecommended") == "1"
        collection.promoted_own_home = query.get("promotedToOwnHome") == "1"
        collection.promoted_shared_home = query.get("promotedToSharedHome") == "1"

    @app.post("/hubs/sections/{section_id}/manage")
    def promote_hub(section_id: int, request: Request) -> Response:
        collection = _collection(int(request.query_params["metadataItemId"]))
        _apply_hub_flags(collection, request.query_params)
        return Response(status_code=200)

    @app.put("/hubs/sections/{section_id}/manage/{identifier}/move")
    def move_hub(section_id: int, identifier: str, request: Request) -> Response:
        # after=None (no query) -> pinned to the top of the Managed Recommendations shelf.
        collection = _collection(int(identifier.rsplit(".", 1)[-1]))
        collection.pinned_top = request.query_params.get("after") is None
        return Response(status_code=200)

    @app.put("/hubs/sections/{section_id}/manage/{identifier}")
    def update_hub(section_id: int, identifier: str, request: Request) -> Response:
        collection = _collection(int(identifier.rsplit(".", 1)[-1]))
        _apply_hub_flags(collection, request.query_params)
        return Response(status_code=200)

    @app.get("/hubs")
    def hubs(request: Request) -> JSONResponse:
        token = request.headers.get("X-Plex-Token", "")
        user = state.user_for_token(token)
        if user is None and token != state.owner_token:
            return JSONResponse({"errors": [{"code": 1001, "message": "Unauthorized"}]}, status_code=401)
        excludes = state.excluded_labels(user) if user else set()
        hub_list: list[dict] = [
            {"key": "/hubs/home/continueWatching", "title": "Continue Watching", "type": "mixed", "promoted": True}
        ]
        for collection in state.collections.values():
            promoted = collection.promoted_shared_home if user else collection.promoted_own_home
            if not promoted:
                continue
            excluded = bool({label.lower() for label in collection.labels} & excludes)
            # An exclude only takes effect if the PMS can actually match this collection with a
            # library filter. Off-type collections are unfilterable and stay visible — the leak.
            if excluded and state.filterable(collection):
                continue
            children_key = f"/library/collections/{collection.rating_key}/children"
            hub_list.append(
                {
                    "key": children_key,
                    "hubKey": children_key,
                    "title": collection.title,
                    "type": collection.subtype,
                    "hubIdentifier": f"custom.collection.{collection.rating_key}",
                    "promoted": True,
                }
            )
        return JSONResponse({"MediaContainer": {"size": len(hub_list), "Hub": hub_list}})

    @app.get("/accounts")
    def accounts() -> Response:
        """PMS's own account table. The owner is a LOCAL account here (not their plex.tv id), which
        is why the owner's history has to be looked up by the id PMS files it under."""
        root = _container(size=len(state.users) + 2)
        _el(root, "Account", id=0, key="/accounts/0", name="", defaultAudioLanguage="", autoSelectAudio="1")
        _el(
            root,
            "Account",
            id=state.owner_pms_account_id,
            key=f"/accounts/{state.owner_pms_account_id}",
            name=state.owner_username,
            defaultAudioLanguage="en",
            autoSelectAudio="1",
        )
        for user in state.users.values():
            _el(root, "Account", id=user.id, key=f"/accounts/{user.id}", name=user.username, autoSelectAudio="1")
        return _xml(root)

    @app.get("/status/sessions/history/all")
    def history(request: Request) -> Response:
        account_id = request.query_params.get("accountID")
        rows = [h for h in state.history if account_id is None or h.account_id == int(account_id)]
        rows.sort(key=lambda h: h.viewed_at, reverse=True)
        start, size = _page(request, len(rows))
        page = rows[start : start + size]
        root = _container(size=len(page), totalSize=len(rows))
        for i, row in enumerate(page):
            item = state.item(row.rating_key)
            if item is None:
                continue
            attrs = {
                "historyKey": f"/status/sessions/history/{start + i + 1}",
                "key": f"/library/metadata/{item.rating_key}",
                "ratingKey": item.rating_key,
                "title": item.title,
                "type": item.media_type,
                "viewedAt": row.viewed_at,
                "accountID": row.account_id,
            }
            if item.media_type == "show":
                # Plex logs TV watches as EPISODE rows: the show is the grandparent, and the
                # episode's own title/ratingKey are useless as a recommendation seed.
                attrs |= {
                    "type": "episode",
                    "title": f"Episode {i + 1}",
                    "ratingKey": 90_000 + item.rating_key,
                    "grandparentTitle": item.title,
                    "grandparentRatingKey": item.rating_key,
                }
            _el(root, "Video", **attrs)
        return _xml(root)

    return app


def make_fake_plextv(state: FakePlexState) -> FastAPI:
    """plex.tv surface — mounted as its own app because the engine hits absolute plex.tv URLs."""
    app = FastAPI()

    @app.get("/api/users")
    def list_users() -> Response:
        root = _container(friendlyName="myPlex", identifier="com.plexapp.plugins.myplex", size=len(state.users))
        for user in state.users.values():
            user_el = _el(
                root,
                "User",
                id=user.id,
                title=user.username,
                username=user.username,
                email=f"{user.username}@example.com",
                thumb=f"https://plex.tv/users/{user.id}/avatar",
                home=int(user.home),
                restricted=int(user.restricted),
                protected=int(user.protected),
                **user.filters,
            )
            _el(user_el, "Server", id=user.id, serverId="1", machineIdentifier=state.machine_id, name="FakePlex")
        return _xml(root)

    @app.get("/api/servers/{machine_id}/shared_servers")
    def shared_servers(machine_id: str) -> Response:
        """The per-user server tokens plex.tv mints for every shared invite (ShareTokenWatchSource).

        Every shared/Home user gets one; the token is ``server-<accountID>`` so the PMS fake serves
        that user's own watched set for it (``user_for_token``). The owner is NOT here — they own the
        server rather than being shared to it, so the source reads their state with the admin token.
        """
        root = _container(size=len(state.users))
        for user in state.users.values():
            _el(
                root,
                "SharedServer",
                id=user.id,
                userID=user.id,
                username=user.username,
                accessToken=f"server-{user.id}",
                machineIdentifier=machine_id,
            )
        return _xml(root)

    @app.put("/api/users/{account_id}")
    def update_user(account_id: int, request: Request) -> Response:
        user = state.users.get(account_id)
        if user is None:
            raise HTTPException(status_code=404, detail=f"user {account_id} not found")
        for fieldname in FILTER_FIELDS:
            if fieldname in request.query_params:
                user.filters[fieldname] = request.query_params[fieldname]
        return Response(status_code=200, content="<Response code='200'/>", media_type="text/xml")

    @app.get("/api/v2/home/users")
    def home_users() -> JSONResponse:
        home = [u for u in state.users.values() if u.home]
        rows = [{"id": u.id, "uuid": u.uuid, "title": u.username, "protected": u.protected} for u in home]
        return JSONResponse({"users": rows})

    @app.post("/api/v2/home/users/{uuid}/switch")
    def switch_home_user(uuid: str) -> JSONResponse:
        user = next((u for u in state.users.values() if u.uuid == uuid), None)
        if user is None:
            raise HTTPException(status_code=404, detail=f"unknown home user {uuid}")
        return JSONResponse({"authToken": f"switch-{user.id}"})

    @app.post("/api/v2/pins")
    def create_pin() -> JSONResponse:
        """Mint a PIN. The e2e sign-in must run the REAL endpoint: a browser stub that forged the
        session cookie would keep passing even if `poll_pin` stopped setting one."""
        return JSONResponse({"id": 1234, "code": "ABCD"})

    @app.get("/api/v2/pins/{pin_id}")
    def poll_pin(pin_id: int) -> JSONResponse:
        """Already linked — a human typed the code at plex.tv while we weren't looking."""
        return JSONResponse({"id": pin_id, "code": "ABCD", "authToken": state.owner_token})

    @app.get("/api/v2/user")
    def whoami(request: Request) -> JSONResponse:
        """Who this token belongs to, and whether they have Plex Pass (the setup probe asks)."""
        if request.headers.get("X-Plex-Token") != state.owner_token:
            raise HTTPException(status_code=401, detail="bad token")
        return JSONResponse(
            {
                "id": state.owner_account_id,
                "username": state.owner_username,
                "title": state.owner_username.title(),
                "thumb": f"https://plex.tv/users/{state.owner_account_id}/avatar",
                "subscription": {"active": True},
            }
        )

    @app.get("/api/v2/resources")
    def resources(request: Request) -> JSONResponse:
        token = request.headers.get("X-Plex-Token", "")
        server = {
            "name": state.friendly_name,
            "clientIdentifier": state.machine_id,
            "provides": "server",
            "productVersion": state.version,
            "owned": True,
            # What the server picker consumes: several advertised addresses, only one of which
            # actually answers — exactly the situation the picker exists to resolve.
            "connections": [
                {"uri": state.pms_url, "local": True, "relay": False},
                {"uri": "http://10.255.255.1:32400", "local": False, "relay": False},
            ],
        }
        if token.startswith("switch-"):
            # The T2 canary flow: exchange a Home-user switch token for a server access token.
            account_id = token.removeprefix("switch-")
            return JSONResponse([{**server, "accessToken": f"server-{account_id}"}])
        return JSONResponse([server])

    return app
