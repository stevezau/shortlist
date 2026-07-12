"""E2E: Users, per-user overrides, triggering runs, and reading the result.

The through-line: a control the owner touches in the browser must change what the ENGINE does
on the next run. A toggle that only repaints the UI is the failure this file exists to catch.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import RowarrApp, build_real_rows

pytestmark = pytest.mark.e2e

LOAD = 20_000
SLOW = 90_000


def _users_by_name(app: RowarrApp) -> dict[str, dict]:
    return {user["username"]: user for user in app.api("GET", "/api/users").json()}


class TestUsers:
    def test_disabling_a_user_persists_and_leaves_the_others_alone(self, page: Page, app: RowarrApp):
        page.goto("/users")
        canary = page.get_by_role("switch", name="Rowarr row for canary")
        expect(canary).to_be_checked(timeout=LOAD)

        canary.click()
        expect(canary).not_to_be_checked(timeout=LOAD)

        page.reload()
        expect(page.get_by_role("switch", name="Rowarr row for canary")).not_to_be_checked(timeout=LOAD)

        # Exactly one user changed — a broadcast PATCH would pass a "it persisted" test too.
        users = _users_by_name(app)
        assert users["canary"]["enabled"] is False
        assert users["sarah"]["enabled"] is True
        assert users["mike"]["enabled"] is True

    def test_per_user_overrides_persist(self, page: Page, app: RowarrApp):
        sarah_id = _users_by_name(app)["sarah"]["id"]
        page.goto(f"/users/{sarah_id}")
        expect(page.get_by_role("heading", name="sarah")).to_be_visible(timeout=LOAD)

        paused = page.get_by_role("switch", name=re.compile("^Paused"))
        paused.click()
        expect(paused).to_be_checked()

        page.get_by_role("button", name="10", exact=True).click()
        page.get_by_role("button", name="Save overrides").click()

        # PATCH /api/users/{id} merges prefs, so both edits must survive together.
        def prefs() -> dict:
            return _users_by_name(app)["sarah"]["prefs"]

        expect(page.get_by_role("button", name="Save overrides")).to_be_enabled(timeout=LOAD)
        assert prefs()["row_size"] == 10
        assert prefs()["paused"] is True
        assert _users_by_name(app)["mike"]["prefs"] == {}

    def test_a_paused_user_is_skipped_by_the_next_run(self, page: Page, app: RowarrApp, reset_fake_plex):
        """The override has to reach the ENGINE, not just the users table."""
        state = reset_fake_plex
        sarah_id = _users_by_name(app)["sarah"]["id"]

        page.goto(f"/users/{sarah_id}")
        expect(page.get_by_role("heading", name="sarah")).to_be_visible(timeout=LOAD)
        page.get_by_role("switch", name=re.compile("^Paused")).click()
        expect(page.get_by_role("switch", name=re.compile("^Paused"))).to_be_checked(timeout=LOAD)

        run = build_real_rows(app)
        built = {result["slug"] for result in app.api("GET", f"/api/runs/{run['id']}").json()["users"]}
        assert built == {"mike", "canary"}, "a paused user must not be rebuilt"

        labels = {label.lower() for c in state.collections.values() for label in c.labels}
        assert labels == {"rowarr_mike", "rowarr_canary"}
        assert "rowarr_sarah" not in labels


class TestRuns:
    def test_a_run_started_from_the_ui_lands_in_runs_with_per_user_rows(
        self, page: Page, app: RowarrApp, reset_fake_plex
    ):
        state = reset_fake_plex
        # The gate is real: without a passing check the server refuses to write (rule 1).
        assert app.api("POST", "/api/privacy/check", json={}).json()["passed"]

        page.goto("/runs")
        expect(page.get_by_text("No runs yet")).to_be_visible(timeout=LOAD)
        page.get_by_role("button", name="Run all users now").click()

        # The row appears the moment the run is queued — the owner is never left guessing.
        run_link = page.get_by_role("link", name="#1")
        expect(run_link).to_be_visible(timeout=LOAD)
        expect(page.get_by_role("cell", name="manual")).to_be_visible()

        app.wait_for_run(1)
        page.reload()
        expect(page.get_by_text("ok", exact=True)).to_be_visible(timeout=LOAD)
        expect(page.get_by_role("cell", name="3 ok")).to_be_visible()
        assert len(state.collections) == 3

        page.get_by_role("link", name="#1").click()
        expect(page.get_by_role("heading", name="Run #1")).to_be_visible(timeout=LOAD)
        for username in ("sarah", "mike", "canary"):
            expect(page.get_by_role("link", name=username, exact=True)).to_be_visible()

    def test_run_detail_shows_what_changed(self, page: Page, app: RowarrApp):
        """Added on the first run; Kept on the next — the diff is the audit trail (rule 10)."""
        first = build_real_rows(app)

        page.goto(f"/runs/{first['id']}")
        expect(page.get_by_role("heading", name=f"Run #{first['id']}")).to_be_visible(timeout=LOAD)
        # sarah and mike can only be offered the 10 unwatched titles TMDB suggests for their
        # seeds; the cold-start canary draws from the whole library and fills all 15.
        expect(page.get_by_text(re.compile(r"^Added \(10\)$"))).to_have_count(2)
        expect(page.get_by_text(re.compile(r"^Added \(15\)$"))).to_have_count(1)
        expect(page.get_by_text(re.compile(r"^Removed"))).to_have_count(0)
        # Titles, not counts: "which titles landed on whose row" must be answerable from here.
        expect(page.locator("body")).to_contain_text("Movie 13")

        second = build_real_rows(app)
        page.goto(f"/runs/{second['id']}")
        expect(page.get_by_role("heading", name=f"Run #{second['id']}")).to_be_visible(timeout=LOAD)
        # The canary's cold-start row is rebuilt identically -> everything kept, nothing churned.
        expect(page.get_by_text(re.compile(r"^Kept \(15\)$"))).to_have_count(1)
        # sarah and mike have no unseen titles left, so their rows are deliberately left alone.
        expect(page.get_by_text("No changes — the row was already up to date.")).to_have_count(2)

    def test_a_users_picks_explain_themselves(self, page: Page, app: RowarrApp):
        """Every pick carries its "Because you watched …" reason — the product's whole promise."""
        build_real_rows(app)
        sarah_id = _users_by_name(app)["sarah"]["id"]

        page.goto(f"/users/{sarah_id}")
        expect(page.get_by_role("heading", name="sarah")).to_be_visible(timeout=LOAD)

        picks = page.get_by_role("listitem").filter(has_text="#1")
        expect(picks.first).to_be_visible(timeout=LOAD)
        reasons = page.get_by_text(re.compile(r"^Because you watched Movie \d+$"))
        expect(reasons).to_have_count(10)

        # Ranked, and the rank the engine chose is the rank shown.
        detail = app.api("GET", f"/api/runs/{app.api('GET', '/api/runs').json()[0]['id']}").json()
        sarah_picks = next(u["picks"] for u in detail["users"] if u["slug"] == "sarah")
        expect(page.get_by_text(sarah_picks[0]["title"], exact=True).first).to_be_visible()

    def test_the_canary_gets_the_cold_start_row_and_says_so(self, page: Page, app: RowarrApp):
        """No history -> the popular-titles fallback, labelled honestly rather than faked."""
        build_real_rows(app)
        canary_id = _users_by_name(app)["canary"]["id"]

        page.goto(f"/users/{canary_id}")
        expect(page.get_by_role("heading", name="canary")).to_be_visible(timeout=LOAD)
        expect(page.get_by_text("cold start").first).to_be_visible()
        expect(page.get_by_text("Popular on this server").first).to_be_visible()
