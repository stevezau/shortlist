"""Capture README/marketing screenshots of the real UI against the fake-Plex harness.

Skipped in CI (writes only when SHOTS_DIR is set). Regenerate with:
    SHOTS_DIR=docs/images .venv/bin/python -m pytest tests/e2e/test_screenshots.py -m e2e --no-cov -n0
Fake data (users sarah/mike/canary, placeholder titles) — no real people, safe for a public repo.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp, build_real_rows

pytestmark = pytest.mark.e2e

SHOTS_DIR = os.environ.get("SHOTS_DIR")
LOAD = 20_000
skip_unless_capturing = pytest.mark.skipif(not SHOTS_DIR, reason="set SHOTS_DIR to capture screenshots")


def _shot(page: Page, name: str) -> None:
    out = Path(SHOTS_DIR) / name
    out.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out))


def _users_by_name(app: ShortlistApp) -> dict:
    return {u["username"].lower(): u for u in app.api("GET", "/api/users").json()}


def _capture(page: Page, path: str, name: str, *, wait: str | None = None) -> None:
    page.goto(path)  # no networkidle: the app holds an SSE stream open, so it never goes idle
    if wait is not None:
        # Best-effort: capture whatever rendered; this is a screenshot tool, not a correctness test.
        with contextlib.suppress(Exception):
            expect(page.get_by_text(re.compile(wait, re.I)).first).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(1200)
    _shot(page, name)


@skip_unless_capturing
def test_capture_app_screenshots(page: Page, app: ShortlistApp) -> None:
    page.set_viewport_size({"width": 1440, "height": 950})
    build_real_rows(app)  # a real run against the fake server, so the pages have rows/picks/history

    sarah = _users_by_name(app)["sarah"]["id"]
    run_id = app.api("GET", "/api/runs").json()[0]["id"]

    _capture(page, f"/users/{sarah}", "user-detail.png", wait="Because you watched")
    _capture(page, "/users", "users.png", wait="sarah")
    _capture(page, "/rows", "rows.png", wait="Picked for You")
    _capture(page, f"/runs/{run_id}", "run-detail.png", wait="AI tokens")
    _capture(page, "/settings", "settings.png", wait="Connections")
    _capture(page, "/", "dashboard.png")


@skip_unless_capturing
def test_capture_wizard_screenshot(fresh_page: Page, fresh_app: ShortlistApp) -> None:
    fresh_page.set_viewport_size({"width": 1440, "height": 950})
    fresh_page.goto("/setup")
    fresh_page.wait_for_timeout(1000)
    _shot(fresh_page, "wizard.png")
