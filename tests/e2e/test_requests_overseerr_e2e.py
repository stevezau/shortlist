"""Routing requests through Overseerr/Jellyseerr, driven through the real screens (discussion #110).

Round-tripped through the API rather than asserted on the DOM alone: a chooser that renders and
saves nothing would pass a render-only check, and what the target setting is worth is entirely the
value that reaches a run.

Nothing here answers as Overseerr — no fake serves its API — which is deliberate. The account list
is fetched, so this covers the case the card has to survive: the target is chosen, saved and
reloaded even when the instance behind it never replies.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

LOAD = 15_000


def _panel(page: Page):
    """The Requests panel alone.

    Scoped, not page-wide: Settings → Connections carries its own Radarr, Sonarr and Overseerr cards,
    which stay on screen whichever route is chosen. A page-wide locator therefore matches the
    connection card as well as the control under test — "Overseerr / Jellyseerr" resolves to two
    buttons, and "Radarr" is present no matter what the target is set to.
    """
    return page.locator("section[aria-labelledby='requests-heading']")


def _connect_overseerr(app: ShortlistApp) -> None:
    """Requests on, with Overseerr's address and key saved — what makes the card render its controls."""
    app.api(
        "PUT",
        "/api/settings",
        json={
            "values": {
                "requests.enabled": True,
                "requests.overseerr.url": "http://overseerr.invalid",
                "requests.overseerr.apikey": "fake-key",
            }
        },
    )


def test_switching_the_target_saves_and_survives_a_reload(page: Page, app: ShortlistApp):
    _connect_overseerr(app)
    page.goto("/settings")

    expect(_panel(page).get_by_role("button", name="Radarr & Sonarr")).to_have_attribute(
        "aria-pressed", "true", timeout=LOAD
    )
    _panel(page).get_by_role("button", name="Overseerr / Jellyseerr", exact=True).click()

    # Autosave has no button; wait for the value to reach the API rather than a fixed sleep.
    expect(_panel(page).get_by_text(re.compile("Saved|Saving", re.I)).first).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(2000)

    assert app.api("GET", "/api/settings").json()["requests.target"] == "overseerr"

    page.reload()
    expect(_panel(page).get_by_role("button", name="Overseerr / Jellyseerr", exact=True)).to_have_attribute(
        "aria-pressed", "true", timeout=LOAD
    )


def test_the_overseerr_card_replaces_the_two_arr_cards(page: Page, app: ShortlistApp):
    _connect_overseerr(app)
    app.api("PUT", "/api/settings", json={"values": {"requests.target": "overseerr"}})
    page.goto("/settings")

    expect(_panel(page).get_by_label("Request as")).to_be_visible(timeout=LOAD)
    expect(_panel(page).get_by_text("Radarr", exact=True)).to_have_count(0)
    expect(_panel(page).get_by_text("Sonarr", exact=True)).to_have_count(0)
    # Guardrails belong to Shortlist, not to the app doing the fetching, so they stay on both routes.
    expect(_panel(page).get_by_text("Guardrails", exact=True)).to_be_visible()


def test_the_tag_fields_are_gone_because_overseerr_cannot_carry_them(page: Page, app: ShortlistApp):
    """Overseerr's POST /request body has no tags field, so offering the setting would be a lie."""
    _connect_overseerr(app)
    app.api("PUT", "/api/settings", json={"values": {"requests.target": "overseerr"}})
    page.goto("/settings")

    expect(_panel(page).get_by_label("Request as")).to_be_visible(timeout=LOAD)
    expect(_panel(page).get_by_label("Tag added items")).to_have_count(0)
    expect(_panel(page).get_by_label("Also tag by person")).to_have_count(0)


def test_the_default_route_still_shows_radarr_and_sonarr(page: Page, app: ShortlistApp):
    """The control case. Adding a second route must not change what an existing install sees."""
    app.api("PUT", "/api/settings", json={"values": {"requests.enabled": True}})
    page.goto("/settings")

    expect(_panel(page).get_by_text("Radarr", exact=True).first).to_be_visible(timeout=LOAD)
    expect(_panel(page).get_by_text("Sonarr", exact=True).first).to_be_visible()
    expect(_panel(page).get_by_label("Request as")).to_have_count(0)
