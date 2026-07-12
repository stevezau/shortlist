"""E2E: the shipped SPA, in a real browser, against the real API and a fake Plex server.

This is the layer that catches cross-boundary contract breaks — a missing CSRF header or a
mismatched request body passes both unit suites and dies here.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import RowarrApp

pytestmark = pytest.mark.e2e


class TestAppLoads:
    def test_dashboard_renders_users_from_the_api(self, page: Page, app: RowarrApp):
        page.goto("/")
        expect(page.get_by_text("sarah", exact=False).first).to_be_visible(timeout=20_000)
        expect(page.get_by_text("mike", exact=False).first).to_be_visible()

    def test_no_console_errors(self, page: Page, app: RowarrApp):
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto("/")
        page.wait_for_timeout(2500)
        assert not errors, errors


class TestMutationContract:
    """A mutation issued BY THE SPA must reach the backend and actually take effect.

    Regression cover for the two HIGH review findings: the missing `x-rowarr-csrf` header
    (every UI mutation 403'd) and the uninstall body that never carried its confirmation.
    The browser context deliberately does not inject the header — the app must send it.
    """

    def test_user_toggle_from_the_ui_persists(self, page: Page, app: RowarrApp):
        page.goto("/users")
        toggle = page.get_by_role("switch").first
        expect(toggle).to_be_visible(timeout=20_000)
        before = toggle.is_checked()

        responses: list[int] = []
        page.on("response", lambda r: responses.append(r.status) if "/api/users/" in r.url else None)
        toggle.click()
        page.wait_for_timeout(2500)

        assert responses, "the toggle issued no PATCH to /api/users/{id}"
        assert 403 not in responses, "the SPA's mutation was rejected — CSRF header missing"
        assert 200 in responses, f"unexpected statuses from the PATCH: {responses}"

        # The change must survive a reload — proof it reached the database, not just the UI.
        page.reload()
        expect(page.get_by_role("switch").first).to_be_visible(timeout=20_000)
        assert page.get_by_role("switch").first.is_checked() is not before

    def test_no_spa_mutation_leaves_without_the_csrf_header(self, page: Page, app: RowarrApp):
        missing: list[str] = []

        def check(request) -> None:
            is_mutation = request.method not in ("GET", "HEAD", "OPTIONS") and "/api/" in request.url
            if is_mutation and not request.header_value("x-rowarr-csrf"):
                missing.append(f"{request.method} {request.url}")

        page.on("request", check)
        page.goto("/users")
        expect(page.get_by_role("switch").first).to_be_visible(timeout=20_000)
        page.get_by_role("switch").first.click()
        page.wait_for_timeout(2000)
        assert not missing, f"SPA sent mutations without the CSRF header: {missing}"
