"""How much of a show Sonarr takes, driven through the real screens (issue #100).

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
    """The default per-person row's editor, with its Requests group expanded (collapsed by default)."""
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Edit").first.click()
    expect(page.get_by_role("heading", name="Edit row")).to_be_visible(timeout=LOAD)
    # The group's own <summary>, not any text reading "Requests" — the sidebar's Requests LINK is
    # first in the DOM, so a loose text match navigates away from the editor instead of expanding it.
    page.locator("details:has(> summary:has-text('Requests')) > summary").click()


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


def test_the_global_amount_of_a_show_saves_and_survives_a_reload(page: Page, app: ShortlistApp):
    _connect_sonarr(app)
    page.goto("/settings")

    monitor = page.get_by_label("How much of a show to grab")
    expect(monitor).to_be_visible(timeout=LOAD)
    expect(monitor).to_have_value("all")  # the default, and what every add did before this existed

    monitor.select_option("firstSeason")
    expect(page.get_by_text(re.compile("Season 1 only"))).to_be_visible()

    # Autosave has no button; wait for the value to reach the API rather than a fixed sleep.
    expect(page.get_by_text(re.compile("Saved|Saving", re.I)).first).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(2000)

    saved = app.api("GET", "/api/settings").json()
    assert saved["requests.sonarr.monitor"] == "firstSeason"

    page.reload()
    expect(page.get_by_label("How much of a show to grab")).to_have_value("firstSeason", timeout=LOAD)


def test_a_row_can_take_less_of_a_show_than_the_global(page: Page, app: ShortlistApp):
    _connect_sonarr(app)
    _open_row_requests(page)

    inherit = page.get_by_label("Use the global amount-of-a-show setting for this row")
    expect(inherit).to_be_checked(timeout=LOAD)
    # While inheriting, the row names the global in Sonarr's own words rather than a value it lacks.
    expect(page.get_by_text(re.compile("All Episodes"))).to_be_visible()

    inherit.uncheck()
    row_monitor = page.locator("#row-req-sonarr-monitor")
    expect(row_monitor).to_be_visible()
    row_monitor.select_option("pilot")

    page.get_by_role("button", name="Save changes").click()
    page.wait_for_timeout(2000)

    rows = {c["slug"]: c for c in app.api("GET", "/api/collections").json()}
    assert rows["picked"]["req_sonarr_monitor"] == "pilot"

    _open_row_requests(page)
    expect(page.locator("#row-req-sonarr-monitor")).to_have_value("pilot", timeout=LOAD)


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

    inherit = page.get_by_label("Use the global amount-of-a-show setting for this row")
    expect(inherit).not_to_be_checked(timeout=LOAD)
    inherit.check()

    page.get_by_role("button", name="Save changes").click()
    page.wait_for_timeout(2000)

    rows = {c["slug"]: c for c in app.api("GET", "/api/collections").json()}
    assert rows["picked"]["req_sonarr_monitor"] is None
