"""E2E: the Privacy Check, the write gate it guards, and the uninstall that undoes everything.

These are the two promises Rowarr makes that a user cannot verify for themselves: "your rows
are private" and "I can put your server back". Both are checked here against a real server
(the fake PMS/plex.tv), not against mocks that agree with us by construction.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import RowarrApp, build_real_rows

pytestmark = pytest.mark.e2e

LOAD = 20_000
SLOW = 60_000


class TestPrivacyBadge:
    def test_the_badge_starts_unverified_and_flips_when_a_check_passes(
        self, page: Page, app: RowarrApp, reset_fake_plex
    ):
        """The dashboard must never imply privacy it hasn't proven.

        The check is fired from outside the browser on purpose: it proves the badge tracks the
        SERVER's privacy state over SSE, rather than merely the click that happened to be local.

        GAP (reported): there is no control anywhere in the UI to run the read-only T1/T2 check.
        The wizard's panel says "re-run … later from the dashboard" and step 7 says "from
        Settings", and neither page has the button — nor can `api.runPrivacyCheck` request
        anything but the full probe (`{probe: true}` is hardcoded).
        """
        page.goto("/")
        expect(page.get_by_text("Privacy: not checked yet")).to_be_visible(timeout=LOAD)

        result = app.api("POST", "/api/privacy/check", json={}).json()
        assert result["passed"] is True
        assert set(result["tiers"]) == {"T1", "T2"}, "both tiers must run when a Home canary exists"

        badge = page.get_by_text(re.compile(r"^Privacy: verified"))
        expect(badge).to_be_visible(timeout=SLOW)
        # "Verified" is worthless without a date — a check from six months ago is not a promise.
        expect(badge).to_have_text(re.compile(r"Privacy: verified \d"))

        status = app.api("GET", "/api/privacy/status").json()
        assert status["passed"] is True
        assert status["last_check"] is not None

    def test_a_lost_exclusion_fails_the_check_and_slams_the_write_gate_shut(
        self, page: Page, app: RowarrApp, reset_fake_plex
    ):
        """If plex.tv drops an exclusion, Rowarr must notice and REFUSE to write again.

        This is the failure mode the whole tiered check exists for: the rows are still on the
        server, but one user's share filter no longer hides the others'. T1 reads the filters
        back from plex.tv and catches it; the run gate then fails closed (plex-safety rule 1).
        """
        state = reset_fake_plex
        build_real_rows(app)
        assert app.api("GET", "/api/privacy/status").json()["passed"] is True

        # Simulate plex.tv losing sarah's exclusions (the exact silent-regression scenario).
        state.users[201].filters["filterMovies"] = ""
        state.users[201].filters["filterTelevision"] = ""

        page.goto("/")
        expect(page.get_by_text(re.compile(r"^Privacy: verified"))).to_be_visible(timeout=LOAD)

        result = app.api("POST", "/api/privacy/check", json={}).json()
        assert result["passed"] is False
        assert result["tiers"]["T1"] is False, "T1 must catch a share filter that lost its excludes"
        assert result["tiers"]["T2"] is True, "the canary's own hubs are still clean — only T1 should fail"

        expect(page.get_by_text("Privacy: check failed — rows may be visible to others")).to_be_visible(timeout=SLOW)

        # Fail closed: no more real writes until the owner fixes it.
        created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
        refused = app.wait_for_run(created["run_id"])
        assert refused["status"] == "error"
        assert "the last Privacy Check FAILED (T1)" in refused["stats"]["error"]

    def test_the_pre_rowarr_filters_are_snapshotted_before_the_first_write(self, app: RowarrApp, reset_fake_plex):
        """plex-safety rule 2: uninstall can only restore what was captured BEFORE we touched it."""
        build_real_rows(app)

        snapshots = app.api("GET", "/api/privacy/snapshots").json()
        assert {snapshot["username"] for snapshot in snapshots} == {"sarah", "mike", "canary"}
        for snapshot in snapshots:
            assert snapshot["reason"] == "initial"
            # Every seeded user started with empty share filters — that is what must come back.
            assert snapshot["filters_before"]["filterMovies"] == ""
            assert snapshot["filters_before"]["filterTelevision"] == ""


class TestUninstall:
    def test_uninstall_puts_the_server_back_as_rowarr_found_it(self, page: Page, app: RowarrApp, reset_fake_plex):
        """The typed-confirmation path, all the way through: rows deleted, filters restored."""
        state = reset_fake_plex
        build_real_rows(app)
        assert len(state.collections) == 3
        assert state.users[201].filters["filterMovies"] == "label!=Rowarr_canary,Rowarr_mike"

        page.goto("/settings")
        page.get_by_role("button", name="Uninstall Rowarr…").click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=LOAD)

        commit = dialog.get_by_role("button", name="Uninstall and restore server")
        expect(commit).to_be_disabled()
        dialog.get_by_role("textbox").fill("uninstall rowarr")
        expect(commit).to_be_enabled()
        commit.click()

        expect(page.get_by_text("Your server is as we found it.")).to_be_visible(timeout=SLOW)

        assert state.collections == {}, "a Rowarr collection survived the uninstall"
        for user in state.users.values():
            assert user.filters["filterMovies"] == "", f"{user.username}'s share filter was not restored"
            assert user.filters["filterTelevision"] == ""

    def test_uninstall_leaves_collections_rowarr_does_not_own_alone(self, page: Page, app: RowarrApp, reset_fake_plex):
        """Kometa coexistence (plex-safety rule 4): only rowarr_* labelled collections may go."""
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
        page.get_by_role("button", name="Uninstall Rowarr…").click()
        dialog = page.get_by_role("dialog")

        dialog.get_by_role("button", name="Preview what would change").click()
        expect(dialog).to_contain_text("3 collections deleted", timeout=SLOW)
        expect(dialog).not_to_contain_text("Kometa")

        dialog.get_by_role("textbox").fill("uninstall rowarr")
        dialog.get_by_role("button", name="Uninstall and restore server").click()
        expect(page.get_by_text("Your server is as we found it.")).to_be_visible(timeout=SLOW)

        assert list(state.collections) == [9999], "uninstall deleted a collection Rowarr did not create"
        assert state.collections[9999].item_keys == [101, 102]
