"""E2E: the Rows page — create curated rows through the UI and confirm they reach the backend.

Full stack: real browser -> built image -> fake PMS/plex.tv. The Rows page is where an owner
decides what Rowarr builds, so "I clicked Add and it saved" has to be true end to end.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import RowarrApp

pytestmark = pytest.mark.e2e

LOAD = 20_000


def _open_rows(page: Page) -> None:
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)


def test_default_row_is_listed_and_a_per_person_row_can_be_added(page: Page, app: RowarrApp):
    _open_rows(page)
    # The migration seeds one default per-person row.
    expect(page.get_by_text("Picked for You").first).to_be_visible(timeout=LOAD)
    expect(page.get_by_text("default")).to_be_visible()

    page.get_by_role("button", name="Add a row").click()
    expect(page.get_by_role("heading", name="Add a row")).to_be_visible()
    page.get_by_label("Name").fill("Hidden Gems")
    page.get_by_role("button", name="Add row").click()

    expect(page.get_by_text("Hidden Gems").first).to_be_visible(timeout=LOAD)
    slugs = {c["slug"] for c in app.api("GET", "/api/collections").json()}
    assert {"picked", "hidden_gems"} <= slugs


def test_a_shared_row_created_in_the_ui_is_stored_as_shared(page: Page, app: RowarrApp):
    _open_rows(page)
    page.get_by_role("button", name="Add a row").click()
    page.get_by_label("Name").fill("Popular Here")
    page.get_by_role("button", name="Shared", exact=True).click()
    # The aggregate-privacy control appears only for shared rows.
    expect(page.get_by_text("Only show titles at least this many people watched")).to_be_visible()
    page.get_by_role("button", name="Add row").click()

    expect(page.get_by_text("Popular Here").first).to_be_visible(timeout=LOAD)
    created = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Popular Here")
    assert created["build"] == "shared"


def test_the_default_row_cannot_be_deleted(page: Page, app: RowarrApp):
    _open_rows(page)
    picked = next(c for c in app.api("GET", "/api/collections").json() if c["slug"] == "picked")
    # The API refuses; the UI never offers a delete button on the default row.
    assert app.api("DELETE", f"/api/collections/{picked['id']}").status_code == 422
