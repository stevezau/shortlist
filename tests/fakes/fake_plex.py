"""In-memory fakes of a Plex Media Server and plex.tv, served over real HTTP.

``make_fake_plex(state)`` builds a FastAPI app speaking just enough of the PMS wire protocol
(XML MediaContainer responses) for plexapi 4.x and the engine's raw ``/hubs`` calls to work
unmodified. ``make_fake_plextv(state)`` builds the plex.tv surface (``/api/users`` XML,
``/api/v2/*`` JSON) the engine's ``PlexTvClient`` talks to. Both share one ``FakePlexState``,
so tests can assert on server-side effects directly.

Fidelity notes (mirrors of real-Plex behavior the engine depends on):
- New labels are stored title-cased (``rowarr_x`` -> ``Rowarr_x``), like a real PMS.
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
    rating_key: int
    title: str
    year: int
    added_at: int  # epoch seconds
    tmdb_id: int
    audience_rating: float


@dataclass
class FakeCollection:
    rating_key: int
    title: str
    section_id: int
    labels: list[str] = field(default_factory=list)  # stored casing, like the PMS keeps them
    item_keys: list[int] = field(default_factory=list)  # ordered movie rating keys
    mode: int = -1  # -1 default / 0 hide (plexapi collectionMode enum)
    sort: int = 0  # 0 release / 1 alpha / 2 custom
    promoted_recommended: bool = False
    promoted_own_home: bool = False
    promoted_shared_home: bool = False


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
    section_id: int = 1
    movies: dict[int, FakeMovie] = field(default_factory=dict)
    collections: dict[int, FakeCollection] = field(default_factory=dict)
    users: dict[int, FakeUser] = field(default_factory=dict)  # owner is NOT in this dict
    history: list[FakeHistoryEntry] = field(default_factory=list)
    next_rating_key: int = 5000

    def new_rating_key(self) -> int:
        self.next_rating_key += 1
        return self.next_rating_key

    @staticmethod
    def store_label(label: str) -> str:
        """Title-case a new label exactly like a real PMS does (``rowarr_x`` -> ``Rowarr_x``)."""
        return label[:1].upper() + label[1:] if label else label

    def user_for_token(self, token: str) -> FakeUser | None:
        """Resolve a server-scoped ``server-<accountID>`` token; None for unknown tokens."""
        if token.startswith("server-") and token.removeprefix("server-").isdigit():
            return self.users.get(int(token.removeprefix("server-")))
        return None

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
    """~30 movies with TMDB guids, 3 users (one Home canary without a PIN), history for 2 users."""
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
    state.users[201] = FakeUser(id=201, username="sarah")
    state.users[202] = FakeUser(id=202, username="mike")
    state.users[203] = FakeUser(id=203, username="canary", home=True, uuid="uuid-203")
    base_viewed = 1_752_000_000
    for account, keys in ((201, range(101, 109)), (202, range(110, 118))):
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


def _movie_xml(parent: Element, state: FakePlexState, movie: FakeMovie) -> Element:
    video = _el(
        parent,
        "Video",
        ratingKey=movie.rating_key,
        key=f"/library/metadata/{movie.rating_key}",
        type="movie",
        title=movie.title,
        year=movie.year,
        addedAt=movie.added_at,
        audienceRating=movie.audience_rating,
        librarySectionID=state.section_id,
    )
    _el(video, "Guid", id=f"tmdb://{movie.tmdb_id}")
    return video


def _collection_xml(parent: Element, state: FakePlexState, collection: FakeCollection) -> Element:
    directory = _el(
        parent,
        "Directory",
        ratingKey=collection.rating_key,
        key=f"/library/metadata/{collection.rating_key}/children",
        type="collection",
        subtype="movie",
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


def _meta_xml(state: FakePlexState, total: int) -> Element:
    """Filter metadata plexapi loads before validating any sort= argument."""
    root = _container(size=0, totalSize=total)
    meta = SubElement(root, "Meta")
    movie_type = _el(meta, "Type", key=f"/library/sections/{state.section_id}/all?type=1", type="movie", title="Movies")
    for key, direction, title in (
        ("addedAt", "asc", "Date Added"),
        ("audienceRating", "desc", "Audience Rating"),
        ("titleSort", "asc", "Title"),
    ):
        _el(movie_type, "Sort", key=key, defaultDirection=direction, title=title)
    collection_type = _el(
        meta, "Type", key=f"/library/sections/{state.section_id}/all?type=18", type="collection", title="Collections"
    )
    _el(collection_type, "Sort", key="titleSort", defaultDirection="asc", title="Title")
    return root


def _sorted_movies(state: FakePlexState, sort: str | None) -> list[FakeMovie]:
    movies = list(state.movies.values())
    if not sort:
        return sorted(movies, key=lambda m: m.rating_key)
    fieldname, _, direction = sort.split(",")[0].rsplit(".", 1)[-1].partition(":")  # 'movie.addedAt:asc' -> addedAt
    return sorted(movies, key=_SORT_KEYS.get(fieldname, lambda m: m.rating_key), reverse=direction == "desc")


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
        root = _container(size=1, allowSync="0", title1="Plex Library")
        _el(root, "Directory", key=state.section_id, type="movie", title="Movies", uuid="section-uuid-1", filters="1")
        return _xml(root)

    @app.get("/library/sections/{section_id}/all")
    @app.get("/library/sections/{section_id}/collections")
    def section_all(section_id: int, request: Request) -> Response:
        query = request.query_params
        if query.get("includeMeta") == "1":
            return _xml(_meta_xml(state, len(state.movies)))
        if query.get("type") == "18" or request.url.path.endswith("/collections"):
            owned = [c for c in state.collections.values() if c.section_id == section_id]
            root = _container(size=len(owned), totalSize=len(owned), librarySectionID=section_id)
            for collection in owned:
                _collection_xml(root, state, collection)
            return _xml(root)
        movies = _sorted_movies(state, query.get("sort"))
        if query.get("limit") is not None:
            movies = movies[: int(query["limit"])]
        start, size = _page(request, len(movies))
        page = movies[start : start + size]
        root = _container(size=len(page), totalSize=len(movies), librarySectionID=section_id)
        for movie in page:
            _movie_xml(root, state, movie)
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
        collection = FakeCollection(
            rating_key=state.new_rating_key(),
            title=query.get("title", ""),
            section_id=int(query.get("sectionId") or state.section_id),
            item_keys=[k for k in item_keys if k in state.movies],
        )
        state.collections[collection.rating_key] = collection
        root = _container(size=1)
        _collection_xml(root, state, collection)
        return _xml(root)

    @app.get("/library/metadata/{rating_key}/children")
    def collection_children(rating_key: int) -> Response:
        collection = _collection(rating_key)
        root = _container(size=len(collection.item_keys), totalSize=len(collection.item_keys))
        for key in collection.item_keys:
            _movie_xml(root, state, state.movies[key])
        return _xml(root)

    @app.put("/library/metadata/{rating_key}/items")
    def collection_add_items(rating_key: int, request: Request) -> Response:
        collection = _collection(rating_key)
        for key in request.query_params["uri"].rsplit("/library/metadata/", 1)[-1].split(","):
            if int(key) in state.movies and int(key) not in collection.item_keys:
                collection.item_keys.append(int(key))
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
        _collection(rating_key)
        del state.collections[rating_key]
        return Response(status_code=200)

    @app.get("/library/metadata/{rating_keys}")
    def metadata(rating_keys: str) -> Response:
        root = _container(librarySectionID=state.section_id)
        found = 0
        for raw in rating_keys.split(","):
            key = int(raw)
            if key in state.movies:
                _movie_xml(root, state, state.movies[key])
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
            if not promoted or {label.lower() for label in collection.labels} & excludes:
                continue
            children_key = f"/library/collections/{collection.rating_key}/children"
            hub_list.append(
                {
                    "key": children_key,
                    "hubKey": children_key,
                    "title": collection.title,
                    "type": "movie",
                    "hubIdentifier": f"custom.collection.{collection.rating_key}",
                    "promoted": True,
                }
            )
        return JSONResponse({"MediaContainer": {"size": len(hub_list), "Hub": hub_list}})

    @app.get("/status/sessions/history/all")
    def history(request: Request) -> Response:
        account_id = request.query_params.get("accountID")
        rows = [h for h in state.history if account_id is None or h.account_id == int(account_id)]
        rows.sort(key=lambda h: h.viewed_at, reverse=True)
        start, size = _page(request, len(rows))
        page = rows[start : start + size]
        root = _container(size=len(page), totalSize=len(rows))
        for i, row in enumerate(page):
            movie = state.movies[row.rating_key]
            _el(
                root,
                "Video",
                historyKey=f"/status/sessions/history/{start + i + 1}",
                key=f"/library/metadata/{movie.rating_key}",
                ratingKey=movie.rating_key,
                title=movie.title,
                type="movie",
                viewedAt=row.viewed_at,
                accountID=row.account_id,
            )
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

    @app.get("/api/v2/resources")
    def resources(request: Request) -> JSONResponse:
        token = request.headers.get("X-Plex-Token", "")
        if not token.startswith("switch-"):
            return JSONResponse([])
        account_id = token.removeprefix("switch-")
        server = {"name": state.friendly_name, "clientIdentifier": state.machine_id, "provides": "server"}
        return JSONResponse([{**server, "accessToken": f"server-{account_id}"}])

    return app
