"""API contract tests: the users list, the plex.tv roster sync, and per-user rows."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from shortlist.server.auth import SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server, User
from shortlist.server.settings_store import SettingsStore
from tests.integration.conftest import OWNER_ID, OWNER_JSON

pytestmark = pytest.mark.integration


class TestUsersApi:
    def test_list_and_patch(self, client: TestClient):
        users = client.get("/api/users").json()
        assert [u["username"] for u in users] == ["mike", "sarah"]
        target = next(u for u in users if u["username"] == "mike")
        # NB: `row_size`/`max_rating` used to live in prefs and were read by NOTHING. Per-person row
        # size is a row override (PUT /users/{id}/rows/{cid}); a maturity cap filtered no content at
        # all, so it was removed rather than left looking enforced.
        r = client.patch(f"/api/users/{target['id']}", json={"enabled": True, "prefs": {"excluded_genres": ["Horror"]}})
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert r.json()["prefs"]["excluded_genres"] == ["Horror"]

    def test_a_nickname_changes_the_row_title_but_never_the_label(self, client: TestClient):
        """Plex usernames are often a handle nobody uses, and `{user}` put it on a Home screen (#4).
        The slug — and so `shortlist_<slug>`, which every other account's share filter excludes —
        must not move, or the old exclusions would point at nothing and the row would go public."""
        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        assert target["display_name"] == "sarah"

        r = client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})

        assert r.status_code == 200
        assert r.json()["nickname"] == "Sarah B"
        assert r.json()["display_name"] == "Sarah B"
        assert r.json()["slug"] == target["slug"], "the label must not move when someone is renamed"

    def test_a_nickname_change_retitles_the_collection_already_on_plex(self, client: TestClient, monkeypatch):
        """`{user}` renders the display name, so renaming someone gives their collections a title no
        future run will ever write. The next run then builds a SECOND one under the new name and the
        old one lives on, labelled and promoted, with nothing able to address it.

        The reconcile therefore renders the OLD name to find the collection and the NEW one to set —
        rendering the new name on both sides would match nothing and silently do nothing."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import EngineConfig

        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("row.name_template", "{user}'s Picks")
            session.commit()

        renames: list[tuple[str, str]] = []
        marker = row_marker(target["plex_account_id"])
        col = MagicMock(title="sarah's Picks" + marker)
        col.editTitle.side_effect = lambda new: renames.append((col.title, new))
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda s, label: [col] if label == "shortlist_sarah" else []
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)

        assert client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"}).status_code == 200

        assert renames == [("sarah's Picks" + marker, "Sarah B's Picks" + marker)]

    def test_clearing_a_nickname_falls_back_to_tautullis_name_then_plex(self, client: TestClient):
        from shortlist.server.db.models import User

        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        with client.app.state.sessions() as session:
            session.get(User, target["id"]).friendly_name = "Sazza"
            session.commit()

        client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})
        assert self._one(client, target["id"])["display_name"] == "Sarah B"

        cleared = client.patch(f"/api/users/{target['id']}", json={"nickname": ""}).json()

        assert cleared["nickname"] == ""
        assert cleared["display_name"] == "Sazza", "a blank nickname falls back to Tautulli's name"

    def test_a_nickname_that_collides_with_someone_else_is_refused(self, client: TestClient):
        """`{user}` renders display_name, and only the USERNAME is unique on Plex. Two people
        resolving to one display name ask for two collections with one title in one library, which
        PMS refuses — so that person's row would fail every night with a generic-looking Plex
        error. Caught at the point of entry instead, where it can be explained."""
        users = client.get("/api/users").json()
        sarah = next(u for u in users if u["username"] == "sarah")
        other = next(u for u in users if u["id"] != sarah["id"])
        client.patch(f"/api/users/{sarah['id']}", json={"nickname": "The Boss"})

        r = client.patch(f"/api/users/{other['id']}", json={"nickname": "the boss"})

        assert r.status_code == 409, "case differences still collide — Plex titles are not case-sensitive"
        assert "already shows up as" in r.json()["detail"]
        assert self._one(client, other["id"])["nickname"] == "", "the rejected name must not be stored"

    @pytest.mark.parametrize("source", ["friendly_name", "username"])
    def test_a_clash_is_caught_whichever_name_the_other_person_shows_under(self, client: TestClient, source: str):
        """display_name has three sources (nickname → Tautulli → Plex) and any of them can be the
        thing you collide with — the Tautulli one especially, now that a sync adopts it."""
        from shortlist.server.db.models import User

        users = client.get("/api/users").json()
        sarah = next(u for u in users if u["username"] == "sarah")
        other = next(u for u in users if u["id"] != sarah["id"])
        if source == "friendly_name":
            with client.app.state.sessions() as session:
                session.get(User, sarah["id"]).friendly_name = "Sazza"
                session.commit()
            taken = "Sazza"
        else:
            taken = sarah["username"]

        r = client.patch(f"/api/users/{other['id']}", json={"nickname": taken})

        assert r.status_code == 409
        assert f"already shows up as “{taken}”" in r.json()["detail"], "the message names what THEY show as"

    def test_clearing_a_nickname_into_someone_elses_name_is_also_refused(self, client: TestClient):
        """Clearing is the one input that reaches a colliding name without anyone typing it."""
        from shortlist.server.db.models import User

        users = client.get("/api/users").json()
        sarah = next(u for u in users if u["username"] == "sarah")
        other = next(u for u in users if u["id"] != sarah["id"])
        client.patch(f"/api/users/{other['id']}", json={"nickname": "Distinct Name"})
        with client.app.state.sessions() as session:
            session.get(User, other["id"]).friendly_name = sarah["username"]
            session.commit()

        r = client.patch(f"/api/users/{other['id']}", json={"nickname": ""})

        assert r.status_code == 409

    def test_a_nickname_can_be_re_saved_without_colliding_with_itself(self, client: TestClient):
        target = next(u for u in client.get("/api/users").json() if u["username"] == "sarah")
        client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})

        r = client.patch(f"/api/users/{target['id']}", json={"nickname": "Sarah B"})

        assert r.status_code == 200

    def _one(self, client: TestClient, user_id: int) -> dict:
        return next(u for u in client.get("/api/users").json() if u["id"] == user_id)

    def test_watch_history_depth_is_read_from_prefs_not_gated_on_a_run(self, client: TestClient):
        """`history_depth` is refreshed for EVERY enabled user by the daily watch sync (the share-token
        read returns one item per distinct title, so a binge is one). It must surface without waiting
        for a run to PROCESS someone — a skipped or never-run user used to read "0 titles" forever (the
        beta bug that reported 0 for all 42 accounts while the log showed watches synced)."""
        from shortlist.server.db.models import User

        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(username="sarah").one()
            # The sync writes the distinct-title count the share-token source returned; no run yet.
            user.prefs = {**(user.prefs or {}), "history_depth": 2}
            session.commit()
            user_id = user.id

        listed = next(u for u in client.get("/api/users").json() if u["id"] == user_id)
        assert listed["history_depth"] == 2, "a binge is one title, and it must not need a run to show"

        # The PATCH response carries the same real number, not a stale zero.
        patched = client.patch(f"/api/users/{user_id}", json={"enabled": True}).json()
        assert patched["history_depth"] == 2

    def test_patch_unknown_user_404(self, client: TestClient):
        assert client.patch("/api/users/9999", json={"enabled": True}).status_code == 404

    def test_disabling_a_user_removes_their_rows_from_plex(self, client: TestClient, monkeypatch):
        """Turning a user off deletes every collection under their label — not just stops delivery.

        One user_type suffices: cleanup is by the user's whole label (displays=None), so it's agnostic
        to shared/managed/owner; enable and no-op transitions are guarded by `user.enabled and False`.
        """
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import EngineConfig
        from shortlist.server.db.models import User

        target = next(u for u in client.get("/api/users").json() if u["username"] == "mike")
        client.patch(f"/api/users/{target['id']}", json={"enabled": True})  # ensure the transition is off->on->off
        with client.app.state.sessions() as session:
            slug = session.get(User, target["id"]).slug

        deleted: list[str] = []
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda s, label: (
            [SimpleNamespace(title="✨ Picked for You" + row_marker(0))] if label == f"shortlist_{slug}" else []
        )
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)

        r = client.patch(f"/api/users/{target['id']}", json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert len(deleted) == 1  # their collection was removed by their whole label

    def test_set_all_users_enabled_toggles_everyone_and_cleans_up_on_disable(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        # Record the actual Plex removals, keyed by the shortlist label they came in on.
        removed_labels: list[str] = []
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(title="Movies")]
        plex.find_owned_collections.side_effect = lambda section, label: [SimpleNamespace(title=label, _label=label)]
        plex.delete_owned_collection.side_effect = lambda collection, prefix: removed_labels.append(collection._label)
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)

        # Enable all -> everyone on, and NOTHING removed from Plex.
        r = client.post("/api/users/set-enabled", json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"] is True
        assert all(u["enabled"] for u in client.get("/api/users").json())
        assert removed_labels == []  # enabling never triggers a Plex write

        # Disable all -> everyone off, and BOTH users' rows actually removed (by their own label).
        r = client.post("/api/users/set-enabled", json={"enabled": False})
        assert r.status_code == 200 and r.json()["cleaned"] == 2
        assert set(removed_labels) == {"shortlist_sarah", "shortlist_mike"}
        assert not any(u["enabled"] for u in client.get("/api/users").json())


class TestUserSync:
    """`POST /users/sync` — the roster from plex.tv, PLUS the owner, who is never in that list.

    Covers the `user_type` matrix's owner cell: without this the person running Shortlist has no
    user row at all, so a one-person server gets no rows (issue #1).
    """

    @pytest.fixture
    def plextv(self, client: TestClient):
        """Both plex.tv reads at the HTTP boundary, so the response SHAPES are under test too —
        the roster from the recorded fixture, the owner from the payload above."""

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.commit()

        users_xml = (Path(__file__).parent.parent / "fixtures" / "plextv_users.xml.txt").read_text()
        with respx.mock:
            roster = respx.get("https://plex.tv/api/users").mock(return_value=httpx.Response(200, text=users_xml))
            whoami = respx.get("https://plex.tv/api/v2/user").mock(
                return_value=httpx.Response(200, json=dict(OWNER_JSON))
            )
            yield SimpleNamespace(roster=roster, whoami=whoami)

    def _users(self, client: TestClient) -> dict[str, dict]:
        return {u["username"]: u for u in client.get("/api/users").json()}

    def test_a_tautulli_rename_triggers_the_same_row_reconcile_a_nickname_does(
        self, client: TestClient, plextv, monkeypatch
    ):
        """`{user}` renders the friendly name, so a Tautulli rename changes every row title — and the
        collections already on Plex still carry the old one. Without this reconcile a multi-row user
        keeps the stale copy alongside the new one forever: `remove_row` matches by rendered title,
        so no sweep ever collects it."""
        from shortlist.server.services import user_sync

        calls: list = []

        async def spy(state, was_called):
            if was_called:  # a no-op call with nothing renamed is not Plex work
                calls.append(was_called)

        monkeypatch.setattr(user_sync, "_rename_after_nickname", spy)
        monkeypatch.setattr(
            user_sync.TautulliClient, "friendly_names", lambda self: {555000100: "Sazza"}, raising=False
        )

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("tautulli.url", "http://tautulli:8181")
            session.commit()

        client.post("/api/users/sync")

        assert self._users(client)["sarah"]["display_name"] == "Sazza"
        assert len(calls) == 1, "a changed display name must reconcile the rows already on Plex"
        # The PREVIOUS name goes with it. It is what their collections are still titled with, and
        # rendering `{user}` from the NEW name on both sides matches nothing — leaving the stale
        # collection in place for the next run to duplicate.
        assert calls[0]["sarah"] == "sarah"

    def test_sync_streams_a_fetch_phase_then_a_save_bar_and_a_finish(self, client: TestClient, plextv, monkeypatch):
        """The Tools page bar reads these events: one indeterminate `fetch`, a determinate `save`
        count, then `sync.finished` echoing the same counts the POST returns."""
        published: list[tuple[str, dict]] = []
        real_publish = client.app.state.bus.publish
        monkeypatch.setattr(
            client.app.state.bus,
            "publish",
            lambda event, data: (published.append((event, data)), real_publish(event, data))[0],
        )

        body = client.post("/api/users/sync").json()

        events = [(e, d) for e, d in published if e in ("sync.progress", "sync.finished")]
        assert all(d["kind"] == "users" for _, d in events), "every sync event must be tagged for the users card"
        phases = [d.get("phase") for e, d in events if e == "sync.progress"]
        assert phases[0] == "fetch", "the opaque plex.tv call is announced as an indeterminate fetch first"
        assert "save" in phases, "the roster upsert drives a determinate save bar"
        save_events = [d for e, d in events if e == "sync.progress" and d.get("phase") == "save"]
        assert save_events[-1]["done"] == save_events[-1]["total"], "the save bar reaches 100%"
        # The finish event carries the same numbers the HTTP response does, so the bar can settle on them.
        finished = next(d for e, d in events if e == "sync.finished")
        assert finished["ok"] is True
        assert {k: finished[k] for k in ("added", "updated", "total")} == body

    def test_a_sync_that_changes_no_name_does_no_plex_work(self, client: TestClient, plextv, monkeypatch):
        """The reconcile does Plex I/O — it must not fire on every routine sync."""
        from shortlist.server.services import user_sync

        calls: list = []

        async def spy(state, was_called):
            if was_called:  # a no-op call with nothing renamed is not Plex work
                calls.append(was_called)

        monkeypatch.setattr(user_sync, "_rename_after_nickname", spy)

        client.post("/api/users/sync")  # the fixture renames one account, so this one DOES reconcile
        calls.clear()
        client.post("/api/users/sync")

        assert calls == []

    def test_a_sync_that_finds_new_accounts_queues_the_share_filter_pass(self, client: TestClient, plextv):
        """A brand-new account has NO excludes while every existing row is already on Shared Home,
        so until its filter is written it sees EVERY user's private row. Waiting for the nightly run
        leaves that open for up to a day.

        Asserted at the JOB boundary, not by spying on the helper: the whole mechanism lives inside
        that helper, so mocking it would let the enqueue be deleted with the test still green — and
        the leak would be back.
        """
        client.post("/api/users/sync")  # first sync -> the whole roster is new

        assert [j["kind"] for j in client.get("/api/system/jobs").json()] == ["privacy.sync"]

    def test_a_sync_that_adds_nobody_writes_no_filters(self, client: TestClient, plextv, monkeypatch):
        """Filter writes are throttled plex.tv traffic across every account — a routine no-change
        sync must not trigger a server-wide pass."""
        from shortlist.server.services import user_sync

        calls: list = []

        async def spy(state, added):
            calls.append(added)

        monkeypatch.setattr(user_sync, "_hide_existing_rows_from_new_accounts", spy)

        client.post("/api/users/sync")
        calls.clear()
        client.post("/api/users/sync")  # everyone already known

        assert calls == []

    def test_a_failure_to_write_the_new_filters_never_fails_the_sync(self, client: TestClient, plextv, monkeypatch):
        """The accounts are already saved and the nightly run is still the backstop, so a Plex
        outage here must not fail the sync the operator asked for."""

        def explode(state, dry_run):
            raise RuntimeError("Plex is down")

        monkeypatch.setattr(client.app.state.run_service, "build_context", explode)

        assert client.post("/api/users/sync").status_code == 200

    def test_a_home_read_blip_never_wipes_a_stored_restriction_profile(self, client: TestClient, plextv, monkeypatch):
        """`/api/home/users` is a best-effort enrichment: when it fails, every profile comes back "".
        Saving that blindly WIPED correct values for everyone — the UI badge vanished and rows got
        built for parental-controlled kids until the next sync happened to heal it.

        A profile we could not read is unknown, and unknown must not overwrite what we already knew.
        """
        import shortlist.engine.clients.plextv as plextv_mod
        from shortlist.server.db.models import User

        # A healthy Home read: the kid's profile is known and gets stored.
        monkeypatch.setattr(
            plextv_mod.PlexTvClient, "home_restriction_profiles", lambda self: {555000200: "little_kid"}
        )
        monkeypatch.setattr(plextv_mod.PlexTvClient, "home_profile_known", lambda self, account_id: True)
        client.post("/api/users/sync")
        with client.app.state.sessions() as session:
            kid = session.query(User).filter_by(plex_account_id=555000200).one()
            assert kid.restriction_profile == "little_kid", "precondition: the profile was stored"

        # Now the Home endpoint goes down. list_users() still returns everyone, profiles all blank.
        monkeypatch.setattr(plextv_mod.PlexTvClient, "home_restriction_profiles", lambda self: {})
        monkeypatch.setattr(plextv_mod.PlexTvClient, "home_profile_known", lambda self, account_id: False)

        assert client.post("/api/users/sync").status_code == 200

        with client.app.state.sessions() as session:
            kid = session.query(User).filter_by(plex_account_id=555000200).one()
            assert kid.restriction_profile == "little_kid", "a failed read must not erase a known profile"

    def test_sync_adds_the_owner_disabled_and_badged(self, client: TestClient, plextv):
        r = client.post("/api/users/sync")
        assert r.status_code == 200
        # Two of the fixture's three accounts are already in the DB (sarah, and 555000200 whom
        # plex.tv now calls "kid"). ADDED are the owner plex.tv never returns, plus 555000300 —
        # the issue-#20 account: managed with no parental profile.
        assert r.json() == {"added": 2, "updated": 2, "total": 4}

        owner = self._users(client)["steve"]
        assert owner["user_type"] == "owner"
        assert owner["plex_account_id"] == OWNER_ID
        assert owner["slug"] == "steve"
        assert owner["avatar_url"] == "https://plex.tv/users/abc/avatar"
        # Off by default: an existing install gains a user to switch on, not a row that turns up on
        # the owner's Home unannounced.
        assert owner["enabled"] is False

    def test_the_owner_lookup_is_authenticated_as_the_stored_plex_token(self, client: TestClient, plextv):
        """The token decides WHOSE account comes back, so sending the wrong one (or none) would
        either 401 or identify somebody else entirely."""
        client.post("/api/users/sync")

        headers = plextv.whoami.calls.last.request.headers
        assert headers["X-Plex-Token"] == "owner-token"
        assert headers["X-Plex-Client-Identifier"] == client.app.state.client_id

    def test_re_syncing_updates_the_owner_instead_of_duplicating_them(self, client: TestClient, plextv):
        client.post("/api/users/sync")
        plextv.whoami.mock(return_value=httpx.Response(200, json={**OWNER_JSON, "username": "steve-renamed"}))

        r = client.post("/api/users/sync")

        assert r.json()["added"] == 0  # nobody new the second time
        by_name = self._users(client)
        assert "steve" not in by_name
        assert by_name["steve-renamed"]["user_type"] == "owner"
        with client.app.state.sessions() as session:
            assert session.query(User).filter_by(plex_account_id=OWNER_ID).count() == 1

    def test_a_token_belonging_to_another_account_never_becomes_the_owner(self, client: TestClient, plextv):
        """Fail-safe: building a row from this account's history and labelling it the owner's would
        hand one person another's picks, so a mismatched token syncs the shared users and stops."""
        plextv.whoami.mock(return_value=httpx.Response(200, json={**OWNER_JSON, "id": 999999}))

        r = client.post("/api/users/sync")

        assert r.status_code == 200
        assert r.json()["total"] == 3  # the fixture's shared users only
        names = self._users(client)
        assert not any(u["user_type"] == "owner" for u in names.values())

    def test_a_re_link_under_a_new_admin_demotes_the_previous_owner(self, client: TestClient, plextv):
        """`owner` is the type `sync_user_restrictions` skips, so a stale one keeps an account
        exempt from restriction on a server it only shares."""
        client.post("/api/users/sync")
        with client.app.state.sessions() as session:
            session.query(Server).first().owner_account_id = 555000999
            session.commit()
        # Re-linking re-homes the whole instance: the previous admin's session stops being the
        # owner's session, so the new admin signs in before anything else happens.
        client.cookies.set(
            SESSION_COOKIE,
            session_serializer(client.app.state.session_secret).dumps(
                {"account_id": 555000999, "username": "new-admin"}
            ),
        )
        plextv.whoami.mock(
            return_value=httpx.Response(200, json={**OWNER_JSON, "id": 555000999, "username": "new-admin"})
        )

        client.post("/api/users/sync")

        by_name = self._users(client)
        assert by_name["new-admin"]["user_type"] == "owner"
        assert by_name["steve"]["user_type"] == "shared"  # the old owner keeps their row, loses the exemption

    @pytest.mark.parametrize(
        ("response", "why"),
        [
            (httpx.Response(500, text="plex.tv is having a day"), "plex.tv errors"),
            (httpx.Response(200, json={"subscription": {"active": True}}), "the payload has no id"),
        ],
    )
    def test_shared_users_still_sync_when_the_owner_lookup_fails(self, client: TestClient, plextv, response, why):
        """The owner is a bonus on top of the roster — a bad answer on that one call must not leave
        everybody else's list stale, whether it fails at the wire or at the payload."""
        plextv.whoami.mock(return_value=response)

        r = client.post("/api/users/sync")

        assert r.status_code == 200, why
        assert r.json()["total"] == 3, why  # every shared user still landed
        assert "sarah" in self._users(client), why
        assert not any(u["user_type"] == "owner" for u in self._users(client).values()), why


class TestUserRowsApi:
    def _sarah_id(self, client: TestClient) -> int:
        return next(u["id"] for u in client.get("/api/users").json() if u["slug"] == "sarah")

    def test_rows_lists_the_default_row_with_no_picks_yet(self, client: TestClient):
        uid = self._sarah_id(client)
        rows = client.get(f"/api/users/{uid}/rows").json()
        assert [r["slug"] for r in rows] == ["picked"]
        assert rows[0]["is_default"] is True
        assert rows[0]["muted"] is False
        assert rows[0]["picks"] == []

    def test_override_mute_and_resize_round_trip(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        r = client.put(f"/api/users/{uid}/rows/{cid}", json={"muted": True, "row_size": 20})
        assert r.status_code == 200
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert row["muted"] is True
        assert row["override"]["row_size"] == 20

    def test_size_override_can_be_reset_to_default(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["row_size"] == 20
        # Sending an explicit null (the "Default" choice) must clear it, not be ignored.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": None})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["row_size"] is None

    def test_mute_toggle_preserves_a_saved_size(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        # A mute toggle sends only {muted}; the saved size must survive it.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"muted": True})
        row = client.get(f"/api/users/{uid}/rows").json()[0]
        assert row["muted"] is True
        assert row["override"]["row_size"] == 20

    def test_recent_count_override_round_trips_and_resets(self, client: TestClient):
        uid = self._sarah_id(client)
        row0 = client.get(f"/api/users/{uid}/rows").json()[0]
        cid = row0["collection_id"]
        # The row reports its own effective depth (the global default) so the UI knows what "default" means.
        assert row0["recent_count"] == 10
        assert row0["override"]["recent_count"] is None

        client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 3})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["recent_count"] == 3
        # An explicit null clears it back to the row's own depth, exactly like the size override.
        client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": None})
        assert client.get(f"/api/users/{uid}/rows").json()[0]["override"]["recent_count"] is None

    def test_recent_count_and_size_overrides_are_independent(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        client.put(f"/api/users/{uid}/rows/{cid}", json={"row_size": 20})
        # A recent_count-only PUT must not clobber the saved size (server writes only fields it's sent).
        client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 5})
        override = client.get(f"/api/users/{uid}/rows").json()[0]["override"]
        assert override == {"row_size": 20, "recent_count": 5}

    def test_recent_count_override_is_bounded(self, client: TestClient):
        uid = self._sarah_id(client)
        cid = client.get(f"/api/users/{uid}/rows").json()[0]["collection_id"]
        # Same 1..25 bounds the global/row settings validate — out of range is a 422, not a bad save.
        assert client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 0}).status_code == 422
        assert client.put(f"/api/users/{uid}/rows/{cid}", json={"recent_count": 26}).status_code == 422

    def test_override_on_unknown_user_or_row_404(self, client: TestClient):
        assert client.put("/api/users/9999/rows/1", json={"muted": True}).status_code == 404
        uid = self._sarah_id(client)
        assert client.put(f"/api/users/{uid}/rows/9999", json={"muted": True}).status_code == 404

    def test_runs_empty_then_unknown_user_404(self, client: TestClient):
        uid = self._sarah_id(client)
        assert client.get(f"/api/users/{uid}/runs").json() == []
        assert client.get("/api/users/9999/runs").status_code == 404

    def test_user_runs_carry_their_own_outcome_not_the_runs(self, client: TestClient):
        """A run is server-wide: its status says whether the RUN succeeded, which says nothing about
        whether this person got a row. `skipped` with a reason is healthy and must read that way."""
        from shortlist.server.db.models import Run, RunUser

        uid = self._sarah_id(client)
        with client.app.state.sessions() as session:
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=uid,
                    status="skipped",
                    reason="no watch history yet",
                    duration_ms=1200,
                )
            )
            session.commit()

        entry = client.get(f"/api/users/{uid}/runs").json()[0]
        assert entry["status"] == "skipped"
        assert entry["reason"] == "no watch history yet"
        assert entry["duration_ms"] == 1200
        assert entry["run_status"] == "ok"  # the run itself was fine

    def test_user_runs_summary_counts_the_runs_that_left_them_out(self, client: TestClient):
        """Six entries on a person's page read as "the server ran six times" without this."""
        from shortlist.server.db.models import Run, RunUser

        uid = self._sarah_id(client)
        with client.app.state.sessions() as session:
            included = Run(trigger="manual", status="ok")
            session.add_all([included, Run(trigger="manual", status="ok"), Run(trigger="manual", status="ok")])
            session.flush()
            session.add(RunUser(run_id=included.id, user_id=uid, status="ok"))
            session.commit()

        assert client.get(f"/api/users/{uid}/runs/summary").json() == {"included": 1, "total": 3}
        assert client.get("/api/users/9999/runs/summary").status_code == 404
