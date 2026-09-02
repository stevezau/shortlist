"""E2E: a title held in two Plex libraries is ONE row on the Watched page (issue #111).

`watched_titles` is unique on `(user, section_key, rating_key)`, so the same film in "Movies" and
"4K Movies" is two cached rows — and the page listed it twice, with two Block buttons that both sent
the same TMDB id. The merge happens in SQL, the library NAME is recorded by the sync and by nothing
else, and the filter is built from the page's own response; a unit test can pin any one of those
while the chain still fails. This walks the whole thing: two library copies on the fake PMS, through
the real sync job, into rendered pixels.
"""

from __future__ import annotations

import re
from dataclasses import replace

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp
from tests.e2e.test_ratings_e2e import _sync_and_open_history
from tests.fakes.fake_plex import FakeHistoryEntry, FakePlexState

pytestmark = pytest.mark.e2e

#: One of sarah's watched movies in `seed_state`, and the ratingKey its second copy gets. RatingKeys
#: are server-unique, so the copy must have its own — while carrying the SAME tmdb_id, which is what
#: makes the two rows one title.
DUPLICATED_TITLE = "Movie 03"
ORIGINAL_KEY = 103
COPY_KEY = 103_000
#: What `seed_state` gives Movie 03 — both copies carry it, which is what makes them one title.
MOVIE_03_TMDB_ID = 9003


def _hold_movie_03_in_two_libraries(state: FakePlexState) -> None:
    """Add a "4K Movies" library holding a second copy of Movie 03, and have sarah watch it."""
    fourk = state.add_section(key=3, kind="movie", title="4K Movies")
    fourk.items[COPY_KEY] = replace(state.movies[ORIGINAL_KEY], rating_key=COPY_KEY)
    # Watched EARLIER than the original, so a merge that kept the wrong copy would show the wrong
    # date — and so the copy is not simply the newest row either way.
    state.history.append(FakeHistoryEntry(account_id=201, rating_key=COPY_KEY, viewed_at=1_700_000_000))


class TestATitleInTwoLibraries:
    def test_it_is_one_row_naming_both_libraries(self, page: Page, app: ShortlistApp, fake_plex):
        _, _, state = fake_plex
        _hold_movie_03_in_two_libraries(state)

        _sync_and_open_history(page, app)

        expect(page.get_by_text(DUPLICATED_TITLE, exact=True)).to_have_count(1, timeout=15_000)
        # Scoped to the row: every other film on this server also carries a "Movies" tag.
        row = page.locator("li").filter(has_text=DUPLICATED_TITLE)
        expect(row.get_by_title("4K Movies")).to_be_visible()
        expect(row.get_by_title("Movies", exact=True)).to_be_visible()

    def test_a_title_in_one_library_still_names_it(self, page: Page, app: ShortlistApp, fake_plex):
        """The library line is on every row, not only the duplicated ones — otherwise the column
        appears and disappears down the list.

        Scoped to the ROW. A bare `get_by_text("Movies")` also matches the media-type filter's own
        "Movies" button, which sits above the list and is always there — so it passed with the
        library line rendering nothing at all.
        """
        _, _, state = fake_plex
        _hold_movie_03_in_two_libraries(state)

        _sync_and_open_history(page, app)
        row = page.locator("li").filter(has_text="Movie 04")

        expect(row).to_have_count(1, timeout=15_000)
        expect(row.get_by_title("Movies", exact=True)).to_be_visible()

    def test_no_library_filter_appears_on_a_one_library_per_type_server(self, page: Page, app: ShortlistApp, fake_plex):
        """`seed_state`'s default layout — one "Movies", one "TV Shows" — is the common server, and
        the one the maintainer runs. A library dropdown there offers the same two words as the
        Movies/Shows buttons next to it, so it must not be rendered at all.

        The library NAME on each row stays, though. An earlier build extended the dropdown's rule to
        the rows as well and so hid the tag on exactly this layout — which is most servers, and is
        the feature the issue asked for. The two are pinned together here so they cannot drift apart
        again.
        """
        _sync_and_open_history(page, app)
        row = page.locator("li").filter(has_text="Movie 04")

        expect(row).to_have_count(1, timeout=15_000)
        expect(row.get_by_title("Movies", exact=True)).to_be_visible()
        expect(page.get_by_label(re.compile("filter by library", re.IGNORECASE))).to_have_count(0)

    def test_the_filter_offers_the_libraries_and_narrows_to_one(self, page: Page, app: ShortlistApp, fake_plex):
        """And the surviving row still names BOTH libraries — filtering selects titles, it does not
        relabel them, so "4K Movies · Movies" under a 4K-only filter is the duplicate you went
        looking for."""
        _, _, state = fake_plex
        _hold_movie_03_in_two_libraries(state)

        _sync_and_open_history(page, app)
        library_filter = page.get_by_label(re.compile("filter by library", re.IGNORECASE))
        expect(library_filter).to_be_visible(timeout=15_000)
        library_filter.select_option("4K Movies")
        page.wait_for_timeout(1500)

        expect(page.get_by_text(DUPLICATED_TITLE, exact=True)).to_have_count(1)
        expect(page.get_by_title("4K Movies")).to_be_visible()
        expect(page.get_by_title("Movies", exact=True)).to_be_visible()
        # Every other title sarah watched lives only in "Movies" or "TV Shows", so the filter left
        # exactly one row — proof it narrowed rather than merely reordering.
        expect(page.get_by_text("Movie 04", exact=True)).to_have_count(0)


class TestBlockingAMergedRow:
    """The claim the merge rests on: a block needs no library, so one button is enough.

    A block is stored per person as a bare TMDB id — no section, no Plex write — and the merged row
    is grouped BY that id, so there is exactly one to send. Before the merge this row was two rows
    with two Block buttons that both sent the same id, and pressing either greyed out both. This
    walks the button: click it on the merged row, and check both the screen and what was stored.
    """

    def test_one_button_blocks_the_title_in_every_library(self, page: Page, app: ShortlistApp, fake_plex):
        _, _, state = fake_plex
        _hold_movie_03_in_two_libraries(state)
        _sync_and_open_history(page, app)
        row = page.locator("li").filter(has_text=DUPLICATED_TITLE)
        expect(row).to_have_count(1, timeout=15_000)
        expect(row.get_by_title("4K Movies")).to_be_visible()

        row.get_by_role("button", name=re.compile(f"Block {DUPLICATED_TITLE}", re.IGNORECASE)).click()

        # On screen: the one row now reads "blocked", and no Block button survived anywhere for it.
        expect(row.get_by_text("blocked")).to_be_visible(timeout=10_000)
        expect(page.get_by_role("button", name=re.compile(f"Block {DUPLICATED_TITLE}", re.IGNORECASE))).to_have_count(0)

        # And in storage: ONE entry, carrying the TMDB id both library copies share. Two entries — or
        # one keyed on a ratingKey — would mean the block was per copy after all.
        sarah = next(u for u in app.api("GET", "/api/users").json() if u["username"] == "sarah")
        blocked = sarah["prefs"]["blocked_seeds"]
        assert [(entry["tmdb_id"], entry["title"]) for entry in blocked] == [(MOVIE_03_TMDB_ID, DUPLICATED_TITLE)]
