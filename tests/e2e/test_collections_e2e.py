"""E2E: the Rows page — create curated rows through the UI and confirm they reach the backend.

Full stack: real browser -> built image -> fake PMS/plex.tv. The Rows page is where an owner
decides what Shortlist builds, so "I clicked Add and it saved" has to be true end to end.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

LOAD = 20_000


def _open_rows(page: Page) -> None:
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)


def test_default_row_is_listed_and_a_per_person_row_can_be_added(page: Page, app: ShortlistApp):
    _open_rows(page)
    # The migration seeds one default per-person row.
    expect(page.get_by_text("Picked for You").first).to_be_visible(timeout=LOAD)
    expect(page.get_by_text("default")).to_be_visible()

    page.get_by_role("button", name="Add a row").click()
    expect(page.get_by_role("heading", name="Add a row")).to_be_visible()
    # exact=True: get_by_label is a substring match, and the default row's name is the *template*
    # "✨ {library_name} Picked for You" — so its card's "Enable …"/"Remove …" aria-labels contain
    # "name" and would otherwise collide with the dialog's real "Name" field.
    page.get_by_label("Name", exact=True).fill("Hidden Gems")
    page.get_by_role("button", name="Add row").click()

    expect(page.get_by_text("Hidden Gems").first).to_be_visible(timeout=LOAD)
    slugs = {c["slug"] for c in app.api("GET", "/api/collections").json()}
    assert {"picked", "hidden_gems"} <= slugs


def test_a_shared_row_created_in_the_ui_is_stored_as_shared(page: Page, app: ShortlistApp):
    _open_rows(page)
    page.get_by_role("button", name="Add a row").click()
    page.get_by_label("Name", exact=True).fill("Popular Here")
    page.get_by_role("button", name="Shared", exact=True).click()
    # The aggregate-privacy control appears only for shared rows.
    expect(page.get_by_text("Only show titles at least this many people watched")).to_be_visible()
    page.get_by_role("button", name="Add row").click()

    expect(page.get_by_text("Popular Here").first).to_be_visible(timeout=LOAD)
    created = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Popular Here")
    assert created["build"] == "shared"


def test_a_row_can_be_given_a_built_in_text_poster(page: Page, app: ShortlistApp):
    _open_rows(page)
    page.get_by_role("button", name="Add a row").click()
    page.get_by_label("Name", exact=True).fill("Poster Row")
    page.get_by_role("button", name="Add row").click()
    expect(page.get_by_text("Poster Row").first).to_be_visible(timeout=LOAD)

    # Re-open it and choose a built-in text poster — this needs no AI provider, so it works on any setup.
    page.get_by_role("button", name="Edit").last.click()
    expect(page.get_by_label("Name", exact=True)).to_have_value("Poster Row")
    page.get_by_role("button", name="Text", exact=True).click()
    page.get_by_label("Title text").fill("Weekend Picks")
    page.get_by_role("button", name="Save changes").click()

    created = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Poster Row")
    assert created["poster"]["mode"] == "text"
    assert created["poster"]["title"] == "Weekend Picks"
    # The built-in renderer produces a real image with no AI provider configured.
    image = app.api("GET", f"/api/collections/{created['id']}/poster/image")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")


def test_the_default_row_cannot_be_deleted(page: Page, app: ShortlistApp):
    _open_rows(page)
    picked = next(c for c in app.api("GET", "/api/collections").json() if c["slug"] == "picked")
    # The API refuses; the UI never offers a delete button on the default row.
    assert app.api("DELETE", f"/api/collections/{picked['id']}").status_code == 422
