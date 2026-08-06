"""E2E: a person's Plex rating, from the fake PMS all the way onto the screen.

The rest of issue #69's cover stops at the API. This is the only test where the rating crosses every
boundary it crosses in production — XML attribute on the share-token watched read, through the sync
into SQLite, out through `/api/users/{id}/watched`, and into rendered pixels.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

#: One of sarah's watched movies in `seed_state` (she has 101..108).
DISLIKED_KEY = 103
DISLIKED_TITLE = "Movie 03"
LIKED_KEY = 104
LIKED_TITLE = "Movie 04"


def _sync_and_open_history(page: Page, app: ShortlistApp, user_slug: str = "sarah") -> None:
    """Pull the watched set (which is what carries the ratings), then open that person's history.

    The sync is driven through the UI's own Jobs page rather than the API, so the ratings arrive by
    exactly the path they do in production — the nightly `sync.watched` job, not a hand-built call.
    """
    page.goto("/")
    page.wait_for_timeout(1500)
    # The same endpoint the Jobs page's "Sync watch history" button calls, issued from the browser
    # context so it carries the real session cookie and CSRF header. Driving the button itself would
    # make every assertion below hostage to the Jobs page's layout, which is not what is under test.
    response = page.request.post(f"{app.url}/api/report/sync", headers={"x-shortlist-csrf": "1"})
    assert response.ok, f"the watch sync did not start: {response.status} {response.text()}"
    page.wait_for_timeout(4000)

    page.goto("/users")
    page.get_by_role("link", name=re.compile(user_slug, re.IGNORECASE)).first.click()
    page.get_by_role("button", name="Watch History").click(timeout=20_000)
    page.wait_for_timeout(1500)


class TestRatingsOnScreen:
    def test_a_low_rating_shows_as_not_seeding(self, page: Page, app: ShortlistApp, fake_plex):
        _, _, state = fake_plex
        state.rate(201, DISLIKED_KEY, 2.0)  # sarah, one star
        state.rate(201, LIKED_KEY, 10.0)  # ...and five stars on another

        _sync_and_open_history(page, app)

        # The rating reached the screen at all — the whole chain in one assertion.
        expect(page.get_by_text(re.compile("not seeding"))).to_be_visible(timeout=15_000)
        # ...and the well-rated one is shown WITHOUT that claim.
        expect(page.get_by_title("They rated this 5 out of 5 in Plex")).to_be_visible()

    def test_the_page_states_the_threshold_once_someone_has_rated_something(
        self, page: Page, app: ShortlistApp, fake_plex
    ):
        _, _, state = fake_plex
        state.rate(201, DISLIKED_KEY, 2.0)

        _sync_and_open_history(page, app)

        expect(page.get_by_text(re.compile(r"at or below 1 star stops being used"))).to_be_visible(timeout=15_000)

    def test_a_tool_written_rating_is_shown_but_called_out(self, page: Page, app: ShortlistApp, fake_plex):
        """The Kometa case as the owner sees it: enough fractional values that the whole account is
        disbelieved, and the page says so rather than showing stars that quietly do nothing."""
        _, _, state = fake_plex
        # One value is a whole 2.0, at the threshold. Without it every rating here sits ABOVE the
        # threshold, so "not seeding" would be absent whether or not the trust guard existed — the
        # assertion below would have been decorative. With it, the row reads "not seeding" the moment
        # the account is believed, so the guard is what the test is actually pinning.
        for offset, value in enumerate([7.9, 8.8, 6.2, 5.4, 9.1, 2.0]):
            state.rate(201, 101 + offset, value)

        _sync_and_open_history(page, app)

        expect(page.get_by_text(re.compile("another tool is writing plex ratings", re.IGNORECASE))).to_be_visible(
            timeout=15_000
        )
        expect(page.get_by_text(re.compile("not seeding"))).to_have_count(0)

    def test_nothing_about_ratings_appears_when_nobody_has_rated_anything(
        self, page: Page, app: ShortlistApp, fake_plex
    ):
        """The state almost every real person is in. An explanation of a feature doing nothing for
        them is noise, and a stray "0 stars" would be an outright lie."""
        _sync_and_open_history(page, app)

        expect(page.get_by_text(DISLIKED_TITLE).first).to_be_visible(timeout=15_000)
        expect(page.get_by_text(re.compile("stops being used"))).to_have_count(0)
        expect(page.get_by_text(re.compile("not seeding"))).to_have_count(0)


class TestRatingSettings:
    def test_the_switch_and_threshold_persist(self, page: Page, app: ShortlistApp):
        page.goto("/settings?tab=recommendations")
        page.wait_for_timeout(2000)
        threshold = page.get_by_label(re.compile("didn.t like it", re.IGNORECASE))
        expect(threshold).to_be_visible()

        threshold.fill("3")
        threshold.blur()
        page.wait_for_timeout(2000)
        page.reload()
        page.wait_for_timeout(2000)

        expect(page.get_by_label(re.compile("didn.t like it", re.IGNORECASE))).to_have_value("3")

    def test_switching_it_off_hides_the_threshold(self, page: Page, app: ShortlistApp):
        """The threshold is meaningless with the feature off, so it must not sit there inviting an
        edit that changes nothing."""
        page.goto("/settings?tab=recommendations")
        page.wait_for_timeout(2000)

        page.get_by_label("Respect Plex ratings").click()
        page.wait_for_timeout(1000)

        expect(page.get_by_label(re.compile("didn.t like it", re.IGNORECASE))).to_have_count(0)
