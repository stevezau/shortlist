"""The app served from a subpath, driven by a real browser.

The ASGI tests (`test_base_path_app.py`) prove the SERVER routes under a prefix. They cannot prove
the half that runs in the browser: that the rewritten shell's bundle actually loads, that React
Router's `basename` resolves a deep link, and that every request the SPA makes carries the prefix.
Get any of those wrong and the symptom is a blank page in a deployment no other test runs in —
which is precisely how this class of bug reaches someone's server.

No fake Plex here on purpose: an unconfigured app still serves the shell, mounts React, routes, and
calls the API, which is the whole surface under test. Adding a seeded server would test the wizard.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser

from shortlist.server.main import create_app
from tests.e2e.conftest import _free_port, _ThreadedServer

pytestmark = pytest.mark.e2e

BASE = "/shortlist"


@pytest.fixture
def prefixed_url(tmp_path, monkeypatch) -> Iterator[str]:
    """A real server that believes it lives at `/shortlist`, as a forwarding proxy would present it."""
    monkeypatch.setenv("APP_BASE_PATH", BASE)
    server = _ThreadedServer(create_app(config_dir=tmp_path), _free_port())
    server.start()
    server.wait_until_up(f"{BASE}/api/system/health")
    yield f"http://127.0.0.1:{server.port}"
    server.stop()


def _watched_page(browser: Browser, url: str):
    """A page that records everything that would show up as "it just doesn't work"."""
    page = browser.new_context().new_page()
    page.set_default_timeout(60_000)
    console_errors: list[str] = []
    failed: list[str] = []
    api_calls: list[str] = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
    page.on("requestfailed", lambda r: failed.append(r.url))
    page.on("request", lambda r: api_calls.append(r.url) if "/api/" in r.url else None)
    return page, console_errors, failed, api_calls


def test_the_app_boots_and_talks_to_itself_under_the_prefix(browser: Browser, prefixed_url: str) -> None:
    page, console_errors, failed, api_calls = _watched_page(browser, prefixed_url)
    page.goto(f"{prefixed_url}{BASE}/", wait_until="networkidle")

    # The shell alone renders an empty <div id="root">. Anything inside it means the bundle was
    # found at the rewritten URL, parsed, and mounted — the asset rewrite reaching a real browser.
    assert page.locator("#root > *").count() > 0, "React never mounted — the bundle did not load"

    # The prefix reached the SPA, not just the server.
    assert page.evaluate("window.__SHORTLIST_BASE_PATH__") == BASE

    # Every API call carries the prefix. A single unprefixed one is the run-log Download bug.
    assert api_calls, "the SPA made no API calls at all — it never got far enough to be a test"
    unprefixed = [url for url in api_calls if f"{BASE}/api/" not in url]
    assert unprefixed == [], f"API calls bypassed the base path: {unprefixed}"

    assert failed == [], f"requests failed under the prefix: {failed}"
    assert console_errors == [], f"console errors under the prefix: {console_errors}"

    page.context.close()


def test_a_deep_link_survives_a_reload(browser: Browser, prefixed_url: str) -> None:
    """`basename` has to strip the prefix before matching, and put it back when navigating.

    Loading a nested route directly is the case a wrong `basename` breaks: the router sees a path
    it cannot match and renders nothing, so the page is blank with a 200 and no console error.
    """
    page, console_errors, failed, _ = _watched_page(browser, prefixed_url)
    page.goto(f"{prefixed_url}{BASE}/settings", wait_until="networkidle")

    assert page.locator("#root > *").count() > 0, "the router matched nothing under the prefix"
    # Wherever the app decided to send an unconfigured visitor, it must stay inside the prefix.
    assert page.url.startswith(f"{prefixed_url}{BASE}/"), f"navigated out of the base path: {page.url}"
    assert failed == []
    assert console_errors == []

    page.context.close()
