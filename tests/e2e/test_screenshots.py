"""Capture README/marketing screenshots of the real UI against the fake-Plex harness.

Skipped in CI (writes only when SHOTS_DIR is set). Regenerate with:
    SHOTS_DIR=docs/images .venv/bin/python -m pytest tests/e2e/test_screenshots.py -m e2e --no-cov -n0
Fake data (users sarah/mike/canary, placeholder titles) — no real people, safe for a public repo.

Shots are captured at 2x device scale: the docs site renders them at half their pixel width, so a
1x capture looks soft on every laptop sold in the last decade.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from shortlist.server.auth import SESSION_COOKIE, session_serializer
from tests.e2e.conftest import OWNER_ACCOUNT_ID, ShortlistApp, build_real_rows

pytestmark = pytest.mark.e2e

SHOTS_DIR = os.environ.get("SHOTS_DIR")
LOAD = 20_000
VIEWPORT = {"width": 1440, "height": 950}
skip_unless_capturing = pytest.mark.skipif(not SHOTS_DIR, reason="set SHOTS_DIR to capture screenshots")


def _retina_page(browser: Browser, app: ShortlistApp, *, authenticated: bool = True) -> Iterator[Page]:
    context = browser.new_context(base_url=app.url, viewport=VIEWPORT, device_scale_factor=2)
    if authenticated:
        cookie = session_serializer(app.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID, "username": "owner"})
        context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": app.url}])
    page = context.new_page()
    page.set_default_timeout(60_000)
    yield page
    context.close()


@pytest.fixture
def shot_page(browser: Browser, app: ShortlistApp) -> Iterator[Page]:
    yield from _retina_page(browser, app)


@pytest.fixture
def fresh_shot_page(browser: Browser, fresh_app: ShortlistApp) -> Iterator[Page]:
    yield from _retina_page(browser, fresh_app, authenticated=False)


def _shot(page: Page, name: str) -> None:
    out = Path(SHOTS_DIR) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))


def _users_by_name(app: ShortlistApp) -> dict:
    return {u["username"].lower(): u for u in app.api("GET", "/api/users").json()}


def _fit_viewport(page: Page) -> None:
    """Shrink the viewport to the height of the page's own content.

    The fake harness has three users and one row, so most screens fill well under the 950px design
    height. Captured at a fixed height they come out 40% empty, which reads as an empty *product*
    rather than a small test fixture. Measuring `<main>` and cropping to it fixes that for every
    page at once, instead of hand-tuning a height per shot.
    """
    # `main` is flex-1, so its own height is always the viewport's — measuring it just returns what
    # we started with. The lowest leaf element that actually renders text is the real content edge;
    # the padding allowance covers the card border and padding those leaves sit inside.
    measured = page.evaluate(
        """() => {
            const main = document.querySelector('main');
            if (!main) return null;
            let bottom = 0;
            for (const el of main.querySelectorAll('*')) {
                if (el.childElementCount || !el.textContent.trim()) continue;
                const r = el.getBoundingClientRect();
                if (r.height > 0) bottom = Math.max(bottom, r.bottom);
            }
            // A sibling of <main> is the nav rail; the wizard renders <main> on its own.
            return bottom ? {height: Math.ceil(bottom + 48), hasRail: main.parentElement.children.length > 1} : null;
        }"""
    )
    if measured:
        # The nav rail pins its account block to the bottom with mt-auto, so below roughly 860px the
        # nav items collide with it and the shot looks like a broken app. Sparse pages therefore keep
        # some empty space on the right — better than a mangled sidebar. Pages with no rail (the
        # wizard) have nothing to collide and can crop as tightly as their content allows.
        floor = 860 if measured["hasRail"] else 560
        page.set_viewport_size({"width": VIEWPORT["width"], "height": max(floor, min(int(measured["height"]), 1400))})
        page.wait_for_timeout(500)


def _capture(page: Page, path: str, name: str, *, wait: str | None = None) -> None:
    page.set_viewport_size(VIEWPORT)  # reset: the previous shot may have resized to fit its content
    page.goto(path)  # no networkidle: the app holds an SSE stream open, so it never goes idle
    if wait is not None:
        # Best-effort: capture whatever rendered; this is a screenshot tool, not a correctness test.
        with contextlib.suppress(Exception):
            expect(page.get_by_text(re.compile(wait, re.I)).first).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(1200)
    _fit_viewport(page)
    _shot(page, name)


@skip_unless_capturing
def test_capture_app_screenshots(shot_page: Page, app: ShortlistApp) -> None:
    build_real_rows(app)  # a real run against the fake server, so the pages have rows/picks/history

    sarah = _users_by_name(app)["sarah"]["id"]
    run_id = app.api("GET", "/api/runs").json()[0]["id"]

    _capture(shot_page, "/", "dashboard.png", wait="picked|watched|run")
    _capture(shot_page, f"/users/{sarah}", "user-detail.png", wait="Because you watched")
    _capture(shot_page, "/users", "users.png", wait="sarah")
    _capture(shot_page, "/rows", "rows.png", wait="Picked for You")
    _capture(shot_page, "/runs", "runs.png", wait="succeeded|ok")
    _capture(shot_page, f"/runs/{run_id}", "run-detail.png", wait="AI tokens")
    _capture(shot_page, "/requests", "requests.png", wait="request")
    _capture(shot_page, "/settings", "settings.png", wait="Connections")


@skip_unless_capturing
def test_capture_wizard_screenshot(fresh_shot_page: Page, fresh_app: ShortlistApp) -> None:
    fresh_shot_page.goto("/setup")
    fresh_shot_page.wait_for_timeout(1000)
    _fit_viewport(fresh_shot_page)
    _shot(fresh_shot_page, "wizard.png")
