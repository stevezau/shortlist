"""How much of a show a request takes, driven through the real screens (issues #100 and #154).

Two surfaces, both round-tripped through the API rather than asserted on the DOM alone: the whole
point of the control is the value that reaches a run, so a select that renders and saves nothing
would pass a render-only check.

The Arr profile/folder dropdowns fetch from Sonarr itself, which no fake answers here — so this also
covers the case the layout was restructured for: the monitor choice is Sonarr's own enum, not
something fetched, and must stay usable when that fetch fails.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

LOAD = 15_000


def _open_row_requests(page: Page) -> None:
    """The default per-person row's editor, at its Requests section (every section is always rendered)."""
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name=re.compile(r"^More actions for ")).first.click()
    page.get_by_role("menuitem", name="Edit", exact=True).click()
    expect(page.get_by_role("heading", name="✨ library name Picked for You", exact=True)).to_be_visible(timeout=LOAD)
    expect(page.locator("section#requests")).to_be_visible()


def _connect_sonarr(app: ShortlistApp) -> None:
    """Requests on, with Sonarr's address and key saved — what makes the card render its controls."""
    app.api(
        "PUT",
        "/api/settings",
        json={
            "values": {
                "requests.enabled": True,
                "requests.sonarr.url": "http://sonarr.invalid",
                "requests.sonarr.apikey": "fake-key",
            }
        },
    )


def _seasons(page: Page):
    """The visible season count — the stepper's `<output>`."""
    return page.get_by_label("Number of seasons", exact=True)


def test_the_global_amount_of_a_show_saves_and_survives_a_reload(page: Page, app: ShortlistApp):
    _connect_sonarr(app)
    page.goto("/settings#requests")

    every = page.get_by_label("Every season", exact=True)
    expect(every).to_be_checked(timeout=LOAD)  # the default, and what every add did before this existed

    every.uncheck()
    expect(page.get_by_role("button", name="First seasons", exact=True)).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_text(re.compile("Season 1 only"))).to_be_visible()
    page.get_by_role("button", name="More seasons", exact=True).click()
    expect(_seasons(page)).to_have_text("2")
    expect(page.get_by_text(re.compile("Seasons 1 to 2"))).to_be_visible()

    # Autosave has no button; the stored value is the real wait.
    # The Requests tab keeps its own "Saved" readout beside the form (it has no save bar).
    expect(page.get_by_role("status").get_by_text("Saved", exact=True)).to_be_visible(timeout=LOAD)
    app.wait_for_setting("requests.sonarr.monitor", "firstSeason")
    app.wait_for_setting("requests.season_count", 2)

    page.reload()
    expect(page.get_by_role("button", name="First seasons", exact=True)).to_have_attribute(
        "aria-pressed", "true", timeout=LOAD
    )
    expect(_seasons(page)).to_have_text("2")
    expect(page.get_by_label("Every season", exact=True)).not_to_be_checked()


def test_overseerr_offers_only_what_it_can_send(page: Page, app: ShortlistApp):
    """Overseerr takes a season list and nothing else, so Sonarr's pilot/unmonitored modes are not offered."""
    app.api("PUT", "/api/settings", json={"values": {"requests.enabled": True, "requests.target": "overseerr"}})
    page.goto("/settings#requests")

    expect(page.get_by_label("Every season", exact=True)).to_be_visible(timeout=LOAD)
    expect(page.get_by_role("button", name="First seasons", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Last seasons", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Pilot episode", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Add unmonitored", exact=True)).to_have_count(0)
    expect(page.get_by_text(re.compile("Monitor New Seasons"))).to_be_visible()


def test_a_row_can_take_less_of_a_show_than_the_global(page: Page, app: ShortlistApp):
    _connect_sonarr(app)
    _open_row_requests(page)

    override = page.get_by_role("button", name="Override How much of a show this row grabs", exact=True)
    expect(override).to_be_visible(timeout=LOAD)
    # While inheriting, the row names the global in the control's own words rather than a value it lacks.
    expect(page.get_by_text(re.compile("Every season"))).to_be_visible()

    override.click()
    section = page.locator("section#requests")
    section.get_by_role("button", name="Last seasons", exact=True).click()
    section.get_by_role("button", name="More seasons", exact=True).click()
    section.get_by_role("button", name="More seasons", exact=True).click()
    expect(_seasons(page)).to_have_text("3")

    page.get_by_role("button", name="Save changes").click()
    app.wait_for_row_field("picked", "req_sonarr_monitor", "lastSeason")
    app.wait_for_row_field("picked", "req_season_count", 3)

    _open_row_requests(page)
    expect(page.get_by_role("button", name="Last seasons", exact=True)).to_have_attribute(
        "aria-pressed", "true", timeout=LOAD
    )
    expect(_seasons(page)).to_have_text("3")


def test_a_row_can_take_only_the_pilot_on_the_sonarr_route(page: Page, app: ShortlistApp):
    _connect_sonarr(app)
    _open_row_requests(page)

    page.get_by_role("button", name="Override How much of a show this row grabs", exact=True).click(timeout=LOAD)
    page.get_by_role("button", name="Pilot episode", exact=True).click()

    page.get_by_role("button", name="Save changes").click()
    app.wait_for_row_field("picked", "req_sonarr_monitor", "pilot")


def test_clearing_the_row_override_puts_it_back_on_the_global(page: Page, app: ShortlistApp):
    """NULL is the inherit signal, so "use the global again" must store null — not the mode that was
    on screen when the box was ticked, which would silently pin the row to today's global."""
    _connect_sonarr(app)
    # Resolve the id rather than assuming the seeded row is 1: `app.api` doesn't raise on a non-2xx,
    # so a 404 here would surface three lines later as a baffling Playwright timeout on a checkbox.
    picked = next(c for c in app.api("GET", "/api/collections").json() if c["slug"] == "picked")
    resp = app.api(
        "PATCH",
        f"/api/collections/{picked['id']}",
        json={"name": picked["name"], "req_sonarr_monitor": "lastSeason"},
    )
    assert resp.status_code == 200, resp.text

    _open_row_requests(page)

    expect(page.get_by_role("button", name="Override How much of a show this row grabs", exact=True)).to_have_count(0)
    page.get_by_role("button", name="Reset How much of a show this row grabs", exact=True).click(timeout=LOAD)

    page.get_by_role("button", name="Save changes").click()
    app.wait_for_row_field("picked", "req_sonarr_monitor", None)
    app.wait_for_row_field("picked", "req_season_count", None)
