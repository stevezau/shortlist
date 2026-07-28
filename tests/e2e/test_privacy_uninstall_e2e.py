"""E2E: row privacy (each user's row hidden from everyone else) and the uninstall that undoes everything.

These are the two promises Shortlist makes that a user cannot verify for themselves: "your rows
are private" and "I can put your server back". Both are checked here against a real server
(the fake PMS/plex.tv), not against mocks that agree with us by construction.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp, build_real_rows

pytestmark = pytest.mark.e2e

LOAD = 20_000
SLOW = 60_000


class TestRowsStayPrivateAcrossLibraries:
    """The promise of the product, end to end: after a real run, no user can see another
    user's row — in ANY library.

    This is the shape of the only leak that ever reached a live server (SFLIX, 2026-07-12).
    Every user's picks were delivered into the movie library regardless of type, so the TV
    watchers' rows sat in a movie library holding shows. Plex applies `label!=` share filters
    per library, and a collection whose contents don't match its library is matched by neither
    filterMovies nor filterTelevision — so those rows were unhidable, and every user saw them.
    The excludes really were on the filters the whole time; only looking at a real user's own
    Home hubs caught it.
    """

    def test_no_user_sees_another_users_row_in_any_library(self, app: ShortlistApp, reset_fake_plex):
        state = reset_fake_plex
        build_real_rows(app)

        owned = {}  # slug -> collection ids, from the labels the PMS actually stored
        for collection in state.collections.values():
            for label in collection.labels:
                if label.lower().startswith("shortlist_"):
                    owned.setdefault(label.lower().removeprefix("shortlist_"), []).append(collection.rating_key)

        assert set(owned) == {"sarah", "mike", "canary"}
        # sarah watches movies AND TV, so she has a row in each library — the case that leaked.
        assert len(owned["sarah"]) == 2, "a both-types watcher must get one row per library"
        libraries = {state.collections[key].section_id for key in owned["sarah"]}
        assert libraries == {state.section_id, state.show_section_id}

        for slug, ids in owned.items():
            for key in ids:
                collection = state.collections[key]
                assert state.filterable(collection), (
                    f"{slug}'s row in library {collection.section_id} holds items of the wrong type: "
                    "no share filter can hide it, so every user can see it"
                )

        # Now look through each user's OWN eyes: their row, and nobody else's.
        for account_id, slug in ((201, "sarah"), (202, "mike"), (203, "canary")):
            hubs = app.plex_hubs_as(account_id)
            visible = {
                int(match.group(1))
                for hub in hubs
                if (match := re.search(r"/library/collections/(\d+)", str(hub.get("key") or "")))
            }
            assert set(owned[slug]) <= visible, f"{slug} cannot see their own row"
            foreign = {key: other for other, ids in owned.items() if other != slug for key in ids}
            leaked = {key: foreign[key] for key in visible & set(foreign)}
            assert not leaked, f"{slug} can see {sorted(set(leaked.values()))}'s row ({leaked})"


class TestUninstall:
    def test_uninstall_puts_the_server_back_as_shortlist_found_it(self, page: Page, app: ShortlistApp, reset_fake_plex):
        """The typed-confirmation path, all the way through: rows deleted, filters restored."""
        state = reset_fake_plex
        build_real_rows(app)
        # 5 rows for 3 users: sarah and the cold-start canary each get one per library; mike watches only TV.
        assert len(state.collections) == 5
        assert state.users[201].filters["filterMovies"] == "label!=Shortlist_canary,Shortlist_mike"

        # Uninstall is its own page now (with a live per-step log), reached from the Danger Zone link.
        page.goto("/settings")
        page.get_by_role("link", name="Uninstall Shortlist…").click()
        expect(page.get_by_role("heading", name="Uninstall Shortlist")).to_be_visible(timeout=LOAD)

        commit = page.get_by_role("button", name="Uninstall and restore server")
        expect(commit).to_be_disabled()
        page.get_by_role("textbox").fill("uninstall shortlist")
        expect(commit).to_be_enabled()
        commit.click()

        expect(page.get_by_text("Uninstall complete")).to_be_visible(timeout=SLOW)

        assert state.collections == {}, "a Shortlist collection survived the uninstall"
        for user in state.users.values():
            assert user.filters["filterMovies"] == "", f"{user.username}'s share filter was not restored"
            assert user.filters["filterTelevision"] == ""

    def test_uninstall_leaves_collections_shortlist_does_not_own_alone(
        self, page: Page, app: ShortlistApp, reset_fake_plex
    ):
        """Kometa coexistence (plex-safety rule 4): only shortlist_* labelled collections may go."""
        from tests.fakes.fake_plex import FakeCollection

        state = reset_fake_plex
        build_real_rows(app)
        foreign = FakeCollection(
            rating_key=9999,
            title="Kometa: Best of the 90s",
            section_id=state.section_id,
            labels=["Kometa"],
            item_keys=[101, 102],
        )
        state.collections[9999] = foreign

        page.goto("/settings")
        page.get_by_role("link", name="Uninstall Shortlist…").click()
        expect(page.get_by_role("heading", name="Uninstall Shortlist")).to_be_visible(timeout=LOAD)

        page.get_by_role("button", name="Preview what would change").click()
        # The preview summary counts the 5 Shortlist rows that would go — and never the Kometa one.
        expect(page.locator("body")).to_contain_text("5 collections", timeout=SLOW)
        expect(page.locator("body")).not_to_contain_text("Kometa")

        page.get_by_role("textbox").fill("uninstall shortlist")
        page.get_by_role("button", name="Uninstall and restore server").click()
        expect(page.get_by_text("Uninstall complete")).to_be_visible(timeout=SLOW)

        assert list(state.collections) == [9999], "uninstall deleted a collection Shortlist did not create"
        assert state.collections[9999].item_keys == [101, 102]


class TestEveryAccountOnTheServerIsCovered:
    """Shortlist's promise is that a user's row is private — from EVERYONE, not from the handful of
    accounts Shortlist happens to manage.

    On a live server this was not true: 45 of its 48 accounts had completely empty share filters
    and could see all three managed users' private rows, because Shortlist only ever wrote filters
    for the users it built rows for (SFLIX, 2026-07-12).
    """

    def test_an_account_shortlist_has_never_seen_still_gets_the_excludes(self, app: ShortlistApp, reset_fake_plex):
        """The owner invites someone to Plex and never opens the Users page. The nightly run must
        still stop them seeing everyone else's rows — and must record what it changed on their
        share (plex-safety rule 10)."""
        from tests.fakes.fake_plex import FakeUser

        state = reset_fake_plex
        # A stranger: on the Plex server, absent from Shortlist's database entirely.
        state.users[299] = FakeUser(id=299, username="stranger")

        build_real_rows(app)

        # Their share filter now excludes every row that isn't theirs...
        stranger = state.users[299]
        assert "Shortlist_sarah" in stranger.filters["filterMovies"]
        assert "Shortlist_sarah" in stranger.filters["filterTelevision"]
        assert "Shortlist_mike" in stranger.filters["filterMovies"]

        # ...they see none of those rows on their own Home...
        hubs = app.plex_hubs_as(299)
        visible = {
            int(match.group(1))
            for hub in hubs
            if (match := re.search(r"/library/collections/(\d+)", str(hub.get("key") or "")))
        }
        assert not visible, f"a stranger can see {len(visible)} of other people's rows"

        # ...and the change to their share is on the record, with the before/after.
        # (/api/events is the live SSE stream; /api/events/log is the audit table.)
        events = app.api("GET", "/api/events/log?scope=run.privacy_sync").json()
        writes = [e for e in events if e["message"]["username"] == "stranger"]
        assert writes, "editing someone's Plex share permissions must never go unaudited"
        fields = writes[0]["message"]["fields"]
        assert fields["filterMovies"]["before"] == ""
        assert "Shortlist_sarah" in fields["filterMovies"]["after"]

    def test_a_completed_run_is_visible_on_the_run_page(self, page: Page, app: ShortlistApp, reset_fake_plex):
        """ "What changed on whose share at 03:31" must be answerable from the UI, not just the
        database — so every real run has its own page."""
        run = build_real_rows(app)

        page.goto(f"/runs/{run['id']}")
        expect(page.get_by_role("heading", name=f"Run #{run['id']}")).to_be_visible(timeout=LOAD)
