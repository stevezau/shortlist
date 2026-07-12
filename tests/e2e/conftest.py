"""E2E harness: the real app (FastAPI + built SPA) against tests/fakes/fake_plex.py.

No real Plex server, no network. The app runs with a temp /config, its Plex settings point at
the fake, and Playwright drives a browser against it. Run with `pytest -m e2e`
(needs `playwright install chromium` once, and a built SPA: `pnpm -C web build`).
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

pytest.importorskip("playwright.sync_api", reason="playwright is not installed")

from playwright.sync_api import Browser, Page, sync_playwright

from rowarr.server.auth import SESSION_COOKIE, session_serializer
from rowarr.server.db.models import Server, User
from rowarr.server.main import create_app
from rowarr.server.settings_store import SettingsStore
from tests.fakes.fake_plex import make_fake_plex, make_fake_plextv, seed_state

OWNER_ACCOUNT_ID = 555000001


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


@pytest.fixture(scope="session")
def fake_plex():
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


@pytest.fixture
def app(fake_plex, tmp_path: Path, monkeypatch) -> Iterator[RowarrApp]:
    """The real Rowarr app pointed at the fakes, with setup already completed."""
    pms_url, plextv_url, state = fake_plex
    monkeypatch.setattr("rowarr.engine.clients.plex.PLEXTV", plextv_url)  # engine uses absolute plex.tv URLs

    fastapi_app = create_app(config_dir=tmp_path)
    server = _ThreadedServer(fastapi_app, _free_port())
    server.start()
    server.wait_until_up("/api/system/health")

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
                version="1.43.3.10793",
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


@pytest.fixture(scope="session")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch()
        yield chromium
        chromium.close()


@pytest.fixture
def page(browser: Browser, app: RowarrApp) -> Iterator[Page]:
    """A page carrying a valid owner session (the PIN popup flow is tested separately).

    Deliberately does NOT inject the CSRF header at the context level: the SPA must send it
    itself, and injecting it here would mask exactly the bug this layer exists to catch.
    """
    cookie = session_serializer(app.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID, "username": "owner"})
    context = browser.new_context(base_url=app.url)
    context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": app.url}])
    page = context.new_page()
    yield page
    context.close()
