"""E2E: the owner's "I see everyone's rows" guide, in a real browser against the fake Plex server.

This page is the one an owner reaches from three different warnings, and its first job is to be
READABLE — so the value here is that the whole flow renders and its actions round-trip, not that any
single string is present.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e


class TestWatchingAccountGuide:
    def test_the_guide_renders_all_three_options(self, page: Page, app: ShortlistApp):
        page.goto("/watching-account")

        expect(page.get_by_role("heading", name=re.compile("You see everyone", re.I))).to_be_visible(timeout=20_000)
        expect(page.get_by_text(re.compile("Take the rows off the library shelf", re.I))).to_be_visible()
        expect(page.get_by_text(re.compile("I don't mind seeing them", re.I))).to_be_visible()
        expect(page.get_by_text(re.compile("Move my watching to a separate account", re.I))).to_be_visible()

    def test_setting_it_up_reveals_the_transfer_step(self, page: Page, app: ShortlistApp):
        page.goto("/watching-account")
        page.get_by_role("button", name=re.compile("Set it up", re.I)).click()

        expect(page.get_by_role("heading", name=re.compile("Set up the watching account", re.I))).to_be_visible()
        # The honest bit that has to survive a skim: Plex cannot record the original watch dates.
        expect(page.get_by_text(re.compile("watched today", re.I))).to_be_visible()

    # The equivalent "the warnings link here" check lives in vitest (`owner-note.test.tsx`), not
    # here: the e2e fixture seeds only managed and shared users (`conftest.py:303`), so no owner row
    # exists and `OwnerNote` — which is what carries the link on the Users page — never renders.


class TestWatchHistorySearch:
    def test_the_watch_history_panel_searches_server_side(self, page: Page, app: ShortlistApp):
        """Proves the /api/users/{id}/watched contract round-trips through the real SPA — a wrong
        query-string or response shape passes both unit suites and dies here."""
        page.goto("/users")
        page.get_by_role("link", name=re.compile("sarah", re.I)).first.click()
        # The user-detail tabs are a `Segmented` control — aria-pressed buttons, not role=tab.
        page.get_by_role("button", name=re.compile("^watched$", re.I)).click()

        search = page.get_by_label(re.compile("Search watched titles", re.I))
        expect(search).to_be_visible(timeout=20_000)
        search.fill("nothing-matches-this")

        expect(page.get_by_text(re.compile("Nothing matches that", re.I))).to_be_visible(timeout=10_000)
