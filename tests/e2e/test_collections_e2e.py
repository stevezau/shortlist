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


def _saved_row(page: Page, name: str):
    """The card for a saved row on the /rows list.

    Scoped to the list, not the whole document: the editor's preview panel renders the name being
    typed, so a bare `get_by_text(name)` matches the form someone is still filling in and would pass
    before anything was saved.
    """
    expect(page).to_have_url(re.compile(r"/rows$"), timeout=LOAD)
    return page.get_by_text(name, exact=False).first


def _open_row_menu(page: Page, name: str | None = None) -> None:
    """Open a row card's "⋯" menu: THIS row's when named, else the first card's.

    Named by its own card's button, not `.last` — under load the list from before the save rendered
    last, so a positional pick opened the default row instead (seen twice at load 27)."""
    if name is None:
        page.get_by_role("button", name=re.compile(r"^More actions for ")).first.click()
    else:
        page.get_by_role("button", name=f"More actions for {name}", exact=True).click()


def _edit_row(page: Page, name: str | None = None) -> None:
    """Open a row's editor through its card's menu (the first card's when no name is given)."""
    _open_row_menu(page, name)
    page.get_by_role("menuitem", name="Edit", exact=True).click()


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
    # One row for everyone is the Popular on this server kind; a new row switches with no dialog.
    page.locator("details[data-setting=kind] > summary").click()
    page.get_by_role("radio", name="Popular on this server", exact=True).click()
    # The aggregate-privacy control appears only for shared rows.
    expect(page.get_by_text("Only titles watched by at least")).to_be_visible()
    page.get_by_role("button", name="Add row").click()

    expect(page.get_by_text("Popular Here").first).to_be_visible(timeout=LOAD)
    created = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Popular Here")
    assert created["build"] == "shared"


def test_a_row_can_be_given_a_built_in_text_poster(page: Page, app: ShortlistApp):
    _open_rows(page)
    _add_a_row(page)
    page.get_by_label("Name", exact=True).fill("Poster Row")
    page.get_by_role("button", name="Add row").click()
    expect(_saved_row(page, "Poster Row")).to_be_visible(timeout=LOAD)

    # Re-open it and choose a built-in text poster — this needs no AI provider, so it works on any setup.
    _edit_row(page, "Poster Row")
    # A saved row's name is read-only now (it changes only through Rename on Plex), so the page
    # heading is what says this is the row just added.
    expect(page.get_by_role("heading", name="Poster Row", level=1)).to_be_visible(timeout=LOAD)
    # The poster sits in the open "Appearance" group, beside the name it belongs to.
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


def test_a_row_can_be_given_a_description_and_sort_title_prefix(page: Page, app: ShortlistApp):
    """Issue #120: the description is set beside the name, the prefix beside the row's placement."""
    _open_rows(page)
    _add_a_row(page)
    page.get_by_label("Name", exact=True).fill("Sorted Row")
    page.get_by_role("button", name="Add row").click()
    expect(_saved_row(page, "Sorted Row")).to_be_visible(timeout=LOAD)

    _edit_row(page, "Sorted Row")
    # A saved row's name is read-only now (it changes only through Rename on Plex), so the page
    # heading is what says this is the row just added.
    expect(page.get_by_role("heading", name="Sorted Row", level=1)).to_be_visible(timeout=LOAD)
    page.get_by_label("Description", exact=True).fill("Picked for {user}")
    page.get_by_label("Sort title prefix").fill("!010_")
    expect(page.get_by_text("!010_Sorted Row")).to_be_visible()
    page.get_by_role("button", name="Save changes").click()
    expect(page).to_have_url(re.compile(r"/rows$"), timeout=LOAD)

    saved = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Sorted Row")
    assert (saved["description"], saved["sort_title_prefix"]) == ("Picked for {user}", "!010_")


def test_the_default_rows_name_can_be_edited_and_updates_the_global_template(page: Page, app: ShortlistApp):
    """The default row's name field used to be disabled (name came only from Settings → Defaults).
    It's now editable through Rename on Plex…, and renaming writes the shared `row.name_template`
    setting."""
    _open_rows(page)
    # The default row is the only one on a fresh install, so its card is the first.
    _edit_row(page)
    expect(page.get_by_role("heading", name="✨ library name Picked for You", exact=True)).to_be_visible()

    # The editor shows the name read-only; a new one is typed in the Rename on Plex dialog, because
    # Save never carries a name — only Rename applies it.
    page.get_by_role("button", name="Rename on Plex…").click()
    name = page.get_by_role("dialog").get_by_label("New name", exact=True)
    expect(name).to_be_enabled()
    expect(name).to_have_value("✨ {library_name} Picked for You")  # its value IS the global template
    # Typing must say, on screen, that nothing has happened yet. Without this the box looks like
    # every other field on the page, which would imply Save applies it — Save deliberately does not.
    name.fill("✨ {library_name} Not applied")
    expect(page.get_by_text("Not applied yet")).to_be_visible()

    # The button only enables once the name differs, and that click IS the go-ahead: the rename
    # screen starts on arrival rather than asking a second time.
    name.fill("✨ {library_name} Handpicked")
    page.get_by_role("dialog").get_by_role("button", name="Rename on Plex", exact=True).click()
    expect(page.get_by_role("heading", name=re.compile("^Renaming "))).to_be_visible(timeout=LOAD)
    expect(page.get_by_role("button", name="Rename on Plex")).to_have_count(0)

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
    # this row, so its rendered title is not stable. "Every row has a way out" is also the actual
    # property — the bug was ONE card missing the control its neighbours had.
    #
    # A LINK, not a button, and in each card's "⋯" menu. "Remove from Plex" and "Delete" used to sit
    # on the card side by side with nothing saying which one loses the row's settings; the menu
    # carries one honest "Remove or delete…" that opens the editor's danger section, where that
    # difference is already written out (audit finding, Sep 2026). The 204 and the row actually
    # disappearing are covered in
    # tests/integration/test_api_collections.py::test_the_default_row_can_be_deleted_like_any_other.
    assert picked, "the seeded default row must exist for this to mean anything"
    rows = app.api("GET", "/api/collections").json()
    menus = page.get_by_role("button", name=re.compile(r"^More actions for "))
    expect(menus).to_have_count(len(rows))
    for index in range(len(rows)):
        menus.nth(index).click()
        way_out = page.get_by_role("menu").get_by_role("menuitem", name="Remove or delete…")
        expect(way_out).to_have_count(1)
        expect(way_out).to_have_attribute("href", re.compile(r"/rows/\d+#remove-this-row$"))
        page.keyboard.press("Escape")
        expect(page.get_by_role("menu")).to_have_count(0)


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
    expect(_saved_row(page, "Split Row")).to_be_visible(timeout=LOAD)

    created = next(c for c in app.api("GET", "/api/collections").json() if c["name"] == "Split Row")
    assert created["placement"] == "both"
    assert created["placement_friends"] == "home"


def test_the_pick_order_chosen_in_the_editor_reaches_the_api(page: Page, app: ShortlistApp):
    """The Order control is the only way to change how a row's titles are arranged on Plex, so its
    round trip is worth an end-to-end check: Plex offers no 'random' collection sort, and the value
    saved here is what the engine turns into the custom order it writes."""
    _open_rows(page)
    _add_a_row(page)
    page.get_by_label("Name", exact=True).fill("Shuffled Row")

    # Default first, so a control that silently ignored the click couldn't pass this.
    expect(page.get_by_role("button", name="Best match")).to_have_attribute("aria-pressed", "true")
    page.get_by_role("button", name="Shuffled").click()
    expect(page.get_by_text("different order every day")).to_be_visible()

    page.get_by_role("button", name="Add row").click()

    expect(page.get_by_text("Shuffled Row").first).to_be_visible(timeout=LOAD)
    rows = {c["slug"]: c for c in app.api("GET", "/api/collections").json()}
    assert rows["shuffled_row"]["pick_order"] == "shuffle"
    assert rows["picked"]["pick_order"] == "best", "an untouched row keeps the default"
