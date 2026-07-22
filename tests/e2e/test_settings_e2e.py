"""E2E: the Settings page — connection tests, defaults, schedule, and the way out.

Settings is where an owner goes when something is wrong, so every control here has to tell the
truth: a Test button that says "Connected" when it isn't, or a Save that silently doesn't, is
worse than no button at all.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp, build_real_rows

pytestmark = pytest.mark.e2e

LOAD = 20_000
SLOW = 60_000


def _open_settings(page: Page) -> None:
    page.goto("/settings")
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible(timeout=LOAD)


class TestConnectionCards:
    def test_every_test_button_reports_the_real_state_of_its_connection(self, page: Page, app: ShortlistApp):
        """All four services, each hitting its real client — not one happy-path card.

        Tautulli is deliberately unconfigured here, which is the state a fresh install is in:
        the card must say so in plain English instead of claiming success.
        """
        _open_settings(page)

        plex = page.get_by_test_id("connection-plex")
        plex.get_by_role("button", name="Test").click()
        expect(plex).to_contain_text("Connected to FakePlex (PMS 1.43.3.10793)", timeout=LOAD)

        tmdb = page.get_by_test_id("connection-tmdb")
        tmdb.get_by_role("button", name="Test").click()
        expect(tmdb).to_contain_text("TMDB key works", timeout=LOAD)

        llm = page.get_by_test_id("connection-llm")
        llm.get_by_role("button", name="Test").click()
        expect(llm).to_contain_text("Built-in picker — no AI, nothing to test, always works", timeout=LOAD)

        # An unconfigured connection says so plainly and never claims a connection it doesn't have —
        # and its Test is disabled until a key is on file (you can't test what isn't set up), rather
        # than letting the owner test nothing and read a raw error.
        tautulli = page.get_by_test_id("connection-tautulli")
        expect(tautulli).to_contain_text("Not set up yet")
        expect(tautulli.get_by_role("button", name="Test")).to_be_disabled()
        expect(tautulli).not_to_contain_text("Connected —")


class TestDefaults:
    def test_a_local_ai_server_can_be_pointed_at_from_settings(self, page: Page, app: ShortlistApp):
        """The wizard is only walked once; everyone who adds a local AI server later does it HERE.

        Covers the whole path an existing owner takes: pick the provider, get a URL field at all,
        type the address their server is known by, and have it persist under the right key (#7).
        """
        page.goto("/settings")
        title = page.get_by_text("AI curator").first
        expect(title).to_be_visible(timeout=LOAD)
        # Scope to the AI-curator card: every connection card has an identical Edit/Test pair.
        card = title.locator('xpath=ancestor::div[contains(@class,"rounded")][1]')
        card.get_by_role("button", name=re.compile("^(Edit|Set up)$")).first.click()

        # "Provider" renders as a segmented group, not a <select> — pick the option by its label.
        page.get_by_label("Provider").get_by_role("button", name="Local server").click()
        # The URL field must APPEAR — without it there is no way to say where the server is.
        url = page.get_by_label("Server URL")
        expect(url).to_be_visible(timeout=LOAD)
        url.fill("http://llama.local:8080")
        page.get_by_role("button", name="Save").first.click()

        for _ in range(20):
            settings = app.api("GET", "/api/settings").json()
            if settings.get("curator.provider") == "openai_compatible":
                break
            page.wait_for_timeout(250)
        settings = app.api("GET", "/api/settings").json()
        assert settings["curator.provider"] == "openai_compatible"
        assert settings["curator.openai_base_url"] == "http://llama.local:8080"

    def test_row_name_and_size_survive_a_reload(self, page: Page, app: ShortlistApp):
        _open_settings(page)

        row_name = page.get_by_label("Row name template")
        row_name.fill("🍿 Tonight's picks for {top_seed}")
        # The preview must show what Plex will show, not the raw template.
        expect(page.get_by_text("🍿 Tonight's picks for Fargo")).to_be_visible()

        # Row size is a free number field now; blur commits the typed value.
        row_size = page.get_by_label("Row size")
        row_size.fill("22")
        row_size.blur()
        # No Save button — the section auto-saves (debounced). Poll until it reaches the database.
        for _ in range(24):
            stored = app.api("GET", "/api/settings").json()
            if stored.get("row.name_template") == "🍿 Tonight's picks for {top_seed}" and stored.get("row.size") == 22:
                break
            page.wait_for_timeout(250)
        stored = app.api("GET", "/api/settings").json()
        assert stored["row.name_template"] == "🍿 Tonight's picks for {top_seed}"
        assert stored["row.size"] == 22

        # Reload: only a value that reached the database can come back.
        page.reload()
        expect(page.get_by_label("Row name template")).to_have_value("🍿 Tonight's picks for {top_seed}", timeout=LOAD)
        expect(page.get_by_label("Row size")).to_have_value("22", timeout=LOAD)

    def test_pause_all_stops_runs_without_disabling_anyone(self, page: Page, app: ShortlistApp):
        """The Danger Zone switch must actually pause runs — it used to 422 as an unknown key."""
        _open_settings(page)
        page.get_by_role("button", name="Pause all").click()

        # It persisted...
        expect(page.get_by_role("alert")).to_have_count(0, timeout=LOAD)
        for _ in range(20):
            if app.api("GET", "/api/settings").json().get("paused_all") is True:
                break
            page.wait_for_timeout(250)
        assert app.api("GET", "/api/settings").json()["paused_all"] is True

        # ...and a run now processes nobody, while every user stays enabled.
        run_id = app.api("POST", "/api/runs", json={"dry_run": True}).json()["run_id"]
        run = app.wait_for_run(run_id)
        assert run["stats"].get("users_ok", 0) == 0
        assert run["stats"].get("users_error", 0) == 0
        assert all(u["enabled"] for u in app.api("GET", "/api/users").json())


class TestDangerZone:
    def test_uninstall_dry_run_previews_the_damage_without_doing_any(
        self, page: Page, app: ShortlistApp, reset_fake_plex
    ):
        """The preview must be honest about what would go, and must not touch Plex.

        This is the trust feature: an owner who cannot see what uninstall would do will never
        press it. So the preview lists every collection by name — and afterwards the server is
        byte-for-byte unchanged.
        """
        state = reset_fake_plex
        build_real_rows(app)
        before_collections = {c.rating_key: c.title for c in state.collections.values()}
        before_filters = {user.id: dict(user.filters) for user in state.users.values()}
        # 5 rows for 3 users: sarah and the cold-start canary each get one per library; mike watches only TV.
        assert len(before_collections) == 5

        _open_settings(page)
        # Uninstall is its own page now; the Danger Zone links to it.
        page.get_by_role("link", name="Uninstall Shortlist…").click()
        expect(page.get_by_role("heading", name="Uninstall Shortlist")).to_be_visible(timeout=LOAD)

        page.get_by_role("button", name="Preview what would change").click()
        body = page.locator("body")
        expect(body).to_contain_text("5 collections", timeout=SLOW)
        expect(body).to_contain_text("3 share filters")
        expect(body).to_contain_text("Preview only — nothing was changed.")
        for title in before_collections.values():
            expect(body).to_contain_text(title)

        # The destructive button stays locked until the phrase is typed — a preview is not consent.
        commit = page.get_by_role("button", name="Uninstall and restore server")
        expect(commit).to_be_disabled()

        # "Keep Shortlist" is the way out — it goes back to Settings without touching anything.
        page.get_by_role("link", name="Keep Shortlist").click()
        expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible(timeout=LOAD)

        # Nothing moved on the fake Plex: not one collection, not one share filter.
        assert {c.rating_key: c.title for c in state.collections.values()} == before_collections
        assert {user.id: dict(user.filters) for user in state.users.values()} == before_filters
        # (The committed uninstall lives in test_privacy_uninstall_e2e.py — this test is only
        # about the promise that a PREVIEW costs nothing.)
