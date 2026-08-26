"""The language preference, driven through the real screens.

Both surfaces are round-tripped through the API rather than asserted on the DOM alone: the point of
the control is the value that reaches a run, so a control that renders and saves nothing would pass a
render-only check.

The case worth driving through a browser rather than a unit test is the DERIVED bar. It has three
parts that only meet on screen — the field shows a number nobody typed, that number moves when the
owner changes their minimum rating, and it must still save as `null` so it keeps following. Any two
of those can be right while the third is wrong, and the result reads as a setting that works.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

LOAD = 15_000
OTHER_BAR = re.compile("Minimum .* rating, other languages", re.I)


def _open_row_requests(page: Page) -> None:
    """The default per-person row's editor, with its Requests group expanded (collapsed by default)."""
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Edit").first.click()
    expect(page.get_by_role("heading", name="Edit row")).to_be_visible(timeout=LOAD)
    page.locator("details:has(> summary:has-text('Requests')) > summary").click()


def _enable_requests(app: ShortlistApp, **extra) -> None:
    app.api("PUT", "/api/settings", json={"values": {"requests.enabled": True, **extra}})


def test_it_ships_on_any_language_so_an_upgrade_changes_nothing(page: Page, app: ShortlistApp):
    _enable_requests(app)
    page.goto("/settings")
    expect(page.get_by_role("button", name="Any language")).to_have_attribute("aria-pressed", "true", timeout=LOAD)
    # Neither the language list nor the second bar is read on "any", and a number on screen that
    # nothing applies reads as a bar that is in force.
    expect(page.get_by_label("Add a language")).to_have_count(0)
    expect(page.get_by_label(OTHER_BAR)).to_have_count(0)
    assert app.api("GET", "/api/settings").json()["requests.language_mode"] == "any"


def test_the_second_bar_follows_the_owners_own_floor_and_saves_as_null(page: Page, app: ShortlistApp):
    """The whole reason the default is derived rather than a constant: it has to carry the OWNER's
    taste. Saving the derived number would freeze it, so raising the minimum rating later would
    silently stop moving it — the field would still say "following" while doing nothing of the sort.
    """
    _enable_requests(app, **{"requests.min_rating": 7.0})
    page.goto("/settings")

    page.get_by_role("button", name="Prefer these").click()

    bar = page.get_by_label(OTHER_BAR)
    expect(bar).to_have_value("8.5", timeout=LOAD)  # 7.0 + 1.5, shown without anyone typing it
    expect(page.get_by_text(re.compile("Following your minimum rating", re.I))).to_be_visible()

    expect(page.get_by_text(re.compile("Saved|Saving", re.I)).first).to_be_visible(timeout=LOAD)
    page.wait_for_timeout(2000)

    saved = app.api("GET", "/api/settings").json()
    assert saved["requests.language_mode"] == "prefer"
    assert saved["requests.min_rating_other"] is None, "the bar must keep following, not be pinned"

    # Move the floor the bar derives from; the bar must move with it, still without being stored.
    minimum = page.get_by_label(re.compile(r"^Minimum .* rating$", re.I))
    minimum.fill("6")
    expect(page.get_by_label(OTHER_BAR)).to_have_value("7.5", timeout=LOAD)
    page.wait_for_timeout(2000)
    assert app.api("GET", "/api/settings").json()["requests.min_rating_other"] is None


def test_typing_a_bar_stops_it_following_and_it_can_be_put_back(page: Page, app: ShortlistApp):
    _enable_requests(app, **{"requests.min_rating": 7.0, "requests.language_mode": "prefer"})
    page.goto("/settings")

    bar = page.get_by_label(OTHER_BAR)
    expect(bar).to_have_value("8.5", timeout=LOAD)
    bar.fill("9.2")
    page.wait_for_timeout(2000)
    assert app.api("GET", "/api/settings").json()["requests.min_rating_other"] == 9.2

    page.get_by_role("button", name=re.compile("Follow my minimum rating again", re.I)).click()
    page.wait_for_timeout(2000)
    assert app.api("GET", "/api/settings").json()["requests.min_rating_other"] is None
    expect(page.get_by_label(OTHER_BAR)).to_have_value("8.5")


def test_only_mode_hides_the_bar_and_warns_on_an_empty_list(page: Page, app: ShortlistApp):
    _enable_requests(app)
    page.goto("/settings")

    page.get_by_role("button", name="Only these").click()
    # No rating can rescue a title in "only" mode, so offering a rating bar would be offering a
    # control that cannot change the outcome.
    expect(page.get_by_label(OTHER_BAR)).to_have_count(0)

    page.get_by_role("button", name=re.compile("Remove English", re.I)).click()
    expect(page.get_by_text(re.compile("will never ask for anything", re.I))).to_be_visible()

    page.wait_for_timeout(2000)
    saved = app.api("GET", "/api/settings").json()
    assert saved["requests.language_mode"] == "only"
    assert saved["requests.preferred_languages"] == []


def test_adding_a_language_saves_it(page: Page, app: ShortlistApp):
    _enable_requests(app, **{"requests.language_mode": "prefer"})
    page.goto("/settings")

    page.get_by_label("Add a language").select_option("ja", timeout=LOAD)
    page.wait_for_timeout(2000)
    assert app.api("GET", "/api/settings").json()["requests.preferred_languages"] == ["en", "ja"]

    page.reload()
    expect(page.get_by_text("Japanese")).to_be_visible(timeout=LOAD)


def test_a_row_can_be_stricter_than_the_server(page: Page, app: ShortlistApp):
    _enable_requests(app, **{"requests.min_rating": 7.3, "requests.language_mode": "prefer"})
    _open_row_requests(page)

    inherit = page.get_by_label("Use the global language setting for this row")
    expect(inherit).to_be_checked(timeout=LOAD)
    # While inheriting, the row names the policy AND the number it derives — 7.3 + 1.5 = 8.8.
    expect(page.get_by_text(re.compile(r"prefer English, others need 8\.8"))).to_be_visible()

    inherit.uncheck()
    mode = page.locator("#row-req-language-mode")
    expect(mode).to_be_visible()
    # Seeded to something that DOES a thing (not "any"), but NOT to "only" — flipping a toggle to
    # see what a control does must not land on the one mode that discards titles.
    expect(mode).to_have_value("prefer")

    mode.select_option("only")
    page.get_by_role("button", name="Save changes").click()
    page.wait_for_timeout(2000)

    rows = {c["slug"]: c for c in app.api("GET", "/api/collections").json()}
    assert rows["picked"]["req_language_mode"] == "only"
    assert rows["picked"]["req_preferred_languages"] == ["en"]

    _open_row_requests(page)
    expect(page.locator("#row-req-language-mode")).to_have_value("only", timeout=LOAD)


def test_clearing_the_row_override_puts_it_back_on_the_global(page: Page, app: ShortlistApp):
    """NULL is the inherit signal, so "use the global again" must store null on ALL THREE fields —
    not just the mode. A stale language list left behind is a row that reads as inheriting on screen
    while the run still resolves its own values."""
    _enable_requests(app, **{"requests.language_mode": "prefer"})
    picked = next(c for c in app.api("GET", "/api/collections").json() if c["slug"] == "picked")
    resp = app.api(
        "PATCH",
        f"/api/collections/{picked['id']}",
        json={
            "name": picked["name"],
            "req_language_mode": "only",
            "req_preferred_languages": ["ja"],
            "req_min_rating_other": 9.0,
        },
    )
    assert resp.status_code == 200, resp.text

    _open_row_requests(page)
    inherit = page.get_by_label("Use the global language setting for this row")
    expect(inherit).not_to_be_checked(timeout=LOAD)
    inherit.check()

    page.get_by_role("button", name="Save changes").click()
    page.wait_for_timeout(2000)

    row = {c["slug"]: c for c in app.api("GET", "/api/collections").json()}["picked"]
    assert row["req_language_mode"] is None
    assert row["req_preferred_languages"] is None
    assert row["req_min_rating_other"] is None
