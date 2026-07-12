"""E2E harness: the real app (FastAPI + built SPA) against tests/fakes/fake_plex.py.

No real Plex server, no network. The app runs with a temp /config, its Plex settings point at
the fake, and Playwright drives a browser against it. Run with `pytest -m e2e`
(needs `playwright install chromium` once, and a built SPA: `pnpm -C web build`).

Three boundaries are faked so the suite never touches the network:
- PMS + plex.tv          -> tests/fakes/fake_plex.py (real HTTP on loopback)
- TMDB                   -> `_make_fake_tmdb` below (real HTTP on loopback)
- plex.tv /api/v2/user   -> `_stub_plextv_account` (httpx patch; the setup probe hardcodes the
                            absolute plex.tv URL, so there is no constant to repoint)
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

from playwright.sync_api import Browser, Page, Route, sync_playwright

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
SARAH_WATCHED = range(101, 113)
MIKE_WATCHED = range(113, 125)


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
    yield f"http://127.0.0.1:{pms.port}", f"http://127.0.0.1:{plextv.port}", state
    pms.stop()
    plextv.stop()


def _make_fake_tmdb(state: FakePlexState) -> FastAPI:
    """Suggestions = the next 10 catalog titles after the seed — deterministic, always in-library."""
    app = FastAPI()
    catalog = sorted(state.movies.values(), key=lambda m: m.tmdb_id)
    index = {movie.tmdb_id: i for i, movie in enumerate(catalog)}

    @app.get("/configuration")
    def configuration() -> dict:
        return {"images": {"base_url": "http://127.0.0.1/img"}}

    @app.get("/genre/movie/list")
    @app.get("/genre/tv/list")
    def genres() -> dict:
        return {"genres": [{"id": 1, "name": "Drama"}]}

    @app.get("/movie/{tmdb_id}/{endpoint}")
    def suggestions(tmdb_id: int, endpoint: str) -> dict:
        base = index.get(tmdb_id, 0)
        results = [
            {
                "id": catalog[(base + offset) % len(catalog)].tmdb_id,
                "title": catalog[(base + offset) % len(catalog)].title,
                "vote_average": catalog[(base + offset) % len(catalog)].audience_rating,
                "genre_ids": [1],
                "release_date": f"{catalog[(base + offset) % len(catalog)].year}-06-01",
            }
            for offset in range(1, 11)
        ]
        return {"results": results}

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


@pytest.fixture(autouse=True)
def stub_plextv_account(monkeypatch) -> None:
    """Answer the ONE plex.tv URL the backend hardcodes (`/api/setup/probe` -> /api/v2/user).

    Everything else — the fake PMS, fake plex.tv, fake TMDB — passes straight through to the
    real transport, so this patch cannot hide a wrong URL anywhere else.
    """
    real_get = httpx.get

    def get(url, **kwargs) -> httpx.Response:
        if str(url).startswith("https://plex.tv/api/v2/user"):
            return httpx.Response(
                200,
                json={
                    "id": OWNER_ACCOUNT_ID,
                    "uuid": "owner-uuid",
                    "username": "owner",
                    "title": "owner",
                    "subscription": {"active": True},
                },
                request=httpx.Request("GET", str(url)),
            )
        return real_get(url, **kwargs)

    monkeypatch.setattr(httpx, "get", get)


# --------------------------------------------------------------------------------------
# The app under test
# --------------------------------------------------------------------------------------


def _boot_app(config_dir: Path) -> tuple[FastAPI, _ThreadedServer]:
    fastapi_app = create_app(config_dir=config_dir)
    server = _ThreadedServer(fastapi_app, _free_port())
    server.start()
    server.wait_until_up("/api/system/health")
    return fastapi_app, server


@pytest.fixture
def app(fake_plex, fake_tmdb, reset_fake_plex, tmp_path: Path, monkeypatch) -> Iterator[RowarrApp]:
    """The real Rowarr app pointed at the fakes, with setup already completed."""
    pms_url, plextv_url, state = fake_plex
    monkeypatch.setattr("rowarr.engine.clients.plex.PLEXTV", plextv_url)  # engine uses absolute plex.tv URLs
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
    )
    server.stop()


@pytest.fixture
def fresh_app(fake_plex, fake_tmdb, reset_fake_plex, tmp_path: Path, monkeypatch) -> Iterator[RowarrApp]:
    """A never-configured Rowarr: no server linked, no users, setup NOT completed.

    This is what a first boot looks like, so `/` bounces to `/setup` and the wizard is the
    only thing the owner can reach.
    """
    _, plextv_url, _ = fake_plex
    monkeypatch.setattr("rowarr.engine.clients.plex.PLEXTV", plextv_url)
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
    """An owner session against a never-configured app — lands on the wizard."""
    page = _owner_page(browser, fresh_app)
    yield page
    page.context.close()


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


def stub_plex_pin(page: Page, *, token: str = "owner-token", username: str = "owner") -> None:
    """Fake the two PIN endpoints in the browser: create -> already-linked with a token.

    Only the plex.tv round-trip is faked. Everything the wizard does with the token afterwards
    (/api/setup/probe, /api/setup/link) hits the real backend against the fake Plex.
    """

    def create(route: Route) -> None:
        route.fulfill(json={"id": 1234, "code": "ABCD", "client_id": "rowarr-e2e"})

    def poll(route: Route) -> None:
        route.fulfill(json={"linked": True, "account_id": OWNER_ACCOUNT_ID, "username": username, "token": token})

    # Routed on the CONTEXT, not the page: the wizard opens a plex.tv popup, which is a
    # separate page — a page-scoped route would let it hit the real network.
    page.context.route("**/api/auth/pin", create)
    page.context.route("**/api/auth/pin/*", poll)
    page.context.route("https://app.plex.tv/**", lambda route: route.fulfill(body="ok", content_type="text/html"))
