"""Capture README/marketing screenshots of the real UI against the fake-Plex harness.

Skipped in CI (writes only when SHOTS_DIR is set). Regenerate with:
    SHOTS_DIR=docs/images .venv/bin/python -m pytest tests/e2e/test_screenshots.py -m e2e --no-cov -n0
Fake data: the users are sarah/mike/jess and nobody real watched anything. The library names real
films and shows so the screens look like what an owner would actually see — see `DEMO_MOVIES` in
`tests/fakes/fake_plex.py`, and `scripts/fetch_demo_posters.py` for the cover art.

Shots are captured at 2x device scale: the docs site renders them at half their pixel width, so a
1x capture looks soft on every laptop sold in the last decade.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Browser, Page, expect

from shortlist.server.auth import SESSION_COOKIE, session_serializer
from tests.e2e.conftest import OWNER_ACCOUNT_ID, ShortlistApp, build_real_rows, stub_plex_pin
from tests.fakes.fake_plex import FakePlexState

pytestmark = pytest.mark.e2e

SHOTS_DIR = os.environ.get("SHOTS_DIR")
LOAD = 20_000
#: The server picker probes every address Plex advertises, including one that is deliberately
#: unreachable — proving that means waiting out a connection timeout, not a page load.
PROBE = 90_000
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
    """Write one capture as WebP.

    These are UI text over real cover art, and each format is bad at one half of that: PNG stores
    the posters losslessly and costs 4x, JPEG rings around the small type. WebP is better at both —
    `user-detail` measures 828KB as a PNG, 387KB as a JPEG and 187KB here. The tour on the home page
    loads four of these, so it is the difference between a 2.2MB landing page and a 600KB one.

    Playwright only writes PNG or JPEG, so the encode goes through Pillow.
    """
    out = Path(SHOTS_DIR) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.open(io.BytesIO(page.screenshot())).convert("RGB").save(out, "WEBP", quality=88, method=6)


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


#: Three more row DEFINITIONS for rows.png, copied verbatim from the production templates in
#: web/src/lib/row-templates.ts (`because-you-watched`, `seen-it-already`, `popular-here`) so the
#: shot shows templates people can actually pick, not an invented fixture shape. The shared one is
#: there for its badge, not its picks: this page lists row DEFINITIONS and nothing runs after they
#: are created — which matters, because sarah's and mike's watch sets are disjoint by fixture
#: design, so no title in this library would clear that row's `min_watchers`.
EXTRA_ROWS = (
    {
        "name": "🎯 Because you watched {top_seed}",
        "build": "per_person",
        "max_seeds": 1,
        "recent_count": 3,
        "media": "movie",
        "size": 20,
        "refresh_days": 1,
        "seed_window": 1,
    },
    {
        "name": "☕ {library_name} you've already seen",
        "build": "per_person",
        "rewatch": True,
        "watched_pct": 1,
        "refresh_days": 11,
        "size": 15,
        # Two of the four rows carry a poster, and two deliberately do not. Every row on this page
        # used to show the same empty dashed placeholder, which reads as a broken page rather than
        # as a feature nobody has switched on — and it hid the row-poster feature entirely. Showing
        # both states says which it is.
        "poster": {"mode": "text", "title": "Seen it?", "subtitle": "Worth another look"},
    },
    {
        "name": "👥 Popular {library_name} on this server",
        "build": "shared",
        "min_watchers": 3,
        "size": 20,
        "poster": {"mode": "text", "title": "Popular here", "subtitle": "What everyone is watching"},
    },
)


@skip_unless_capturing
def test_capture_app_screenshots(shot_page: Page, app: ShortlistApp) -> None:
    build_real_rows(app)  # a real run against the fake server, so the pages have rows/picks/history

    sarah = _users_by_name(app)["sarah"]["id"]
    run_id = app.api("GET", "/api/runs").json()[0]["id"]

    _capture(shot_page, "/", "dashboard.webp", wait="picked|watched|run")
    _capture(shot_page, f"/users/{sarah}", "user-detail.webp", wait="Because you watched")
    _capture(shot_page, "/users", "users.webp", wait="sarah")
    _capture(shot_page, "/runs", "runs.webp", wait="succeeded|ok")
    _capture(shot_page, f"/runs/{run_id}", "run-detail.webp", wait="AI tokens")
    _capture(shot_page, "/requests", "requests.webp", wait="request")
    _capture(shot_page, "/settings", "settings.webp", wait="Connections")

    # rows.png needs row VARIETY, and the seeded install has exactly one row, so it came out as one
    # card in an empty frame. The extra rows go in HERE rather than in `build_real_rows`, which is
    # shared with four other e2e files — three of them assert an exact collection count that a
    # second row per user would break. Captured last so every shot above still sees the same
    # single-row state it did before, and no committed image moves for a reason unrelated to it.
    # No second run needed: the Rows list renders `collections` rows, not delivered picks.
    for payload in EXTRA_ROWS:
        created = app.api("POST", "/api/collections", json=payload)
        assert created.status_code == 201, created.text
    _capture(shot_page, "/rows", "rows.webp", wait="Picked for You")


@skip_unless_capturing
def test_capture_wizard_screenshot(fresh_shot_page: Page, fresh_app: ShortlistApp, fake_plex) -> None:
    """Two shots of the wizard: the welcome step, and the capability checklist on step 2.

    getting-started.md walks seven steps with no picture at all. Welcome alone shows the product
    exists; the Connect Plex checklist shows it verifying the reader's own server (version, Plex
    Pass, libraries) before they commit to anything, which is the part worth seeing in advance.
    Both come out of one browser context, so the second costs a few seconds and no extra fixture.
    """
    pms_url, _, _ = fake_plex
    page = fresh_shot_page
    stub_plex_pin(page, fresh_app)

    page.goto("/setup")
    expect(page.get_by_role("heading", name="Welcome")).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(1000)
    _fit_viewport(page)
    _shot(page, "wizard.webp")

    # The same sequence test_wizard_e2e.py::_connect_plex asserts, minus its assertions: if the
    # wizard's labels ever change, that test fails first and says so, and this capture breaks with
    # it for the same reason.
    page.get_by_role("button", name="Get started").click()
    expect(page.get_by_role("heading", name="Connect Plex")).to_be_visible()
    page.get_by_role("button", name="Sign in with Plex").click()
    expect(page.get_by_role("button", name="Sign in with Plex")).to_have_count(0, timeout=LOAD)
    expect(page.get_by_text(FakePlexState.friendly_name, exact=True).first).to_be_visible(timeout=PROBE)
    expect(page.locator("button", has_text=pms_url).first).to_be_enabled(timeout=LOAD)
    page.get_by_role("button", name="Run checks").click()
    expect(page.get_by_text("Plex Pass active")).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(500)
    _fit_viewport(page)
    _shot(page, "wizard-connect.webp")
