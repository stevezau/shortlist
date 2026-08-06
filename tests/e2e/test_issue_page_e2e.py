"""E2E: the "Have an issue?" page, in a real browser, against the real API and a fake Plex server.

This layer exists for the cross-boundary breaks the other two suites cannot see. The integration
tests prove the endpoints; the vitest tests prove the page against a MOCKED api client. Neither
notices a route that was never registered, a nav link pointing at the old path, or a check that
403s because the mode toggle didn't actually reach the server.

The flow asserted here is the one a maintainer talks someone through over chat, in order:
open the page → switch the checks on → run one → copy the result.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e


class TestGettingThere:
    def test_the_sidebar_offers_one_door_for_problems(self, page: Page, app: ShortlistApp):
        """It replaced two separate sidebar actions, so the link has to actually be there and go to
        the right place — a stale `to=` would still render and still look fine."""
        page.goto("/")
        page.get_by_role("link", name=re.compile("have an issue", re.IGNORECASE)).click()

        expect(page).to_have_url(re.compile(r"/issue$"))
        expect(page.get_by_role("heading", name=re.compile("have an issue", re.IGNORECASE))).to_be_visible(
            timeout=20_000
        )

    def test_the_old_sidebar_actions_are_gone(self, page: Page, app: ShortlistApp):
        """Two competing entry points is the state this change removed; one of them produced reports
        with no diagnostics attached."""
        page.goto("/")
        expect(page.get_by_role("link", name=re.compile(r"^report a bug$", re.IGNORECASE))).to_have_count(0)
        expect(page.get_by_role("button", name=re.compile("copy diagnostics", re.IGNORECASE))).to_have_count(0)


class TestTheChecksAreOffUntilAsked:
    def test_no_check_runs_before_the_mode_is_switched_on(self, page: Page, app: ShortlistApp):
        """Off by default even for the owner. The surface reads share filters and per-user tokens, so
        an install that is not currently debugging anything should not expose it."""
        page.goto("/issue")
        expect(page.get_by_role("button", name=re.compile("switch on the checks", re.IGNORECASE))).to_be_visible(
            timeout=20_000
        )
        expect(page.get_by_text(re.compile("what's the problem", re.IGNORECASE))).to_have_count(0)

    def test_switching_on_reaches_the_server_and_unlocks_the_checks(self, page: Page, app: ShortlistApp):
        """The mutation contract: the SPA must send the CSRF header, and the server must actually
        record the mode — a client-side-only toggle would light the UI up and then 403 every check."""
        page.goto("/issue")
        page.get_by_role("button", name=re.compile("switch on the checks", re.IGNORECASE)).click()

        expect(page.get_by_text(re.compile("checks are switched on", re.IGNORECASE))).to_be_visible(timeout=20_000)
        # Health runs by itself once unlocked, and it only renders after a 200.
        expect(page.get_by_text(re.compile("plex server", re.IGNORECASE)).first).to_be_visible(timeout=20_000)
        assert app.api("GET", "/api/support/status").json()["enabled"] is True


class TestRunningACheck:
    def test_the_title_check_round_trips_and_shows_what_will_be_copied(self, page: Page, app: ShortlistApp):
        """The whole loop in one test. The copy block is the deliverable — a maintainer reads that
        text and nothing else — so the page must show the SERVER's block, not one it rebuilt."""
        page.goto("/issue")
        page.get_by_role("button", name=re.compile("switch on the checks", re.IGNORECASE)).click()
        expect(page.get_by_text(re.compile("checks are switched on", re.IGNORECASE))).to_be_visible(timeout=20_000)

        page.get_by_role("button", name=re.compile("keep seeing something", re.IGNORECASE)).click()
        page.get_by_label("Title").fill("Teacup")
        page.get_by_role("button", name=re.compile(r"^check$", re.IGNORECASE)).click()

        block = page.locator("pre").first
        expect(block).to_be_visible(timeout=20_000)
        # Stamped and terminated: without both, a paste cannot be told apart from a truncated one.
        expect(block).to_contain_text("=== Shortlist support")
        expect(block).to_contain_text("=== end ===")
        expect(page.get_by_text(re.compile("exactly what", re.IGNORECASE))).to_be_visible()

    def test_a_check_that_needs_a_name_will_not_run_without_one(self, page: Page, app: ShortlistApp):
        """Running with an empty name would return every person's data under a heading naming one."""
        page.goto("/issue")
        page.get_by_role("button", name=re.compile("switch on the checks", re.IGNORECASE)).click()
        expect(page.get_by_text(re.compile("checks are switched on", re.IGNORECASE))).to_be_visible(timeout=20_000)

        page.get_by_role("button", name=re.compile("one person's recommendations", re.IGNORECASE)).click()

        expect(page.get_by_role("button", name=re.compile(r"^check$", re.IGNORECASE))).to_be_disabled()

    def test_every_check_is_reachable_not_just_the_shortcuts(self, page: Page, app: ShortlistApp):
        page.goto("/issue")
        page.get_by_role("button", name=re.compile("switch on the checks", re.IGNORECASE)).click()
        expect(page.get_by_text(re.compile("checks are switched on", re.IGNORECASE))).to_be_visible(timeout=20_000)

        page.get_by_role("button", name=re.compile("show all 21 checks", re.IGNORECASE)).click()

        expect(page.get_by_role("button", name=re.compile("ask plex directly", re.IGNORECASE))).to_be_visible()
        expect(
            page.get_by_role("button", name=re.compile("does plex match our records", re.IGNORECASE))
        ).to_be_visible()


class TestFilingTheReport:
    def test_the_bug_link_and_the_diagnostics_sit_together(self, page: Page, app: ShortlistApp):
        page.goto("/issue")
        page.get_by_role("button", name=re.compile("switch on the checks", re.IGNORECASE)).click()
        expect(page.get_by_text(re.compile("still stuck", re.IGNORECASE))).to_be_visible(timeout=20_000)

        report = page.get_by_role("link", name=re.compile("report a bug on github", re.IGNORECASE))
        expect(report).to_have_attribute("href", re.compile(r"github\.com.*issues/new"))
        expect(page.get_by_role("button", name=re.compile("copy the summary", re.IGNORECASE))).to_be_visible()
        # Says what it masks AND what it keeps, and does not overpromise. The button beside it
        # publishes, so "no passwords or tokens" alone was true and misleading — and any absolute
        # framing is wrong outright: three separate leaks reached a real report while one was on
        # screen. Names are kept and the copy says so; there is no toggle claiming otherwise.
        expect(page.get_by_text(re.compile("passwords, tokens, api keys, ip addresses", re.IGNORECASE))).to_be_visible()
        expect(page.get_by_text(re.compile("rather than a guarantee", re.IGNORECASE))).to_be_visible()
        expect(page.get_by_text(re.compile("plex usernames of people on your server", re.IGNORECASE))).to_be_visible()
        expect(page.get_by_role("checkbox")).to_have_count(0)

    def test_the_downloadable_report_is_real_text_and_carries_no_secrets(self, page: Page, app: ShortlistApp):
        """Fetched through the API rather than clicked, because what matters is the CONTENT: someone
        is about to attach this to a public GitHub issue."""
        app.api("POST", "/api/support/enable")

        body = app.api("GET", "/api/support/bundle.txt").text

        assert body.startswith("=== Shortlist support")
        assert body.rstrip().endswith("=== end ===")
        assert "X-Plex-Token=" not in body or "X-Plex-Token=<redacted>" in body
        for line in body.splitlines():
            assert len(line) <= 76, line


class TestNoConsoleErrors:
    def test_the_page_runs_clean(self, page: Page, app: ShortlistApp):
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.goto("/issue")
        page.get_by_role("button", name=re.compile("switch on the checks", re.IGNORECASE)).click()
        page.wait_for_timeout(3000)
        assert not errors, errors


class TestTheLivePlexChecksActuallyReachPlex:
    """The unit tests monkeypatch the clients, so they cannot catch a method that does not exist.

    One did: `sharing` called `client.users()` where the real method is `list_users()`, and the stub
    in the unit tests carried the same wrong name — so the whole class went green while the tool
    reported "COULD NOT READ: AttributeError" against a real server. These go through the actual
    clients against the fake PMS and fake plex.tv, which is the only layer that would have noticed.
    """

    def test_sharing_reads_the_roster_and_classifies_by_label(self, app: ShortlistApp):
        app.api("POST", "/api/support/enable")

        body = app.api("GET", "/api/support/sharing").json()

        assert body["error"] is None, body["error"]
        assert body["accounts"], "the fake plex.tv roster came back empty"
        for account in body["accounts"]:
            # Labels, never whole clauses — every entry must be one `shortlist_*` value.
            for label in account["shortlist_excludes"]:
                assert label.startswith("shortlist_"), label
            # Nobody is ever expected to hide their OWN row.
            assert f"shortlist_{account['user']}".lower() not in account["should_hide"]

    def test_connection_reads_the_share_tokens(self, app: ShortlistApp):
        app.api("POST", "/api/support/enable")

        body = app.api("GET", "/api/support/connection").json()

        assert body["error"] is None, body["error"]
        assert body["users"], "no enabled users came back"

    def test_drift_compares_the_ledger_against_the_real_server(self, app: ShortlistApp):
        app.api("POST", "/api/support/enable")

        body = app.api("GET", "/api/support/drift").json()

        assert body["error"] is None, body["error"]
        assert "COULD NOT READ PLEX" not in body["text"]

    def test_health_reports_the_fake_server_as_reachable_with_a_real_timing(self, app: ShortlistApp):
        app.api("POST", "/api/support/enable")

        checks = {c["name"]: c for c in app.api("GET", "/api/support/health").json()["checks"]}

        assert checks["Plex server"]["ok"] is True, checks["Plex server"]
        assert checks["Libraries"]["ok"] is True, checks["Libraries"]
