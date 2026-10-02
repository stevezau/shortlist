"""E2E: a season of the owner's own, added from the Seasonal row editor (issue #137).

Full stack: the built SPA and the real API, the fake PMS (the film search and the library scan a count
reads) and the fake TMDB (a tag's list, read to its last page). The editor counts a season on the server
before anything is saved, so this is the one place the count, the film search, the save and the row that
ticks the season are seen working together.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import FAKE_TMDB_TAGS, THANKSGIVING_DINNER_TAG, THANKSGIVING_TAG, ShortlistApp
from tests.fakes.fake_plex import FakePlexState

pytestmark = pytest.mark.e2e

LOAD = 20_000
#: The first count scans every library before it can answer.
COUNT = 60_000
BUILT_INS = ("valentines", "halloween", "christmas")


def _films_counted(total: int) -> re.Pattern[str]:
    """ "7 films in your libraries", and not "17 films …": the count sits flush against the text before it."""
    return re.compile(rf"(?<!\d){total}\s*films? in your libraries")


def test_a_ready_made_season_with_a_film_picked_by_hand_is_saved_and_ticked_in_a_new_seasonal_row(
    page: Page, app: ShortlistApp, reset_fake_plex: FakePlexState
):
    thanksgiving = FAKE_TMDB_TAGS[THANKSGIVING_TAG]
    tagged_here = len(thanksgiving.movies_in_library) + len(thanksgiving.shows_in_library)
    # Searched by the middle of its title, and listed past the first ten: a search that matched only from
    # the start, or a library that ignored `title=` and served its first page, would not find it.
    pick = next(movie for movie in reset_fake_plex.movies.values() if movie.title == "Nightcrawler")
    first_page = sorted(reset_fake_plex.movies)[:10]
    assert pick.rating_key not in first_page
    assert pick.tmdb_id not in thanksgiving.movies_in_library, "the pick must add a film the tag doesn't"

    # 1. A new Seasonal row, from its template, ticks the three built-in seasons and nothing else.
    page.goto("/rows")
    expect(page.get_by_role("heading", name="Rows", exact=True)).to_be_visible(timeout=LOAD)
    page.get_by_role("button", name="Add a row").click()
    page.get_by_role("group", name="Templates", exact=True).get_by_role("button", name=re.compile(r"^Seasonal")).click()
    page.get_by_role("button", name="Use template").click()
    expect(page.get_by_role("heading", name="Add a row")).to_be_visible(timeout=LOAD)
    page.locator('details[data-settings-group="Row settings"] > summary').click()

    seasons = page.locator("li[data-season]")
    expect(seasons).to_have_count(len(BUILT_INS), timeout=LOAD)
    assert {item.get_attribute("data-season") for item in seasons.all()} == set(BUILT_INS)
    for item in seasons.all():
        expect(item).to_contain_text("Built in")
        expect(item.get_by_role("checkbox")).to_be_checked()

    # 2. "Add more seasons" is open on a row that ticks only built-ins. Each card counts its own films.
    more = page.locator("summary", has_text="Add more seasons").locator("..")
    expect(more).to_have_attribute("open", "")
    card = (
        page.get_by_role("list", name="Ready-made seasons").get_by_role("listitem").filter(has_text="Thanksgiving (US)")
    )
    expect(card).to_contain_text(_films_counted(tagged_here), timeout=COUNT)
    card.get_by_role("button", name="Add Thanksgiving (US)").click()

    dialog = page.get_by_role("dialog", name="Add Thanksgiving (US)")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label("Season name", exact=True)).to_have_value("Thanksgiving")
    expect(dialog.get_by_label("Emoji", exact=True)).to_have_value("🦃")

    # 3. The server's count: every tagged film and show the libraries hold, and none of the ones they don't.
    summary = dialog.get_by_role("complementary", name="This season draws from")
    expect(summary).to_contain_text(_films_counted(tagged_here), timeout=COUNT)
    expect(dialog.get_by_role("list", name="Chosen tags")).to_contain_text(
        f"thanksgiving {tagged_here} in your libraries"
    )

    # The tag search reads TMDB's tags and each one's film count; the tag already chosen can't be added twice.
    dialog.get_by_label("Search TMDB tags").fill("thanks")
    tags_found = dialog.get_by_role("list", name="TMDB tags found")
    chosen_tag = tags_found.get_by_role("listitem").filter(has_text=re.compile(r"^thanksgiving —"))
    expect(chosen_tag).to_contain_text(f"{len(thanksgiving.movies_in_library) + thanksgiving.movies_elsewhere} films")
    expect(chosen_tag.get_by_role("button", name="Added")).to_be_disabled()
    dinner = FAKE_TMDB_TAGS[THANKSGIVING_DINNER_TAG]
    expect(tags_found.get_by_role("button", name=f"Add tag {dinner.name}")).to_be_enabled()
    dialog.get_by_label("Search TMDB tags").fill("")

    # A film found by part of its title, picked by hand: one more film, under its own name in the sample.
    dialog.get_by_label("Search your libraries").fill("crawl")
    dialog.get_by_role("button", name=f"Add {pick.title} ({pick.year})", exact=True).click()
    expect(dialog.get_by_role("list", name="Picked films")).to_contain_text(f"{pick.title} ({pick.year})")
    expect(summary).to_contain_text(_films_counted(tagged_here + 1), timeout=COUNT)
    expect(summary).to_contain_text("Picked by hand+1 more")
    expect(summary.get_by_role("listitem").filter(has_text=pick.title)).to_be_visible()

    dialog.get_by_role("button", name="Save and add to this row").click()
    expect(dialog).to_be_hidden(timeout=LOAD)

    # 4. Saved for the server and ticked here, as the owner's own; its card has gone from the ready-made ones.
    expect(page.get_by_role("status").filter(has_text="Saved “🦃 Thanksgiving” and ticked it here.")).to_be_visible()
    mine = page.locator('li[data-season="thanksgiving"]')
    expect(mine).to_contain_text("Yours")
    expect(mine.get_by_role("checkbox", name="Show Thanksgiving in this row")).to_be_checked()
    expect(mine.get_by_role("checkbox", name="Show Thanksgiving in this row")).to_be_focused()
    expect(mine).to_contain_text(re.compile(rf"(?<!\d){tagged_here + 1} films"), timeout=COUNT)
    expect(page.get_by_role("button", name="Add Thanksgiving (US)")).to_have_count(0)

    page.get_by_role("button", name="Add row").click()
    expect(page).to_have_url(re.compile(r"/rows$"), timeout=LOAD)

    # 5. The row follows it, and the season names the row as one that uses it.
    row = next(c for c in app.api("GET", "/api/collections").json() if "thanksgiving" in c["seasons"])
    assert set(row["seasons"]) == {*BUILT_INS, "thanksgiving"}
    season = next(s for s in app.api("GET", "/api/seasons").json() if s["slug"] == "thanksgiving")
    assert season["used_by"] == [{"id": row["id"], "name": row["name"]}]
    assert (season["name"], season["emoji"], season["builtin"], season["preset"]) == (
        "Thanksgiving",
        "🦃",
        False,
        "thanksgiving_us",
    )
    assert season["tags"] == [{"id": THANKSGIVING_TAG, "name": "thanksgiving"}]
    assert season["picks"] == [{"tmdb_id": pick.tmdb_id, "media_type": "movie", "title": pick.title, "year": pick.year}]
