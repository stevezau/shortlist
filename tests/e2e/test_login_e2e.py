"""E2E: the front door.

Every other e2e test arrives holding a session cookie, so none of them ever walk through the
front door — and a real deployment immediately showed why that matters: an unauthenticated
visitor got a permanent loading skeleton instead of the login screen, because the route guard
asked for owner-only setup state before it knew who the visitor was, got a 401, and retried it
behind a spinner.
"""

from __future__ import annotations

import contextlib

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.e2e.conftest import RowarrApp

pytestmark = pytest.mark.e2e

LOAD = 20_000


@pytest.fixture
def anonymous_page(browser: Browser, app: RowarrApp) -> Page:
    """A visitor with NO session cookie — exactly what a stranger (or the owner) first sees."""
    context = browser.new_context(base_url=app.url)
    page = context.new_page()
    yield page
    context.close()


def test_an_unauthenticated_visitor_lands_on_the_login_screen(anonymous_page: Page, app: RowarrApp):
    """Once the instance is CLAIMED (the `app` fixture has a linked server), it is the owner's."""
    page = anonymous_page
    page.goto("/")

    expect(page).to_have_url(f"{app.url}/login", timeout=LOAD)
    expect(page.get_by_role("button", name="Login with Plex")).to_be_visible(timeout=LOAD)
    # The skeleton must not still be sitting there behind the login card.
    expect(page.get_by_role("button", name="Login with Plex")).to_be_enabled()


def test_a_fresh_install_opens_the_wizard_without_asking_anyone_to_sign_in(browser: Browser, fresh_app: RowarrApp):
    """Nobody has linked a Plex server yet: there is no token, no user list, no history — nothing
    to protect and nobody to protect it for. Demanding a sign-in first is a door with no house
    behind it. Signing in with Plex is not a gate in front of setup; it IS a step of setup, and it
    is the step that claims the instance."""
    context = browser.new_context(base_url=fresh_app.url)  # no session cookie at all
    page = context.new_page()
    try:
        page.goto("/")

        expect(page).to_have_url(f"{fresh_app.url}/setup", timeout=LOAD)
        expect(page.get_by_role("heading", name="Welcome")).to_be_visible(timeout=LOAD)
        # And no login wall in front of it.
        expect(page.get_by_role("button", name="Login with Plex")).to_have_count(0)
    finally:
        context.close()


def test_a_protected_route_redirects_a_stranger_to_login(anonymous_page: Page, app: RowarrApp):
    page = anonymous_page
    page.goto("/settings")

    expect(page).to_have_url(f"{app.url}/login", timeout=LOAD)
    expect(page.get_by_role("button", name="Login with Plex")).to_be_visible()


def test_the_login_screen_does_not_hammer_the_api_with_retries(anonymous_page: Page, app: RowarrApp):
    """A 401 cannot fix itself: retrying it just hides the login screen behind a spinner."""
    page = anonymous_page
    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url) if "/api/setup/state" in r.url else None)

    page.goto("/")
    expect(page.get_by_role("button", name="Login with Plex")).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(3000)

    assert not calls, f"owner-only setup state was fetched while signed out: {calls}"


def test_the_plex_token_never_reaches_the_browser(anonymous_page: Page, app: RowarrApp):
    """The owner's Plex token is the keys to their server. It stays on the server.

    It used to be handed to the SPA so the wizard could probe and link — which meant an XSS
    anywhere in the UI could steal it, and (because it lived in component memory only) the
    owner had to sign in a second time on the wizard's first step. Now the backend mints it
    and keeps it, so neither is true.
    """
    page = anonymous_page
    bodies: list[str] = []

    def capture(response) -> None:
        if "/api/auth/" in response.url:
            with contextlib.suppress(Exception):  # streamed/redirect responses have no body
                bodies.append(response.text())

    page.on("response", capture)
    page.goto("/")
    expect(page.get_by_role("button", name="Login with Plex")).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(1000)

    for body in bodies:
        assert '"token"' not in body, f"an auth response carried a Plex token to the browser: {body[:200]}"
