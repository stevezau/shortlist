"""E2E harness: the real app (FastAPI + built SPA) against tests/fakes/fake_plex.py.

No real Plex server, no network. The app runs with a temp /config, its Plex settings point at
the fake, and Playwright drives a browser against it. Run with `pytest -m e2e`
(needs `playwright install chromium` once, and a built SPA: `pnpm -C web build`).

Two boundaries are faked so the suite never touches the network:
- PMS + plex.tv          -> tests/fakes/fake_plex.py (real HTTP on loopback)
- TMDB                   -> `_make_fake_tmdb` below (real HTTP on loopback)
The Plex PIN endpoints are stubbed in the BROWSER instead (`stub_plex_pin`), because that is
the one flow whose contract is "the SPA polls until plex.tv says linked".
"""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Browser, Page, sync_playwright

from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server, User
from shortlist.server.main import create_app
from shortlist.server.settings_store import SettingsStore
from tests.fakes.fake_plex import FakeHistoryEntry, FakePlexState, make_fake_plex, make_fake_plextv, seed_state
from tests.uvicorn_thread import UvicornThread

#: The built SPA these tests drive, and the sources it is built from.
_REPO = Path(__file__).resolve().parents[2]
_DIST = _REPO / "web" / "dist"
_SRC = _REPO / "web" / "src"


@pytest.fixture(scope="session", autouse=True)
def _refuse_a_stale_spa() -> None:
    """Fail loudly if `web/dist` is older than `web/src`.

    This suite drives the BUILT bundle and does not build it, so an unbuilt change is invisible: the
    tests pass, against the previous UI. That is not hypothetical — a dashboard redesign that broke
    six assertions in `test_watch_outcomes_e2e.py` reported a fully green e2e run, because `dist` was
    a day old and none of the new markup was in it. CI builds fresh and would have caught it; the
    local run said everything was fine.

    Compared by mtime rather than content: a hash would have to be stored somewhere, and "the build
    is older than the source" is the whole failure mode.

    A session fixture, not a module-level call: this conftest is COLLECTED by a plain `pytest` run
    even though the marker deselects everything in it, so checking at import time turned every
    ordinary test run into ten collection errors about a build it was never going to use.
    """
    if not _DIST.exists():
        pytest.fail("web/dist is missing — build the SPA first: pnpm -C web build", pytrace=False)
    newest_src = max((f.stat().st_mtime for f in _SRC.rglob("*") if f.is_file()), default=0)
    newest_dist = max((f.stat().st_mtime for f in _DIST.rglob("*") if f.is_file()), default=0)
    if newest_src > newest_dist:
        pytest.fail(
            "web/dist is older than web/src — these tests would run against the PREVIOUS UI and "
            "pass. Rebuild first: pnpm -C web build",
            pytrace=False,
        )


OWNER_ACCOUNT_ID = 555000001
PMS_VERSION = "1.43.3.10793"

# seed_state() gives sarah/mike 8 watches each — below EngineConfig.min_history (10), which would
# push every user down the cold-start path and never exercise seeds -> TMDB -> curator. Top both
# up to 12 distinct titles so the real recommendation path runs (and reasons say "Because you
# watched …"); jess keeps an empty history, which is exactly the cold-start case.
#
# Sarah watches movies AND TV, mike watches only TV: a suite where everyone watches movies can
# never catch a show being delivered into the movie library, which is the one leak that reached
# a live server. Rating keys 1xx are movies, 3xx are shows.
SARAH_WATCHED = [*range(101, 109), *range(301, 305)]
MIKE_WATCHED = [*range(305, 317)]
# The owner watches too — filed by PMS under its LOCAL account id, never their plex.tv one. Seeding
# this under `owner_account_id` would make the owner look like a cold-start user forever.
OWNER_WATCHED = [*range(109, 117), *range(313, 317)]


def _free_port(preferred: int | None = None) -> int:
    """A bindable localhost port, taking `preferred` when it happens to be free.

    Only the capture run asks for one. The wizard screenshot shows the address it actually probed,
    and an ephemeral port made the docs site advertise `http://127.0.0.1:58041` — which is not a
    number any Plex install has ever used, on the screen whose job is "this is what connecting
    looks like". 32400 is, and a Plex on the same box really is reachable there.
    """
    if preferred is not None:
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", preferred))
            except OSError:
                pass  # already taken — fall through to an ephemeral one rather than fail the run
            else:
                return preferred
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class ShortlistApp:
    url: str
    session_secret: str
    config_dir: Path
    pms_url: str = ""

    def plex_hubs_as(self, plex_account_id: int) -> list[dict]:
        """The Home hubs a given user actually sees — Plex's answer, not Shortlist's.

        The only way to prove a row is private is to look through the other user's eyes: the
        share filters can be perfectly correct while the row is still visible to everyone.
        """
        r = httpx.get(
            f"{self.pms_url}/hubs",
            headers={"X-Plex-Token": f"server-{plex_account_id}", "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["MediaContainer"]["Hub"]

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Call the real API as the owner, the way the SPA does (session cookie + CSRF header)."""
        cookie = session_serializer(self.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID, "username": "owner"})
        headers = {CSRF_HEADER: "1", **kwargs.pop("headers", {})}
        return httpx.request(
            method,
            f"{self.url}{path}",
            cookies={SESSION_COOKIE: cookie},
            headers=headers,
            timeout=kwargs.pop("timeout", 120),
            **kwargs,
        )

    def wait_for_setting(self, key: str, expected: object, timeout_s: float = 10) -> None:
        """Block until ``/api/settings`` reports ``key == expected``.

        The SPA autosaves, so a UI change is not in the database yet when the click returns. Wait on the
        stored value itself, not a fixed pause: a pause is too long on a quiet host and too short on a busy one.
        """
        self._wait_until(
            lambda: self.api("GET", "/api/settings").json().get(key), expected, f"setting {key}", timeout_s
        )

    def wait_for_row_field(self, slug: str, field: str, expected: object, timeout_s: float = 10) -> dict:
        """Block until the row ``slug`` reports ``field == expected``; returns that row."""

        def current() -> object:
            rows = {c["slug"]: c for c in self.api("GET", "/api/collections").json()}
            return rows.get(slug, {}).get(field)

        self._wait_until(current, expected, f"row {slug!r} field {field}", timeout_s)
        return {c["slug"]: c for c in self.api("GET", "/api/collections").json()}[slug]

    @staticmethod
    def _wait_until(read, expected: object, what: str, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        value = read()
        while value != expected and time.monotonic() < deadline:
            time.sleep(0.1)
            value = read()
        assert value == expected, f"{what} never became {expected!r} (last read {value!r})"

    def wait_for_run(self, run_id: int, timeout_s: float = 120) -> dict:
        """Block until a run reaches a terminal state (runs execute as background tasks)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            run = self.api("GET", f"/api/runs/{run_id}").json()
            if run["status"] in ("ok", "error", "aborted"):
                return run
            time.sleep(0.2)
        raise AssertionError(f"run {run_id} never reached a terminal state")


# --------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fake_plex() -> Iterator[tuple[str, str, FakePlexState]]:
    """Fake PMS + fake plex.tv, booted once for the session."""
    state = seed_state()
    # Plex's own port while capturing, so the wizard screenshot shows an address a reader
    # recognises. Ordinary runs stay ephemeral: parallel workers would otherwise all want 32400.
    pms = UvicornThread(make_fake_plex(state), _free_port(32400 if os.environ.get("SHOTS_DIR") else None))
    plextv = UvicornThread(make_fake_plextv(state), _free_port())
    pms.start()
    plextv.start()
    pms.wait_until_up("/identity")
    plextv.wait_until_up("/api/users")
    # The server picker asks plex.tv what addresses a server advertises, so the fake plex.tv
    # has to know where the fake PMS ended up listening.
    state.pms_url = f"http://127.0.0.1:{pms.port}"
    yield f"http://127.0.0.1:{pms.port}", f"http://127.0.0.1:{plextv.port}", state
    pms.stop()
    plextv.stop()


@dataclass(frozen=True)
class FakeTmdbTag:
    """A TMDB tag (keyword) the fake knows: its name, and the titles a discover by it lists.

    `*_in_library` are TMDB ids the fake PMS holds; `*_elsewhere` counts titles TMDB tags that no library
    here has, as most of a real tag's list is. They are what a season's count has to leave out.
    """

    name: str
    movies_in_library: tuple[int, ...] = ()
    shows_in_library: tuple[int, ...] = ()
    movies_elsewhere: int = 0
    shows_elsewhere: int = 0


#: Thanksgiving is the one tag of both Thanksgiving presets (#137). Its 31 films are two discover pages, and
#: the six the library holds (1994 on) sort after the 25 it doesn't (1951-1975) in the release order a season
#: reads, so they are all on page 2: a count that stopped at page 1 would find none of them.
THANKSGIVING_TAG = 4543
#: Made up: a second tag for the tag search to find beside it.
THANKSGIVING_DINNER_TAG = 990001
FAKE_TMDB_TAGS: dict[int, FakeTmdbTag] = {
    THANKSGIVING_TAG: FakeTmdbTag(
        "thanksgiving",
        movies_in_library=(9011, 9012, 9013, 9014, 9015, 9016),
        shows_in_library=(7003,),
        movies_elsewhere=25,
        shows_elsewhere=2,
    ),
    THANKSGIVING_DINNER_TAG: FakeTmdbTag("thanksgiving dinner", movies_elsewhere=3),
}
#: TMDB pages a list 20 at a time (tmdb_discover_paged.json: 676 results over 34 pages).
TMDB_PAGE_SIZE = 20


def _make_fake_tmdb(state: FakePlexState) -> FastAPI:
    """Suggestions = the next 10 catalog titles after the seed — deterministic, always in-library.

    Movie seeds suggest movies and TV seeds suggest shows, exactly as TMDB does. A movies-only
    fake would never produce a show pick, so it could never catch a show being delivered into a
    movie collection.
    """
    app = FastAPI()
    movies = sorted(state.movies.values(), key=lambda m: m.tmdb_id)
    shows = sorted(state.shows.values(), key=lambda m: m.tmdb_id)
    by_id = {"movie": {m.tmdb_id: m for m in movies}, "tv": {s.tmdb_id: s for s in shows}}

    def _listed(kind: str, tmdb_id: int, title: str, year: int) -> dict:
        """One title as a TMDB list serves it (tmdb_discover_paged.json), less fields nothing reads."""
        named = (
            ("title", "original_title", "release_date")
            if kind == "movie"
            else ("name", "original_name", "first_air_date")
        )
        return {
            "adult": False,
            "id": tmdb_id,
            named[0]: title,
            named[1]: title,
            named[2]: f"{year}-11-01",
            "genre_ids": [1],
            "original_language": "en",
            "poster_path": f"/poster-{tmdb_id}.jpg",
            "vote_average": 6.5,
            "vote_count": 100 + tmdb_id % 900,
        }

    def _tagged(kind: str, tag_id: int) -> list[dict]:
        tag = FAKE_TMDB_TAGS.get(tag_id)
        if tag is None:
            return []
        held = tag.movies_in_library if kind == "movie" else tag.shows_in_library
        elsewhere = tag.movies_elsewhere if kind == "movie" else tag.shows_elsewhere
        titles = [_listed(kind, tmdb_id, by_id[kind][tmdb_id].title, by_id[kind][tmdb_id].year) for tmdb_id in held]
        titles += [
            _listed(kind, tag_id * 1000 + n, f"{tag.name.title()} {kind} {n}", 1950 + n)
            for n in range(1, elsewhere + 1)
        ]
        return titles

    def _suggest(catalog: list, tmdb_id: int, key: str) -> dict:
        index = {item.tmdb_id: i for i, item in enumerate(catalog)}
        base = index.get(tmdb_id, 0)
        results = []
        for offset in range(1, 11):
            item = catalog[(base + offset) % len(catalog)]
            results.append(
                {
                    "id": item.tmdb_id,
                    key: item.title,
                    "vote_average": item.audience_rating,
                    "genre_ids": [1],
                    # Real TMDB list responses carry the poster path; the request inbox reads it from
                    # here rather than paying a detail call per title.
                    "poster_path": f"/poster-{item.tmdb_id}.jpg",
                    ("release_date" if key == "title" else "first_air_date"): f"{item.year}-06-01",
                }
            )
        return {"results": results}

    @app.get("/configuration")
    def configuration() -> dict:
        return {"images": {"base_url": "http://127.0.0.1/img"}}

    @app.get("/genre/movie/list")
    @app.get("/genre/tv/list")
    def genres() -> dict:
        return {"genres": [{"id": 1, "name": "Drama"}]}

    @app.get("/search/movie")
    @app.get("/search/tv")
    def search_title(request: Request, query: str = "") -> dict:
        """A title search over the fake library, by a piece of its name (`TmdbClient.search`)."""
        kind = "movie" if request.url.path.endswith("/movie") else "tv"
        needle = query.strip().casefold()
        found = [
            _listed(kind, item.tmdb_id, item.title, item.year)
            for item in by_id[kind].values()
            if needle and needle in item.title.casefold()
        ]
        return {"page": 1, "results": found, "total_pages": 1, "total_results": len(found)}

    @app.get("/search/keyword")
    def search_keyword(query: str = "") -> dict:
        found = [
            {"id": tag_id, "name": tag.name}
            for tag_id, tag in FAKE_TMDB_TAGS.items()
            if query.strip().casefold() in tag.name.casefold()
        ]
        return {"page": 1, "results": found, "total_pages": 1, "total_results": len(found)}

    @app.get("/discover/movie")
    @app.get("/discover/tv")
    def discover(request: Request) -> dict:
        """A tag list, paged as TMDB pages one and in the release order a season asks for.

        `|` between tag ids is OR (tmdb_discover_paged.json). Only tags are listed: a genre query is
        answered empty, which keeps every other e2e's taste-discover source as it was.
        """
        kind = request.url.path.rsplit("/", 1)[-1]
        params = request.query_params
        page = int(params.get("page") or 1)
        if page > 500:
            raise HTTPException(status_code=400, detail="page must be less than or equal to 500")
        titles: dict[int, dict] = {}
        for raw in (params.get("with_keywords") or "").split("|"):
            if raw.strip().isdigit():
                for title in _tagged(kind, int(raw)):
                    titles.setdefault(title["id"], title)
        dated = "release_date" if kind == "movie" else "first_air_date"
        listing = sorted(titles.values(), key=lambda title: (title[dated], title["id"]))
        start = (page - 1) * TMDB_PAGE_SIZE
        return {
            "page": page,
            "results": listing[start : start + TMDB_PAGE_SIZE],
            "total_pages": max(1, -(-len(listing) // TMDB_PAGE_SIZE)),
            "total_results": len(listing),
        }

    # Declared before the two-segment catch-all below, which would answer it with a suggestion list.
    @app.get("/movie/{tmdb_id}/keywords")
    def movie_keywords(tmdb_id: int) -> dict:
        """A film's tags, shaped as TMDB serves `/movie/{id}/keywords`: `keywords`, not `results`."""
        tags = [
            {"id": tag_id, "name": tag.name}
            for tag_id, tag in FAKE_TMDB_TAGS.items()
            if tmdb_id in tag.movies_in_library
        ]
        return {"id": tmdb_id, "keywords": tags}

    @app.get("/movie/{tmdb_id}/{endpoint}")
    def movie_suggestions(tmdb_id: int, endpoint: str) -> dict:
        return _suggest(movies, tmdb_id, "title")

    @app.get("/tv/{tmdb_id}/{endpoint}")
    def tv_suggestions(tmdb_id: int, endpoint: str) -> dict:
        return _suggest(shows, tmdb_id, "name")

    # The title detail endpoints — how a poster is recovered for a title a NON-TMDB source surfaced
    # (Trakt, the web search), which never carries one. Declared after the two-segment routes above so
    # those keep matching `/movie/123/similar`.
    #
    # A title the fake library holds also carries its name, date and votes, as a real detail payload does:
    # a season's hand pick is read from here (`TmdbClient.list_item`) and shows by that name in its sample.
    # No `genres`, so the genre-led sources of every other e2e stay as they were.
    @app.get("/movie/{tmdb_id}")
    @app.get("/tv/{tmdb_id}")
    def title_detail(tmdb_id: int, request: Request) -> dict:
        kind = request.url.path.split("/")[1]
        detail = {"id": tmdb_id, "poster_path": f"/poster-{tmdb_id}.jpg"}
        if (item := by_id[kind].get(tmdb_id)) is not None:
            detail = {**_listed(kind, tmdb_id, item.title, item.year), **detail}
            del detail["genre_ids"]  # a detail payload carries `genres`, never `genre_ids`
        return detail

    return app


@pytest.fixture(scope="session")
def fake_tmdb(fake_plex) -> Iterator[str]:
    _, _, state = fake_plex
    server = UvicornThread(_make_fake_tmdb(state), _free_port())
    server.start()
    server.wait_until_up("/configuration")
    yield f"http://127.0.0.1:{server.port}"
    server.stop()


@pytest.fixture(autouse=True)
def reset_fake_plex(fake_plex) -> Iterator[FakePlexState]:
    """Re-seed the (session-scoped, mutable) fake Plex state before every test.

    Runs create collections and rewrite share filters on the fake; without this, one test's
    writes would decide the next test's starting point.
    """
    _, _, state = fake_plex
    fresh = seed_state()
    state.collections.clear()
    # A library a test ADDED, dropped before the next one runs. Only the extras: sections 1 and 2 are
    # the objects `state.movies`/`state.shows` resolve through, so replacing them wholesale here
    # would make the two lines below clear and then re-fill the same dict from itself.
    for key in [key for key in state.sections if key not in fresh.sections]:
        del state.sections[key]
    state.movies.clear()
    state.movies.update(fresh.movies)
    state.shows.clear()
    state.shows.update(fresh.shows)
    state.users.clear()
    state.users.update(fresh.users)
    state.history.clear()
    # Every mutable collection on the state needs a line here, and this list is maintained BY HAND —
    # deliberately, because a blanket copy from `fresh` would clobber the fields the harness owns
    # (`pms_url`, set only once the fake server has a port). That makes it the kind of fixture a new
    # field silently escapes: `user_ratings` was added without one of these lines, so a test that
    # rated something left the rating in place for whatever ran next, and the test asserting "nobody
    # has rated anything" failed depending purely on execution order. It passed locally and failed in
    # CI. If you add a field to FakePlexState that a test can write, add its reset here.
    state.user_ratings.clear()
    # The same omission `user_ratings` was fixed for: a test calling `watch_episodes` left a show
    # part-watched for whatever ran next, which is an order-dependent failure that passes locally.
    state.partial_shows.clear()
    for account_id, keys in (
        (201, SARAH_WATCHED),
        (202, MIKE_WATCHED),
        (state.owner_pms_account_id, OWNER_WATCHED),
    ):
        for offset, rating_key in enumerate(keys):
            state.history.append(
                FakeHistoryEntry(account_id=account_id, rating_key=rating_key, viewed_at=1_752_000_000 + offset)
            )
    state.next_rating_key = 5000
    yield state


def _boot_app(config_dir: Path) -> tuple[FastAPI, UvicornThread]:
    fastapi_app = create_app(config_dir=config_dir)
    server = UvicornThread(fastapi_app, _free_port())
    server.start()
    server.wait_until_up("/api/system/health")
    # The PIN flow stashes the owner's Plex token server-side (it never goes to the browser);
    # the browser-level PIN stub can't do that, so stand in for it here.
    fastapi_app.state.pending_plex_tokens[OWNER_ACCOUNT_ID] = "owner-token"
    return fastapi_app, server


@pytest.fixture
def app(fake_plex, fake_tmdb, reset_fake_plex, tmp_path: Path, monkeypatch) -> Iterator[ShortlistApp]:
    """The real Shortlist app pointed at the fakes, with setup already completed."""
    pms_url, plextv_url, state = fake_plex
    monkeypatch.setattr("shortlist.engine.clients.plextv.PLEXTV", plextv_url)  # engine uses absolute plex.tv URLs
    monkeypatch.setattr("shortlist.server.auth.PLEXTV", plextv_url)  # the PIN flow has its own constant
    # The server picker and the capability probe each did `from ...plextv import PLEXTV`, binding the
    # name in their own module — so patching the engine module's attribute alone leaves them pointed
    # at the real plex.tv (a 401 listing servers, an HTTPStatusError probing plex_pass).
    monkeypatch.setattr("shortlist.server.api.setup.PLEXTV", plextv_url)
    monkeypatch.setattr("shortlist.server.services.setup_probe.PLEXTV", plextv_url)
    monkeypatch.setattr("shortlist.engine.clients.tmdb.API", fake_tmdb)

    fastapi_app, server = _boot_app(tmp_path)

    with fastapi_app.state.sessions() as session:
        store = SettingsStore(session, fastapi_app.state.secrets)
        store.set("plex.url", pms_url)
        store.set("plex.token", "owner-token")
        store.set("tmdb.apikey", "fake")
        store.set("setup.completed", True)
        session.add(
            Server(
                machine_id=state.machine_id,
                url=pms_url,
                token_enc=fastapi_app.state.secrets.encrypt("owner-token"),
                name="FakeServer",
                version=PMS_VERSION,
                owner_account_id=OWNER_ACCOUNT_ID,
                plex_pass=True,
                capabilities={},
            )
        )
        for user in state.users.values():
            session.add(
                User(
                    plex_account_id=user.id,
                    username=user.username,
                    slug=user.username.lower(),
                    user_type="managed" if user.home else "shared",
                    # BOTH flags, because the product's skip needs both (`privacy.py`, and
                    # `context_builder.enabled_profiles`) — plex.tv sets `restricted=1` for every Plex
                    # Home account, preset or not, and only the preset says which. A fixture that
                    # dropped either one built the profiled account a row like anyone else, so every
                    # e2e ran against a roster the product cannot actually produce.
                    restricted=user.home,
                    restriction_profile=user.restriction_profile,
                    enabled=True,
                )
            )
        session.commit()

    yield ShortlistApp(
        url=f"http://127.0.0.1:{server.port}",
        session_secret=fastapi_app.state.session_secret,
        config_dir=tmp_path,
        pms_url=state.pms_url,
    )
    server.stop()


@pytest.fixture
def fresh_app(fake_plex, fake_tmdb, reset_fake_plex, tmp_path: Path, monkeypatch) -> Iterator[ShortlistApp]:
    """A never-configured Shortlist: no server linked, no users, setup NOT completed.

    This is what a first boot looks like, so `/` bounces to `/setup` and the wizard is the
    only thing the owner can reach.
    """
    _, plextv_url, _ = fake_plex
    monkeypatch.setattr("shortlist.engine.clients.plextv.PLEXTV", plextv_url)
    # The auth module has its OWN plex.tv constant (the PIN flow) — without this the real sign-in
    # would reach for the internet, and the e2e would have to forge the session cookie instead of
    # letting the product mint it.
    monkeypatch.setattr("shortlist.server.auth.PLEXTV", plextv_url)
    # The server picker and capability probe each bind their own PLEXTV name
    # (`from ...plextv import PLEXTV`), so they must be redirected here too or the wizard's first
    # step reaches the real plex.tv (a 401 listing servers, an HTTPStatusError probing plex_pass).
    monkeypatch.setattr("shortlist.server.api.setup.PLEXTV", plextv_url)
    monkeypatch.setattr("shortlist.server.services.setup_probe.PLEXTV", plextv_url)
    monkeypatch.setattr("shortlist.engine.clients.tmdb.API", fake_tmdb)

    fastapi_app, server = _boot_app(tmp_path)
    yield ShortlistApp(
        url=f"http://127.0.0.1:{server.port}",
        session_secret=fastapi_app.state.session_secret,
        config_dir=tmp_path,
    )
    server.stop()


# --------------------------------------------------------------------------------------
# Browser
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


def _owner_page(browser: Browser, app: ShortlistApp) -> Page:
    cookie = session_serializer(app.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID, "username": "owner"})
    context = browser.new_context(base_url=app.url)
    context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": app.url}])
    page = context.new_page()
    # GH Actions runners are slow; the default 30s intermittently fires on cold-boot paths.
    page.set_default_timeout(60_000)
    return page


@pytest.fixture
def page(browser: Browser, app: ShortlistApp) -> Iterator[Page]:
    """A page carrying a valid owner session (the PIN popup flow is tested separately).

    Deliberately does NOT inject the CSRF header at the context level: the SPA must send it
    itself, and injecting it here would mask exactly the bug this layer exists to catch.
    """
    page = _owner_page(browser, app)
    yield page
    page.context.close()


@pytest.fixture
def fresh_page(browser: Browser, fresh_app: ShortlistApp) -> Iterator[Page]:
    """A brand-new install, opened by someone with NO session — the real first-boot experience.

    Injecting an owner session here would skip the only part of the wizard a new owner cannot
    avoid: connecting their Plex account. That connection is not a gate in front of setup, it is
    step 1 OF setup, and it is what claims the instance — so the wizard test has to walk it.
    """
    context = browser.new_context(base_url=fresh_app.url)
    page = context.new_page()
    yield page
    context.close()


def build_real_rows(app: ShortlistApp) -> dict:
    """Get an app that has actually written rows to Plex: run for real.

    Uses the API rather than the UI on purpose — tests that assert on Runs/Users/uninstall need
    rows to EXIST, and driving the wizard again to get them would test the wizard twice.
    """
    created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
    run = app.wait_for_run(created["run_id"])
    assert run["status"] == "ok", run
    return run


def stub_plex_pin(page: Page, app: ShortlistApp | None = None, *, username: str = "owner") -> None:
    """Block only the plex.tv POPUP — the human's trip to plex.tv/link.

    Shortlist's own PIN endpoints are NOT stubbed: they run for real against the fake plex.tv, which
    serves `/api/v2/pins` and `/api/v2/user`. That matters because those endpoints are what mint
    the session cookie. An earlier version of this fixture forged the cookie itself, which meant
    every e2e would still pass if `poll_pin` stopped setting one — the sign-in being tested was
    the fixture's, not the product's.

    `app` is accepted (and ignored) so callers need not care which mechanism is in play.
    """
    del app, username  # nothing left to fake on our side
    # Routed on the CONTEXT, not the page: the wizard opens a plex.tv popup, which is a separate
    # page — a page-scoped route would let it hit the real network.
    page.context.route("https://app.plex.tv/**", lambda route: route.fulfill(body="ok", content_type="text/html"))
