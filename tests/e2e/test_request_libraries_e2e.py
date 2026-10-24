"""A row's "Which libraries get requests" switches, driven through the real editor (issue #169).

Round-tripped through the API: the point of the control is the value that reaches a run, so switches
that render and save nothing would pass a render-only check.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

LOAD = 15_000
PARTIAL_NOTE = re.compile("Titles already waiting in your inbox stay there")
NOTHING_NOTE = re.compile("This row won.t ask for anything")


def _open_row_requests(page: Page) -> None:
    """The default per-person row's editor, at its Requests section (every section is always rendered)."""
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name=re.compile(r"^More actions for ")).first.click()
    page.get_by_role("menuitem", name="Edit", exact=True).click()
    expect(page.get_by_role("heading", name="✨ library name Picked for You", exact=True)).to_be_visible(timeout=LOAD)
    expect(page.locator("section#requests")).to_be_visible()


def _connect_arrs(app: ShortlistApp) -> None:
    """Requests on, with Sonarr and Radarr saved — what makes their per-row fields render."""
    app.api(
        "PUT",
        "/api/settings",
        json={
            "values": {
                "requests.enabled": True,
                "requests.sonarr.url": "http://sonarr.invalid",
                "requests.sonarr.apikey": "fake-key",
                "requests.radarr.url": "http://radarr.invalid",
                "requests.radarr.apikey": "fake-key",
            }
        },
    )


def test_a_row_can_stop_asking_for_one_library_and_it_sticks(page: Page, app: ShortlistApp):
    _connect_arrs(app)
    _open_row_requests(page)
    section = page.locator("section#requests")

    movies = section.get_by_role("switch", name=re.compile("Movies"))
    shows = section.get_by_role("switch", name=re.compile("TV Shows"))
    expect(section.get_by_text("Which libraries get requests", exact=True)).to_be_visible(timeout=LOAD)
    expect(movies).to_be_checked()
    expect(shows).to_be_checked()
    expect(section.get_by_text(PARTIAL_NOTE)).to_have_count(0)
    expect(section.get_by_text(NOTHING_NOTE)).to_have_count(0)
    films = section.get_by_text("Where films from this row land", exact=True)
    show_folder = section.get_by_text("Where shows from this row land", exact=True)
    show_amount = section.get_by_text("How much of a show this row grabs", exact=True)
    expect(show_folder).to_be_visible()

    shows.click()
    expect(section.get_by_text(PARTIAL_NOTE)).to_be_visible()
    expect(show_folder).to_have_count(0)
    expect(show_amount).to_have_count(0)
    expect(films).to_be_visible()

    page.get_by_role("button", name="Save changes").click()
    row = app.wait_for_row_field("picked", "req_skip_library_keys", ["2"])
    assert row["req_skip_library_keys"] == ["2"]

    _open_row_requests(page)
    expect(page.locator("section#requests").get_by_role("switch", name=re.compile("TV Shows"))).not_to_be_checked(
        timeout=LOAD
    )

    page.locator("section#requests").get_by_role("switch", name=re.compile("TV Shows")).click()
    page.get_by_role("button", name="Save changes").click()
    app.wait_for_row_field("picked", "req_skip_library_keys", None)


def test_turning_every_library_off_says_the_row_asks_for_nothing(page: Page, app: ShortlistApp):
    _connect_arrs(app)
    _open_row_requests(page)
    section = page.locator("section#requests")

    section.get_by_role("switch", name=re.compile("Movies")).click(timeout=LOAD)
    section.get_by_role("switch", name=re.compile("TV Shows")).click()

    expect(section.get_by_text(NOTHING_NOTE)).to_be_visible()
    expect(section.get_by_text(PARTIAL_NOTE)).to_have_count(0)
