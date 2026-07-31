"""E2E: the Rows page — create curated rows through the UI and confirm they reach the backend.

Full stack: real browser -> built image -> fake PMS/plex.tv. The Rows page is where an owner
decides what Shortlist builds, so "I clicked Add and it saved" has to be true end to end.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

LOAD = 20_000


def _add_a_row(page: Page) -> None:
    """Open the row editor via the template gallery.

    "Add a row" now opens a gallery first — a blank 17-field form only ever helped someone who
    already knew what they wanted to build. These tests are about the editor, so they take the
    "Start from scratch" tile, which is the same blank form as before.
    """
    page.get_by_role("button", name="Add a row").click()
    page.get_by_role("button", name="Start from scratch").click()
    expect(page.get_by_role("heading", name="Add a row")).to_be_visible()


def _open_rows(page: Page) -> None:
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)


def test_default_row_is_listed_and_a_per_person_row_can_be_added(page: Page, app: ShortlistApp):
    _open_rows(page)
    # The migration seeds one default per-person row.
    expect(page.get_by_text("Picked for You").first).to_be_visible(timeout=LOAD)
    expect(page.get_by_text("default")).to_be_visible()

    _add_a_row(page)
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
    _add_a_row(page)
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
    _add_a_row(page)
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


def test_the_default_rows_name_can_be_edited_and_updates_the_global_template(page: Page, app: ShortlistApp):
    """The default row's name field used to be disabled (name came only from Settings → Defaults).
    It's now editable inline, and saving it writes the shared `row.name_template` setting."""
    _open_rows(page)
    # The default row is the only one on a fresh install, so its Edit button is the first.
    page.get_by_role("button", name="Edit").first.click()
    expect(page.get_by_role("heading", name="Edit row")).to_be_visible()

    name = page.get_by_label("Name", exact=True)
    expect(name).to_be_disabled()  # existing rows show name read-only; rename via button
    expect(name).to_have_value("✨ {library_name} Picked for You")  # its value IS the global template
    # Close the editor; rename happens via the row-card's Rename button + dialog.
    page.get_by_role("button", name="Cancel").click()

    page.get_by_role("button", name="Rename").click()
    rename_input = page.get_by_label("New name")
    expect(rename_input).to_be_visible()
    rename_input.fill("✨ {library_name} Handpicked")
    page.get_by_role("button", name="Rename on Plex").click()

    # The rename triggers an SSE stream page — wait for it to finish, then check the DB.
    expect(page.get_by_text("Done")).to_be_visible(timeout=LOAD)
    settings = app.api("GET", "/api/settings").json()
    assert settings["row.name_template"] == "✨ {library_name} Handpicked"


def test_the_default_row_can_be_deleted_like_any_other(page: Page, app: ShortlistApp):
    """It used to 422, and the card hid its Delete button — so the first row in the list lacked the
    control every row below it had, with nothing on screen saying why. Rows are user-created now and
    an empty list means "everything is off", not "resurrect the default"."""
    _open_rows(page)
    picked = next(c for c in app.api("GET", "/api/collections").json() if c["slug"] == "picked")

    # Counted, not matched by name: the app is shared across this module and another test renames
    # this row, so its rendered title is not stable. "Every row has a Delete button" is also the
    # actual property — the bug was ONE card missing the control its neighbours had.
    #
    # Asserts the BUTTON only: deleting the seeded row here would pull it out from under every test
    # that follows. The 204 and the row actually disappearing are covered in
    # tests/integration/test_api_collections.py::test_the_default_row_can_be_deleted_like_any_other.
    assert picked, "the seeded default row must exist for this to mean anything"
    rows = app.api("GET", "/api/collections").json()
    expect(page.get_by_role("button", name=re.compile(r"^Delete "))).to_have_count(len(rows))


PLACEMENT_SWITCHES = (
    "Owner Library Recommended",
    "Owner Home",
    "Friends Library Recommended",
    "Friends' Home",
)


def test_every_surface_can_be_turned_off_and_reaches_the_api(page: Page, app: ShortlistApp):
    """Issue #6: all four "Where it shows" switches must be able to be off at once.

    The old encoder had no "neither" state and fell through to Library Recommended, so turning the
    second switch of a pair off silently turned the first back on — reported by two beta users as
    "the toggles are mutually exclusive".
    """
    _open_rows(page)
    _add_a_row(page)
    page.get_by_label("Name", exact=True).fill("Quiet Row")

    for name in PLACEMENT_SWITCHES:
        page.get_by_role("switch", name=name).click()
    for name in PLACEMENT_SWITCHES:
        expect(page.get_by_role("switch", name=name)).not_to_be_checked()

    page.get_by_role("button", name="Add row").click()
    expect(page.get_by_text("Quiet Row").first).to_be_visible(timeout=LOAD)

    created = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Quiet Row")
    assert created["placement"] == "off"
    assert created["placement_friends"] == "off"


def test_the_two_placement_columns_are_saved_independently(page: Page, app: ShortlistApp):
    """The owner keeps their own row on the Recommended shelf while friends' rows come off it —
    the split that is only possible because every person gets their own Plex collection."""
    _open_rows(page)
    _add_a_row(page)
    page.get_by_label("Name", exact=True).fill("Split Row")

    page.get_by_role("switch", name="Friends Library Recommended").click()
    page.get_by_role("button", name="Add row").click()
    expect(page.get_by_text("Split Row").first).to_be_visible(timeout=LOAD)

    created = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Split Row")
    assert created["placement"] == "both"
    assert created["placement_friends"] == "home"
