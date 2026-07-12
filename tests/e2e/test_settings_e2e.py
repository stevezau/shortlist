"""E2E: the Settings page — connection tests, defaults, schedule, and the way out.

Settings is where an owner goes when something is wrong, so every control here has to tell the
truth: a Test button that says "Connected" when it isn't, or a Save that silently doesn't, is
worse than no button at all.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import RowarrApp, build_real_rows

pytestmark = pytest.mark.e2e

LOAD = 20_000
SLOW = 60_000


def _open_settings(page: Page) -> None:
    page.goto("/settings")
    expect(page.get_by_role("heading", name="Settings", exact=True)).to_be_visible(timeout=LOAD)


class TestConnectionCards:
    def test_every_test_button_reports_the_real_state_of_its_connection(self, page: Page, app: RowarrApp):
        """All four services, each hitting its real client — not one happy-path card.

        Tautulli is deliberately unconfigured here, which is the state a fresh install is in:
        the card must say so in plain English instead of claiming success.
        """
        _open_settings(page)

        plex = page.get_by_test_id("connection-plex")
        plex.get_by_role("button", name="Test").click()
        expect(plex).to_contain_text("Connected — Connected to FakePlex (PMS 1.43.3.10793)", timeout=LOAD)

        tmdb = page.get_by_test_id("connection-tmdb")
        tmdb.get_by_role("button", name="Test").click()
        expect(tmdb).to_contain_text("Connected — TMDB key works", timeout=LOAD)

        llm = page.get_by_test_id("connection-llm")
        llm.get_by_role("button", name="Test").click()
        expect(llm).to_contain_text("Heuristic mode — nothing to test, always works", timeout=LOAD)

        tautulli = page.get_by_test_id("connection-tautulli")
        expect(tautulli).to_contain_text("Not configured yet.")
        tautulli.get_by_role("button", name="Test").click()
        # An error, surfaced — the card must not stay silent or claim a connection it doesn't have.
        expect(tautulli).to_contain_text(re.compile("Error|error|missing"), timeout=LOAD)
        expect(tautulli).not_to_contain_text("Connected —")


class TestDefaults:
    def test_row_name_and_size_survive_a_reload(self, page: Page, app: RowarrApp):
        _open_settings(page)

        row_name = page.get_by_label("Row name template")
        row_name.fill("🍿 Tonight's picks for {top_seed}")
        # The preview must show what Plex will show, not the raw template.
        expect(page.get_by_text("🍿 Tonight's picks for Fargo")).to_be_visible()

        page.get_by_role("button", name="20", exact=True).click()
        page.get_by_role("button", name="Save defaults").click()

        # Reload: only a value that reached the database can come back.
        page.reload()
        expect(page.get_by_label("Row name template")).to_have_value("🍿 Tonight's picks for {top_seed}", timeout=LOAD)
        expect(page.get_by_role("button", name="20", exact=True)).to_have_attribute("aria-pressed", "true")

        stored = app.api("GET", "/api/settings").json()
        assert stored["row.name_template"] == "🍿 Tonight's picks for {top_seed}"
        assert stored["row.size"] == 20

    def test_pause_all_stops_runs_without_disabling_anyone(self, page: Page, app: RowarrApp):
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


class TestSchedule:
    def test_saving_the_schedule_persists_a_valid_cron(self, page: Page, app: RowarrApp):
        _open_settings(page)

        page.get_by_label("Run at").fill("04:45")
        page.get_by_role("button", name="weekly", exact=True).click()
        page.get_by_role("button", name="Save schedule").click()

        expect(page.get_by_text(re.compile(r"Rows refresh weekly at 04:45"))).to_be_visible()
        page.reload()
        expect(page.get_by_label("Run at")).to_have_value("04:45", timeout=LOAD)
        assert app.api("GET", "/api/settings").json()["schedule.cron"] == "45 4 * * 0"

    def test_an_invalid_cron_is_rejected_with_a_readable_message(self, page: Page, app: RowarrApp):
        """The backend must never accept a cron that would silently kill the nightly run.

        Issued through the SPA's own fetch (session cookie + CSRF header, same as any mutation)
        rather than a UI control, because there is no cron field on the page yet — Settings says
        "Cron expressions ... are coming to this page". When that field lands, point this at it.
        """
        _open_settings(page)

        result = page.evaluate(
            """async () => {
                const response = await fetch('/api/settings', {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json', 'x-rowarr-csrf': '1' },
                    body: JSON.stringify({ values: { 'schedule.cron': 'not a cron' } }),
                });
                return { status: response.status, body: await response.json() };
            }"""
        )

        assert result["status"] == 422
        assert "invalid cron expression" in result["body"]["detail"]
        # And the good schedule is still in place — a rejected write changes nothing.
        assert app.api("GET", "/api/settings").json()["schedule.cron"] == "30 3 * * *"


class TestDangerZone:
    def test_uninstall_dry_run_previews_the_damage_without_doing_any(self, page: Page, app: RowarrApp, reset_fake_plex):
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
        page.get_by_role("button", name="Uninstall Rowarr…").click()

        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("Uninstall Rowarr from this server?")

        dialog.get_by_role("button", name="Preview what would change").click()
        expect(dialog).to_contain_text("5 collections deleted", timeout=SLOW)
        expect(dialog).to_contain_text("3 share filters restored")
        expect(dialog).to_contain_text("Preview only — nothing was changed.")
        for title in before_collections.values():
            expect(dialog).to_contain_text(title)

        # The destructive button stays locked until the phrase is typed — a preview is not consent.
        commit = dialog.get_by_role("button", name="Uninstall and restore server")
        expect(commit).to_be_disabled()

        dialog.get_by_role("button", name="Keep Rowarr").click()
        expect(dialog).not_to_be_visible()

        # Nothing moved on the fake Plex: not one collection, not one share filter.
        assert {c.rating_key: c.title for c in state.collections.values()} == before_collections
        assert {user.id: dict(user.filters) for user in state.users.values()} == before_filters
        # (The committed uninstall lives in test_privacy_uninstall_e2e.py — this test is only
        # about the promise that a PREVIEW costs nothing.)
