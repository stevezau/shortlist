"""E2E: the poster proxy and the Sharing and privacy screen, against the fake PMS and plex.tv.

Both are read-only surfaces that answer a question the owner cannot check any other way — "is that
the artwork my server actually holds" and "is everyone's row really hidden from everyone else". Both
go through the real client code here rather than through mocks that agree with us by construction.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp, build_real_rows

pytestmark = pytest.mark.e2e

LOAD = 20_000


class TestThePosterProxy:
    """`GET /api/picks/{rating_key}/poster` — the PMS's own artwork, with the token kept server-side."""

    def test_a_delivered_pick_serves_the_artwork_the_server_actually_holds(self, app: ShortlistApp, reset_fake_plex):
        state = reset_fake_plex
        build_real_rows(app)
        rating_key = next(key for collection in state.collections.values() for key in collection.item_keys)

        response = app.api("GET", f"/api/picks/{rating_key}/poster")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG"), "real image bytes, streamed from the PMS"
        # The fake serves the thumb only behind the metadata read, so a 200 here proves both hops ran.
        assert response.headers["cache-control"] == "private, max-age=604800"
        assert response.headers["etag"]

    def test_the_owners_plex_token_never_reaches_the_response(self, app: ShortlistApp, reset_fake_plex):
        state = reset_fake_plex
        build_real_rows(app)
        rating_key = next(key for collection in state.collections.values() for key in collection.item_keys)

        response = app.api("GET", f"/api/picks/{rating_key}/poster")

        # Rule 9: the whole reason this is a proxy rather than a direct link.
        assert state.owner_token not in str(response.headers)
        assert state.owner_token.encode() not in response.content

    def test_two_posters_share_one_plex_connection(self, app: ShortlistApp, reset_fake_plex):
        """Constructing a PlexServer costs a `GET /` handshake. This endpoint is called once per
        PICTURE, so rebuilding it per request made a ten-poster list pay ten handshakes on top of the
        reads it actually needed."""
        from shortlist.server.api import picks

        state = reset_fake_plex
        build_real_rows(app)
        keys = [key for collection in state.collections.values() for key in collection.item_keys][:2]
        assert len(keys) == 2, "need two distinct items to prove the client is shared"

        clients = set()
        for key in keys:
            assert app.api("GET", f"/api/picks/{key}/poster").status_code == 200
            clients.add(id(next(iter(picks._CLIENT.values()))))

        assert len(clients) == 1, "a second poster rebuilt the connection instead of reusing it"

    def test_a_rating_key_plex_does_not_hold_is_a_404_not_a_picture(self, app: ShortlistApp, reset_fake_plex):
        """A stale ratingKey must stay distinguishable from a real item with no art. Answering with a
        picture would make "why isn't X in my row" unanswerable from the UI."""
        response = app.api("GET", "/api/picks/999999/poster")

        assert response.status_code == 404
        assert not response.content.startswith(b"\x89PNG")

    def test_an_unmatched_pick_is_a_404_without_touching_plex(self, app: ShortlistApp, reset_fake_plex):
        assert app.api("GET", "/api/picks/0/poster").status_code == 404

    def test_the_second_request_is_a_304_when_the_browser_already_has_it(self, app: ShortlistApp, reset_fake_plex):
        state = reset_fake_plex
        build_real_rows(app)
        rating_key = next(key for collection in state.collections.values() for key in collection.item_keys)
        etag = app.api("GET", f"/api/picks/{rating_key}/poster").headers["etag"]

        response = app.api("GET", f"/api/picks/{rating_key}/poster", headers={"If-None-Match": etag})

        assert response.status_code == 304
        assert not response.content


class TestTheSharingScreen:
    def test_it_reports_a_healthy_server_from_a_live_read(self, app: ShortlistApp, reset_fake_plex, page: Page):
        build_real_rows(app)

        page.goto(f"{app.url}/sharing")

        expect(page.get_by_text("Sharing and privacy").first).to_be_visible(timeout=LOAD)
        expect(page.get_by_text("that aren't theirs", exact=False).first).to_be_visible(timeout=LOAD)
        # The provenance is on screen: a reading without a timestamp reads as a standing guarantee.
        expect(page.get_by_text("Read from plex.tv at", exact=False).first).to_be_visible()

    def test_it_names_an_exclude_plex_tv_really_is_missing(self, app: ShortlistApp, reset_fake_plex, page: Page):
        """The fault case, planted on the fake rather than mocked: take one `shortlist_*` label back
        off an account's filter and the screen must name the row that account can now see."""
        state = reset_fake_plex
        build_real_rows(app)
        victim = next(u for u in state.users.values() if not u.home)
        stripped = {
            field: ",".join(part for part in value.split(",") if "shortlist_" not in part.lower())
            for field, value in victim.filters.items()
        }
        assert any("shortlist_" in value.lower() for value in victim.filters.values()), (
            "the run should have written excludes for this account"
        )
        victim.filters.update(stripped)

        page.goto(f"{app.url}/sharing")

        expect(page.get_by_text("can see a row that isn't theirs", exact=False).first).to_be_visible(timeout=LOAD)
        expect(page.get_by_text("Can see:", exact=False).first).to_be_visible()

    def test_it_never_claims_coverage_of_the_collections_tab(self, app: ShortlistApp, reset_fake_plex, page: Page):
        """Rule 11: there is no recorded answer for whether Plex applies a share `label!=` filter
        outside Home, so the screen must say so rather than imply coverage it cannot back."""
        build_real_rows(app)

        page.goto(f"{app.url}/sharing")

        expect(page.get_by_text("These checks cover the Home screen", exact=False)).to_be_visible(timeout=LOAD)
        expect(page.get_by_text("no way to confirm what Plex does on the Collections tab", exact=False)).to_be_visible()

    def test_the_users_page_links_to_it_rather_than_burying_it_in_settings(
        self, app: ShortlistApp, reset_fake_plex, page: Page
    ):
        page.goto(f"{app.url}/users")

        link = page.get_by_role("link", name="Sharing and privacy")
        expect(link).to_be_visible(timeout=LOAD)
        link.click()

        expect(page).to_have_url(f"{app.url}/sharing", timeout=LOAD)
