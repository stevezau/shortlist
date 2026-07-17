"""E2E: Users, per-user overrides, triggering runs, and reading the result.

The through-line: a control the owner touches in the browser must change what the ENGINE does
on the next run. A toggle that only repaints the UI is the failure this file exists to catch.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp, build_real_rows

pytestmark = pytest.mark.e2e

LOAD = 20_000
SLOW = 90_000


def _users_by_name(app: ShortlistApp) -> dict[str, dict]:
    return {user["username"]: user for user in app.api("GET", "/api/users").json()}


def _wait_until(condition: Callable[[], bool], message: str, timeout_s: float = 10.0) -> None:
    """Poll the API until a persisted value shows up — robust to the auto-save debounce, which the
    'Saved' indicator can race (it flips before the PUT the debounce fired has fully committed)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.25)
    raise AssertionError(message)


class TestUsers:
    def test_disabling_a_user_persists_and_leaves_the_others_alone(self, page: Page, app: ShortlistApp):
        page.goto("/users")
        canary = page.get_by_role("switch", name="Shortlist row for canary")
        expect(canary).to_be_checked(timeout=LOAD)

        canary.click()
        expect(canary).not_to_be_checked(timeout=LOAD)

        page.reload()
        expect(page.get_by_role("switch", name="Shortlist row for canary")).not_to_be_checked(timeout=LOAD)

        # Exactly one user changed — a broadcast PATCH would pass a "it persisted" test too.
        users = _users_by_name(app)
        assert users["canary"]["enabled"] is False
        assert users["sarah"]["enabled"] is True
        assert users["mike"]["enabled"] is True

    def test_per_user_overrides_persist(self, page: Page, app: ShortlistApp):
        sarah_id = _users_by_name(app)["sarah"]["id"]
        page.goto(f"/users/{sarah_id}")
        expect(page.get_by_role("heading", name="sarah")).to_be_visible(timeout=LOAD)

        # Pause is a user-level control in the header (switch is "on" when active).
        paused = page.get_by_role("switch", name="Pause or resume sarah")
        expect(paused).to_be_checked(timeout=LOAD)
        paused.click()
        expect(paused).not_to_be_checked(timeout=LOAD)

        # Size is a PER-ROW override now: open a row's drawer, switch on a custom size and type 10.
        # The drawer AUTO-SAVES (no Save button — the whole app is one save paradigm).
        page.get_by_role("button", name="Customize for this person").first.click()
        page.get_by_role("switch", name="Custom row size for this person").click()
        titles = page.get_by_label("Titles for this person")
        titles.fill("10")
        titles.blur()  # the free number field commits on blur
        expect(page.get_by_text("Saved").first).to_be_visible(timeout=LOAD)

        # Pause reaches the user's prefs; the size override lives on the row, not user prefs. Poll the
        # API for the persisted value rather than trusting the indicator, which races the debounce.
        assert _users_by_name(app)["sarah"]["prefs"]["paused"] is True
        expect_row_size = 10

        def _size_persisted() -> bool:
            rows = app.api("GET", f"/api/users/{sarah_id}/rows").json()
            return any(r["override"].get("row_size") == expect_row_size for r in rows)

        _wait_until(_size_persisted, "the per-row size override must persist")
        # Exactly this user changed — mike's prefs stay empty.
        assert _users_by_name(app)["mike"]["prefs"] == {}

    def test_a_paused_user_is_skipped_by_the_next_run(self, page: Page, app: ShortlistApp, reset_fake_plex):
        """The override has to reach the ENGINE, not just the users table."""
        state = reset_fake_plex
        sarah_id = _users_by_name(app)["sarah"]["id"]

        page.goto(f"/users/{sarah_id}")
        expect(page.get_by_role("heading", name="sarah")).to_be_visible(timeout=LOAD)
        # The header pause switch is "on" when active; clicking it pauses this user.
        paused = page.get_by_role("switch", name="Pause or resume sarah")
        expect(paused).to_be_checked(timeout=LOAD)
        paused.click()
        expect(paused).not_to_be_checked(timeout=LOAD)

        run = build_real_rows(app)
        built = {result["slug"] for result in app.api("GET", f"/api/runs/{run['id']}").json()["users"]}
        assert built == {"mike", "canary"}, "a paused user must not be rebuilt"

        labels = {label.lower() for c in state.collections.values() for label in c.labels}
        assert labels == {"shortlist_mike", "shortlist_canary"}
        assert "shortlist_sarah" not in labels


class TestRuns:
    def test_a_run_started_from_the_ui_lands_in_runs_with_per_user_rows(
        self, page: Page, app: ShortlistApp, reset_fake_plex
    ):
        state = reset_fake_plex

        page.goto("/runs")
        expect(page.get_by_text("No runs yet")).to_be_visible(timeout=LOAD)
        page.get_by_role("button", name="Run all rows now").click()

        # The row appears the moment the run is queued — the owner is never left guessing.
        run_link = page.get_by_role("link", name="#1")
        expect(run_link).to_be_visible(timeout=LOAD)
        expect(page.get_by_role("cell", name="manual")).to_be_visible()

        app.wait_for_run(1)
        page.reload()
        # The status badge renders the title-cased label ("OK") — scoped to the table, since the
        # page's stats bar also shows "OK" as the last-run status. The users cell keeps the "3 ok" count.
        expect(page.get_by_role("table").get_by_text("OK", exact=True)).to_be_visible(timeout=LOAD)
        expect(page.get_by_role("cell", name="3 ok")).to_be_visible()
        # 5 rows for 3 users: sarah and the cold-start canary each get one per library; mike watches only TV.
        assert len(state.collections) == 5

        page.get_by_role("link", name="#1").click()
        expect(page.get_by_role("heading", name="Run #1")).to_be_visible(timeout=LOAD)
        # Every user is a clickable tab in the run's nav; the selected one's rows show below it.
        for username in ("sarah", "mike", "canary"):
            expect(page.get_by_role("tab", name=re.compile(username, re.IGNORECASE))).to_be_visible()

    def test_run_detail_shows_what_changed(self, page: Page, app: ShortlistApp, reset_fake_plex):
        """The diff is the audit trail (rule 10): everything Added on the first run, and on the
        next run the staleness guard rotates fresh titles in — without ever shrinking a row."""
        state = reset_fake_plex
        first = build_real_rows(app)
        # The engine must record a per-(row, library) breakdown — the whole run page is built from it.
        run = app.api("GET", f"/api/runs/{first['id']}").json()
        assert any(b["added"] for u in run["users"] for b in u["breakdown"]), "no per-library breakdown recorded"

        page.goto(f"/runs/{first['id']}")
        expect(page.get_by_role("heading", name=f"Run #{first['id']}")).to_be_visible(timeout=LOAD)
        # First run: every pick is new, so the selected user's row shows a "+N new" badge and nothing
        # removed. Picks render as a ranked list, and their titles are answerable from here.
        expect(page.get_by_text(re.compile(r"\+\d+ new")).first).to_be_visible()
        expect(page.get_by_text(re.compile(r"\d+ removed"))).to_have_count(0)
        expect(page.locator("body")).to_contain_text(re.compile(r"(Movie|Show) \d+"))

        def row_sizes() -> dict[str, int]:
            sizes: dict[str, int] = {}
            for collection in state.collections.values():
                for label in collection.labels:
                    if label.lower().startswith("shortlist_"):
                        sizes[label.lower()] = sizes.get(label.lower(), 0) + len(collection.item_keys)
            return sizes

        before = row_sizes()
        second = build_real_rows(app)
        page.goto(f"/runs/{second['id']}")
        expect(page.get_by_role("heading", name=f"Run #{second['id']}")).to_be_visible(timeout=LOAD)

        # "Don't repeat the last 3 runs' picks" rotates the row; it must never leave it short.
        # When fresh candidates run out, held-back titles backfill — a row shrinking (or being
        # pruned) because the pool was thin for one night would be a regression, not variety.
        assert row_sizes() == before, "a re-run must keep every row exactly as full as it was"

        for user in app.api("GET", f"/api/runs/{second['id']}").json()["users"]:
            diff = user["diff"]
            assert len(diff.get("added", [])) == len(diff.get("removed", [])), (
                f"{user['slug']}: a rotated-out title must be replaced, not simply dropped"
            )
        # The second run's page renders its users (the per-user nav), so its results are reachable.
        expect(page.get_by_role("tab").first).to_be_visible(timeout=LOAD)

    def test_a_users_picks_explain_themselves(self, page: Page, app: ShortlistApp):
        """Every pick carries its "Because you watched …" reason — the product's whole promise."""
        build_real_rows(app)
        sarah_id = _users_by_name(app)["sarah"]["id"]
        run_id = app.api("GET", "/api/runs").json()[0]["id"]
        sarah_picks = next(
            u["picks"] for u in app.api("GET", f"/api/runs/{run_id}").json()["users"] if u["slug"] == "sarah"
        )

        page.goto(f"/users/{sarah_id}")
        expect(page.get_by_role("heading", name="sarah")).to_be_visible(timeout=LOAD)

        picks = page.get_by_role("listitem").filter(has_text="#1")
        expect(picks.first).to_be_visible(timeout=LOAD)
        # Each pick renders its "Because you watched …" reason inside its list item (the PickList now
        # combines title + reason + seed in one line). sarah watches movies AND TV, so both appear.
        reasons = page.get_by_role("listitem").filter(has_text=re.compile(r"Because you watched (Movie|Show) \d+"))
        expect(reasons).to_have_count(len(sarah_picks))
        assert {p["title"].split()[0] for p in sarah_picks} == {"Movie", "Show"}, (
            "sarah's row should mix both libraries — otherwise this test proves nothing about them"
        )

        # Ranked, and the rank the engine chose is the rank shown.
        expect(page.get_by_text(sarah_picks[0]["title"], exact=True).first).to_be_visible()

    def test_the_canary_gets_the_cold_start_row_and_says_so(self, page: Page, app: ShortlistApp):
        """No history -> the popular-titles fallback, labelled honestly rather than faked."""
        build_real_rows(app)
        canary_id = _users_by_name(app)["canary"]["id"]

        page.goto(f"/users/{canary_id}")
        expect(page.get_by_role("heading", name="canary")).to_be_visible(timeout=LOAD)
        expect(page.get_by_text("New viewer").first).to_be_visible()
        expect(page.get_by_text("Popular on this server").first).to_be_visible()
