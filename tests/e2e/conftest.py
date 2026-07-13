"""E2E harness: the real app (FastAPI + built SPA) against tests/fakes/fake_plex.py.

No real Plex server, no network. The app runs with a temp /config, its Plex settings point at
the fake, and Playwright drives a browser against it. Run with `pytest -m e2e`
(needs `playwright install chromium` once, and a built SPA: `pnpm -C web build`).

Three boundaries are faked so the suite never touches the network:
- PMS + plex.tv          -> tests/fakes/fake_plex.py (real HTTP on loopback)
- TMDB                   -> `_make_fake_tmdb` below (real HTTP on loopback)
The Plex PIN endpoints are stubbed in the BROWSER instead (`stub_plex_pin`), because that is
the one flow whose contract is "the SPA polls until plex.tv says linked".
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Browser, Page, sync_playwright

from rowarr.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from rowarr.server.db.models import Server, User
from rowarr.server.main import create_app
from rowarr.server.settings_store import SettingsStore
from tests.fakes.fake_plex import FakeHistoryEntry, FakePlexState, make_fake_plex, make_fake_plextv, seed_state

OWNER_ACCOUNT_ID = 555000001
PMS_VERSION = "1.43.3.10793"

# seed_state() gives sarah/mike 8 watches each — below EngineConfig.min_history (10), which would
# push every user down the cold-start path and never exercise seeds -> TMDB -> curator. Top both
# up to 12 distinct titles so the real recommendation path runs (and reasons say "Because you
# watched …"); the canary keeps its empty history, which is exactly the cold-start case.
#
# Sarah watches movies AND TV, mike watches only TV: a suite where everyone watches movies can
# never catch a show being delivered into the movie library, which is the one leak that reached
# a live server. Rating keys 1xx are movies, 3xx are shows.
SARAH_WATCHED = [*range(101, 109), *range(301, 305)]
MIKE_WATCHED = [*range(305, 317)]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _ThreadedServer(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        self.port = port

    def run(self) -> None:
        self._server.run()

    def wait_until_up(self, path: str, timeout_s: float = 20) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                httpx.get(f"http://127.0.0.1:{self.port}{path}", timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError(f"server on port {self.port} never came up")

    def stop(self) -> None:
        self._server.should_exit = True
        self.join(timeout=5)


@dataclass
class RowarrApp:
    url: str
    session_secret: str
    config_dir: Path
    pms_url: str = ""

    def plex_hubs_as(self, plex_account_id: int) -> list[dict]:
        """The Home hubs a given user actually sees — Plex's answer, not Rowarr's.

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
    pms = _ThreadedServer(make_fake_plex(state), _free_port())
    plextv = _ThreadedServer(make_fake_plextv(state), _free_port())
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


def _make_fake_tmdb(state: FakePlexState) -> FastAPI:
    """Suggestions = the next 10 catalog titles after the seed — deterministic, always in-library.

    Movie seeds suggest movies and TV seeds suggest shows, exactly as TMDB does. A movies-only
    fake would never produce a show pick, so it could never catch a show being delivered into a
    movie collection.
    """
    app = FastAPI()
    movies = sorted(state.movies.values(), key=lambda m: m.tmdb_id)
    shows = sorted(state.shows.values(), key=lambda m: m.tmdb_id)

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

    @app.get("/movie/{tmdb_id}/{endpoint}")
    def movie_suggestions(tmdb_id: int, endpoint: str) -> dict:
        return _suggest(movies, tmdb_id, "title")

    @app.get("/tv/{tmdb_id}/{endpoint}")
    def tv_suggestions(tmdb_id: int, endpoint: str) -> dict:
        return _suggest(shows, tmdb_id, "name")

    return app


@pytest.fixture(scope="session")
def fake_tmdb(fake_plex) -> Iterator[str]:
    _, _, state = fake_plex
    server = _ThreadedServer(_make_fake_tmdb(state), _free_port())
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
    state.movies.clear()
    state.movies.update(fresh.movies)
    state.shows.clear()
    state.shows.update(fresh.shows)
    state.users.clear()
    state.users.update(fresh.users)
    state.history.clear()
    for account_id, keys in ((201, SARAH_WATCHED), (202, MIKE_WATCHED)):
        for offset, rating_key in enumerate(keys):
            state.history.append(
                FakeHistoryEntry(account_id=account_id, rating_key=rating_key, viewed_at=1_752_000_000 + offset)
            )
    state.next_rating_key = 5000
    yield state


def _boot_app(config_dir: Path) -> tuple[FastAPI, _ThreadedServer]:
    fastapi_app = create_app(config_dir=config_dir)
    server = _ThreadedServer(fastapi_app, _free_port())
    server.start()
    server.wait_until_up("/api/system/health")
    # The PIN flow stashes the owner's Plex token server-side (it never goes to the browser);
    # the browser-level PIN stub can't do that, so stand in for it here.
    fastapi_app.state.pending_plex_tokens[OWNER_ACCOUNT_ID] = "owner-token"
    return fastapi_app, server


@pytest.fixture
def app(fake_plex, fake_tmdb, reset_fake_plex, tmp_path: Path, monkeypatch) -> Iterator[RowarrApp]:
    """The real Rowarr app pointed at the fakes, with setup already completed."""
    pms_url, plextv_url, state = fake_plex
    monkeypatch.setattr("rowarr.engine.clients.plextv.PLEXTV", plextv_url)  # engine uses absolute plex.tv URLs
    monkeypatch.setattr("rowarr.server.auth.PLEXTV", plextv_url)  # the PIN flow has its own constant
    monkeypatch.setattr("rowarr.engine.clients.tmdb.API", fake_tmdb)

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
                    enabled=True,
                )
            )
        session.commit()

    yield RowarrApp(
        url=f"http://127.0.0.1:{server.port}",
        session_secret=fastapi_app.state.session_secret,
        config_dir=tmp_path,
        pms_url=state.pms_url,
    )
    server.stop()


@pytest.fixture
def fresh_app(fake_plex, fake_tmdb, reset_fake_plex, tmp_path: Path, monkeypatch) -> Iterator[RowarrApp]:
    """A never-configured Rowarr: no server linked, no users, setup NOT completed.

    This is what a first boot looks like, so `/` bounces to `/setup` and the wizard is the
    only thing the owner can reach.
    """
    _, plextv_url, _ = fake_plex
    monkeypatch.setattr("rowarr.engine.clients.plextv.PLEXTV", plextv_url)
    # The auth module has its OWN plex.tv constant (the PIN flow) — without this the real sign-in
    # would reach for the internet, and the e2e would have to forge the session cookie instead of
    # letting the product mint it.
    monkeypatch.setattr("rowarr.server.auth.PLEXTV", plextv_url)
    monkeypatch.setattr("rowarr.engine.clients.tmdb.API", fake_tmdb)

    fastapi_app, server = _boot_app(tmp_path)
    yield RowarrApp(
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


def _owner_page(browser: Browser, app: RowarrApp) -> Page:
    cookie = session_serializer(app.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID, "username": "owner"})
    context = browser.new_context(base_url=app.url)
    context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": app.url}])
    return context.new_page()


@pytest.fixture
def page(browser: Browser, app: RowarrApp) -> Iterator[Page]:
    """A page carrying a valid owner session (the PIN popup flow is tested separately).

    Deliberately does NOT inject the CSRF header at the context level: the SPA must send it
    itself, and injecting it here would mask exactly the bug this layer exists to catch.
    """
    page = _owner_page(browser, app)
    yield page
    page.context.close()


@pytest.fixture
def fresh_page(browser: Browser, fresh_app: RowarrApp) -> Iterator[Page]:
    """A brand-new install, opened by someone with NO session — the real first-boot experience.

    Injecting an owner session here would skip the only part of the wizard a new owner cannot
    avoid: connecting their Plex account. That connection is not a gate in front of setup, it is
    step 1 OF setup, and it is what claims the instance — so the wizard test has to walk it.
    """
    context = browser.new_context(base_url=fresh_app.url)
    page = context.new_page()
    yield page
    context.close()


def build_real_rows(app: RowarrApp) -> dict:
    """Get an app that has actually written rows to Plex: pass the privacy gate, then run for real.

    Uses the API rather than the UI on purpose — tests that assert on Runs/Users/uninstall need
    rows to EXIST, and driving the wizard again to get them would test the wizard twice.
    """
    check = app.api("POST", "/api/privacy/check", json={}).json()
    assert check["passed"], f"the read-only privacy check must pass against the fake: {check}"
    created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
    run = app.wait_for_run(created["run_id"])
    assert run["status"] == "ok", run
    return run


def stub_plex_pin(page: Page, app: RowarrApp | None = None, *, username: str = "owner") -> None:
    """Block only the plex.tv POPUP — the human's trip to plex.tv/link.

    Rowarr's own PIN endpoints are NOT stubbed: they run for real against the fake plex.tv, which
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
