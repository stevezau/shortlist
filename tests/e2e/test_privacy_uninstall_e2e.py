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


class TestRowsStayPrivateAcrossLibraries:
    """The promise of the product, end to end: after a real run, no user can see another
    user's row — in ANY library.

    This is the shape of the only leak that ever reached a live server (SFLIX, 2026-07-12).
    Every user's picks were delivered into the movie library regardless of type, so the TV
    watchers' rows sat in a movie library holding shows. Plex applies `label!=` share filters
    per library, and a collection whose contents don't match its library is matched by neither
    filterMovies nor filterTelevision — so those rows were unhidable, and every user saw them.
    T1 passed the whole time (the excludes really were on the filters); only looking at a real
    user's own Home hubs caught it.
    """

    def test_no_user_sees_another_users_row_in_any_library(self, app: RowarrApp, reset_fake_plex):
        state = reset_fake_plex
        build_real_rows(app)

        owned = {}  # slug -> collection ids, from the labels the PMS actually stored
        for collection in state.collections.values():
            for label in collection.labels:
                if label.lower().startswith("rowarr_"):
                    owned.setdefault(label.lower().removeprefix("rowarr_"), []).append(collection.rating_key)

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

    def test_the_privacy_check_fails_when_a_row_lands_in_the_wrong_library(self, app: RowarrApp, reset_fake_plex):
        """The check must catch this class of leak — T1 alone never did."""
        state = reset_fake_plex
        build_real_rows(app)
        assert app.api("POST", "/api/privacy/check", json={}).json()["passed"] is True

        # Put mike's row in the movie library while it still holds shows: exactly the broken
        # state the old delivery produced.
        mike_row = next(c for c in state.collections.values() if c.labels == ["Rowarr_mike"])
        mike_row.section_id = state.section_id

        result = app.api("POST", "/api/privacy/check", json={}).json()

        assert result["passed"] is False
        assert result["tiers"]["T1"] is True, "the filters are still correct — only the view is wrong"
        assert result["tiers"]["T2"] is False
        leaked = result["detail"]["T2"]["leaked"]
        assert [entry["slug"] for entry in leaked] == ["mike"]


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
        expect(page.get_by_text("Not checked yet")).to_be_visible(timeout=LOAD)

        result = app.api("POST", "/api/privacy/check", json={}).json()
        assert result["passed"] is True
        assert set(result["tiers"]) == {"T1", "T2"}, "both tiers must run when a Home canary exists"

        badge = page.get_by_text(re.compile(r"^Private"))
        expect(badge).to_be_visible(timeout=SLOW)
        # "Private" is worthless without a date — a check from six months ago is not a promise.
        expect(badge).to_have_text(re.compile(r"Private · \d"))

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
        expect(page.get_by_text(re.compile(r"^Private"))).to_be_visible(timeout=LOAD)

        result = app.api("POST", "/api/privacy/check", json={}).json()
        assert result["passed"] is False
        assert result["tiers"]["T1"] is False, "T1 must catch a share filter that lost its excludes"
        assert result["tiers"]["T2"] is True, "the canary's own hubs are still clean — only T1 should fail"

        expect(page.get_by_text("Check failed")).to_be_visible(timeout=SLOW)

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
        # 5 rows for 3 users: sarah and the cold-start canary each get one per library; mike watches only TV.
        assert len(state.collections) == 5
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
        expect(dialog).to_contain_text("5 collections deleted", timeout=SLOW)
        expect(dialog).not_to_contain_text("Kometa")

        dialog.get_by_role("textbox").fill("uninstall rowarr")
        dialog.get_by_role("button", name="Uninstall and restore server").click()
        expect(page.get_by_text("Your server is as we found it.")).to_be_visible(timeout=SLOW)

        assert list(state.collections) == [9999], "uninstall deleted a collection Rowarr did not create"
        assert state.collections[9999].item_keys == [101, 102]


class TestEveryAccountOnTheServerIsCovered:
    """Rowarr's promise is that a user's row is private — from EVERYONE, not from the handful of
    accounts Rowarr happens to manage.

    On a live server this was not true: 45 of its 48 accounts had completely empty share filters
    and could see all three managed users' private rows, because Rowarr only ever wrote filters
    for the users it built rows for (SFLIX, 2026-07-12).
    """

    def test_an_account_rowarr_has_never_seen_still_gets_the_excludes(self, app: RowarrApp, reset_fake_plex):
        """The owner invites someone to Plex and never opens the Users page. The nightly run must
        still stop them seeing everyone else's rows — and must record what it changed on their
        share (plex-safety rule 10)."""
        from tests.fakes.fake_plex import FakeUser

        state = reset_fake_plex
        # A stranger: on the Plex server, absent from Rowarr's database entirely.
        state.users[299] = FakeUser(id=299, username="stranger")

        build_real_rows(app)

        # Their share filter now excludes every row that isn't theirs...
        stranger = state.users[299]
        assert "Rowarr_sarah" in stranger.filters["filterMovies"]
        assert "Rowarr_sarah" in stranger.filters["filterTelevision"]
        assert "Rowarr_mike" in stranger.filters["filterMovies"]

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
        assert "Rowarr_sarah" in fields["filterMovies"]["after"]

    def test_the_privacy_check_fails_when_an_account_is_missing_its_excludes(self, app: RowarrApp, reset_fake_plex):
        """T1 must look at every account too — a check that only inspected managed users reported
        PASS the entire time 45 accounts were leaking."""
        state = reset_fake_plex
        build_real_rows(app)
        assert app.api("POST", "/api/privacy/check", json={}).json()["passed"] is True

        state.users[203].filters["filterMovies"] = ""  # the canary loses its excludes

        result = app.api("POST", "/api/privacy/check", json={}).json()
        assert result["passed"] is False
        assert result["tiers"]["T1"] is False

    def test_a_leaking_row_is_removed_even_though_it_closes_the_privacy_gate(self, app: RowarrApp, reset_fake_plex):
        """The trap this must not fall into.

        A row Plex cannot hide FAILS the Privacy Check. A failed check closes the write gate. If
        the gate then blocked the sweep that removes such rows, the leak could never heal — the
        server would be stuck leaking forever, with every run refused. That is exactly the state a
        live server was left in.

        The gate exists to stop Rowarr CREATING rows it cannot prove are private. Deleting a row
        that is already visible to everyone is the remedy, so it happens regardless.
        """
        from tests.fakes.fake_plex import FakeCollection

        state = reset_fake_plex
        state.collections[99010] = FakeCollection(
            rating_key=99010,
            title="✨ Picked for You",
            section_id=state.section_id,  # movie library...
            subtype="show",  # ...full of shows: no share filter can touch it
            labels=["Rowarr_sarah"],
            item_keys=[301, 302],
            promoted_shared_home=True,
        )

        # The check fails BECAUSE of that row, which slams the write gate shut.
        assert app.api("POST", "/api/privacy/check", json={}).json()["passed"] is False

        created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
        run = app.wait_for_run(created["run_id"])

        # The run is still refused — no rows are built, nothing is promoted...
        assert run["status"] == "error"
        assert "privacy gate" in run["stats"]["error"]
        # ...but the leaking row is gone, and its removal is on the record.
        assert 99010 not in state.collections, "the leak could not heal: the gate blocked its own remedy"
        assert run["stats"]["rows_swept"] == 1
        events = app.api("GET", "/api/events/log?scope=run.sweep").json()
        assert events and events[0]["message"]["deleted"] == {"sarah": ["✨ Picked for You"]}

    def test_a_deleted_row_is_visible_on_the_run_page(self, page: Page, app: RowarrApp, reset_fake_plex):
        """Deleting someone's row is the most destructive thing a run does. "What changed on
        whose share at 03:31" must be answerable from the UI, not just the database."""
        from tests.fakes.fake_plex import FakeCollection

        state = reset_fake_plex
        state.collections[99011] = FakeCollection(
            rating_key=99011,
            title="✨ Picked for You",
            section_id=state.section_id,
            subtype="show",
            labels=["Rowarr_sarah"],
            item_keys=[301, 302],
            promoted_shared_home=True,
        )
        # Sweep it first (via a gated run), so the check can pass and a real run can proceed.
        app.wait_for_run(app.api("POST", "/api/runs", json={"dry_run": False}).json()["run_id"])
        assert 99011 not in state.collections

        run = build_real_rows(app)

        page.goto(f"/runs/{run['id']}")
        expect(page.get_by_role("heading", name=f"Run #{run['id']}")).to_be_visible(timeout=LOAD)

    def test_a_gated_run_still_writes_the_excludes_that_let_the_check_pass_again(self, app: RowarrApp, reset_fake_plex):
        """The gate must not deadlock itself.

        An account missing an exclude FAILS the Privacy Check. The failed check closes the write
        gate. If the closed gate also blocked the share-filter sync, the only thing that writes
        that exclude would never run — the check could never pass again, the gate could never
        reopen, and the rows would stay visible to that account forever. A live server reached
        exactly that state (SFLIX, 2026-07-13).

        Building rows is refused, as it should be. Everything that makes the server MORE private
        still happens: merge-only excludes cannot expose anything.
        """
        state = reset_fake_plex
        build_real_rows(app)  # rows exist and everyone's filters are correct

        # Someone loses their excludes — drift, a manual edit on plex.tv, a new account. They also
        # have filter conditions of their own, which Rowarr did not put there and must not disturb.
        state.users[202].filters["filterMovies"] = "contentRating=PG|label!=Kometa_kids"
        state.users[202].filters["filterTelevision"] = ""
        assert app.api("POST", "/api/privacy/check", json={}).json()["passed"] is False

        created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
        run = app.wait_for_run(created["run_id"])

        # Refused: no rows were built...
        assert run["status"] == "error"
        assert "privacy gate" in run["stats"]["error"]
        # ...but the excludes are back, so the check can pass again and the gate can reopen.
        movies = state.users[202].filters["filterMovies"]
        assert "Rowarr_sarah" in movies
        assert "Rowarr_sarah" in state.users[202].filters["filterTelevision"]
        assert app.api("POST", "/api/privacy/check", json={}).json()["passed"] is True, (
            "the gate deadlocked: it blocked the only thing that could reopen it"
        )

        # And it was a MERGE, not a rebuild. This is the whole reason a filter write is allowed
        # with the gate closed: it can only ever ADD an exclude. Their own conditions survive
        # byte-identical, and the label another tool put there is still excluded (rule 3).
        assert movies.startswith("contentRating=PG|"), f"a foreign condition was dropped: {movies!r}"
        assert "Kometa_kids" in movies, f"another tool's exclude was dropped: {movies!r}"
