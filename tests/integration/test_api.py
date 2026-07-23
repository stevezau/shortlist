"""API contract tests — full app via TestClient (real lifespan, tmp SQLite, forged owner session)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server, Setting, User
from shortlist.server.main import create_app
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration

OWNER_ID = 555000001

# plex.tv `GET /api/v2/user` — the same payload the PIN login (auth.py) and the wizard's capability
# probe already read from a live server; `thumb` is the one key the user sync adds on top.
OWNER_JSON = {
    "id": OWNER_ID,
    "uuid": "abc123",
    "username": "steve",
    "title": "Steve",
    "email": "steve@example.com",
    "thumb": "https://plex.tv/users/abc/avatar",
    "subscription": {"active": True},
}


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(config_dir=tmp_path)
    with TestClient(app) as test_client:
        # Link a server so owner checks are active, and add users.
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="m1",
                    url="http://pms:32400",
                    token_enc="x",
                    owner_account_id=OWNER_ID,
                    plex_pass=True,
                    capabilities={},
                )
            )
            session.add(User(plex_account_id=555000100, username="sarah", slug="sarah", enabled=True))
            session.add(User(plex_account_id=555000200, username="mike", slug="mike"))
            session.commit()
        cookie = session_serializer(app.state.session_secret).dumps({"account_id": OWNER_ID, "username": "owner"})
        test_client.cookies.set(SESSION_COOKIE, cookie)
        test_client.headers[CSRF_HEADER] = "1"
        yield test_client


class TestAuthBoundary:
    def test_health_needs_no_auth(self, client: TestClient):
        fresh = TestClient(client.app)
        with fresh:
            r = fresh.get("/api/system/health")
            assert r.status_code == 200
            assert r.json()["status"] == "ok"

    def test_users_requires_session(self, client: TestClient):
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/users").status_code == 401

    def test_non_owner_session_rejected_everywhere(self, client: TestClient):
        """A session issued during the pre-link window must lose access once an owner exists."""
        cookie = session_serializer(client.app.state.session_secret).dumps({"account_id": 999, "username": "intruder"})
        client.cookies.set(SESSION_COOKIE, cookie)
        assert client.get("/api/users").status_code == 403
        assert client.get("/api/runs").status_code == 403
        assert client.get("/api/settings").status_code == 403
        assert client.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).status_code == 403
        assert client.get("/api/setup/state").status_code == 403

    def test_mutations_require_csrf_header(self, client: TestClient):
        del client.headers[CSRF_HEADER]
        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
        r = client.patch(f"/api/users/{user_id}", json={"enabled": True})
        assert r.status_code == 403
        assert CSRF_HEADER in r.json()["detail"]


class TestApiToken:
    """The owner API token: generate once, authenticate with Bearer, revoke — and the hash never
    leaks through the settings endpoint."""

    def test_generate_authenticate_and_revoke_round_trip(self, client: TestClient):
        made = client.post("/api/system/api-token")
        assert made.status_code == 200
        token = made.json()["token"]
        assert token.startswith("shl_")

        # The owner can read the token back (revealable, like Sonarr/Radarr) — encrypted at rest but
        # returned in plaintext to the authenticated owner on this dedicated, owner-gated endpoint.
        status = client.get("/api/system/api-token").json()
        assert status["enabled"] is True
        assert status["token"] == token

        # …but it must NEVER surface via the general settings endpoint (private + secret).
        settings = client.get("/api/settings").json()
        assert not any(key.startswith("api.token") for key in settings)

        # A cookie-less, CSRF-less client authenticates with only the Bearer token.
        bare = TestClient(client.app)
        ok = bare.get("/api/users", headers={"Authorization": f"Bearer {token}"})
        assert ok.status_code == 200
        assert [u["username"] for u in ok.json()] == ["mike", "sarah"]

        # A wrong token is rejected, never falling through to anonymous access.
        assert bare.get("/api/users", headers={"Authorization": "Bearer shl_wrong"}).status_code == 401

        # Revoke → the previously-valid token stops working immediately.
        assert client.delete("/api/system/api-token").status_code == 200
        assert bare.get("/api/users", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        assert client.get("/api/system/api-token").json()["enabled"] is False

    def test_a_bad_bearer_fails_closed_even_with_a_valid_owner_cookie(self, client: TestClient):
        # `client` carries a valid owner cookie + CSRF. A wrong Bearer must NOT fall through to it —
        # it fails closed with the token-specific 401, proving the cookie isn't honored alongside a
        # (bad) token. Guards the exact regression the unit test's discriminating detail also covers.
        r = client.get("/api/users", headers={"Authorization": "Bearer shl_wrong"})
        assert r.status_code == 401
        assert r.json()["detail"] == "invalid or revoked API token"

    def test_the_token_is_stored_encrypted_not_plaintext(self, client: TestClient):
        token = client.post("/api/system/api-token").json()["token"]
        with client.app.state.sessions() as session:
            raw = session.get(Setting, "api.token").value["v"]
        assert raw != token  # ciphertext at rest, not the plaintext
        # …and the store decrypts it back to the original.
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            assert store.get("api.token") == token

    def test_legacy_hash_keys_never_leak_via_settings(self, client: TestClient):
        # The prior hash-only version stored these as NON-secret keys; on an upgraded DB they must not
        # surface in GET /api/settings. They're tombstoned in PRIVATE_KEYS regardless of boot purge.
        with client.app.state.sessions() as session:
            session.add(Setting(key="api.token_hash", value={"v": "deadbeef"}))
            session.add(Setting(key="api.token_hint", value={"v": "wxyz"}))
            session.commit()
        settings = client.get("/api/settings").json()
        assert "api.token_hash" not in settings
        assert "api.token_hint" not in settings

    def test_a_non_owner_cannot_mint_a_token(self, client: TestClient):
        cookie = session_serializer(client.app.state.session_secret).dumps({"account_id": 999, "username": "intruder"})
        client.cookies.set(SESSION_COOKIE, cookie)
        assert client.post("/api/system/api-token").status_code == 403


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

    def test_watch_history_is_counted_from_the_watch_mirror_not_a_run_written_pref(self, client: TestClient):
        """`prefs["history_depth"]` is only written once a run PROCESSES someone, so a skipped user —
        or anyone before their first successful run — read "0 titles" forever. A beta user saw 0 for
        all 42 of his accounts while the log showed 170 events synced for one of them."""
        from shortlist.server.db.models import User, WatchEvent

        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(username="sarah").one()
            # Two plays of one show + one film = 2 distinct TITLES, not 3 events. (Distinct
            # timestamps: (user, rating_key, watched_at) is unique, which is how the sync dedups.)
            when = datetime(2026, 7, 21, 2, 0, tzinfo=UTC)
            session.add_all(
                [
                    WatchEvent(user_id=user.id, rating_key=100, watched_at=when, media_type="show"),
                    WatchEvent(
                        user_id=user.id, rating_key=100, watched_at=when + timedelta(hours=1), media_type="show"
                    ),
                    WatchEvent(user_id=user.id, rating_key=200, watched_at=when, media_type="movie"),
                ]
            )
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
        from shortlist.server.settings_store import SettingsStore

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
        from shortlist.server.api import users as users_api

        calls: list = []

        async def spy(state):
            calls.append(state)

        monkeypatch.setattr(users_api, "_rename_after_nickname", spy)
        monkeypatch.setattr(
            users_api.TautulliClient, "friendly_names", lambda self: {555000100: "Sazza"}, raising=False
        )
        from shortlist.server.settings_store import SettingsStore

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("tautulli.url", "http://tautulli:8181")
            session.commit()

        client.post("/api/users/sync")

        assert self._users(client)["sarah"]["display_name"] == "Sazza"
        assert len(calls) == 1, "a changed display name must reconcile the rows already on Plex"

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
        from shortlist.server.api import users as users_api

        calls: list = []

        async def spy(state):
            calls.append(state)

        monkeypatch.setattr(users_api, "_rename_after_nickname", spy)

        client.post("/api/users/sync")  # the fixture renames one account, so this one DOES reconcile
        calls.clear()
        client.post("/api/users/sync")

        assert calls == []

    def test_sync_adds_the_owner_disabled_and_badged(self, client: TestClient, plextv):
        r = client.post("/api/users/sync")
        assert r.status_code == 200
        # The fixture's two accounts are both already in the DB (sarah, and 555000200 whom plex.tv
        # now calls "kid") — so the only thing ADDED is the owner plex.tv never returns.
        assert r.json() == {"added": 1, "updated": 2, "total": 3}

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
        assert r.json()["total"] == 2  # the fixture's shared users only
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
        assert r.json()["total"] == 2, why  # both shared users still landed
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

    def test_override_on_unknown_user_or_row_404(self, client: TestClient):
        assert client.put("/api/users/9999/rows/1", json={"muted": True}).status_code == 404
        uid = self._sarah_id(client)
        assert client.put(f"/api/users/{uid}/rows/9999", json={"muted": True}).status_code == 404

    def test_runs_empty_then_unknown_user_404(self, client: TestClient):
        uid = self._sarah_id(client)
        assert client.get(f"/api/users/{uid}/runs").json() == []
        assert client.get("/api/users/9999/runs").status_code == 404


class TestRunsApi:
    def test_empty_list_then_trigger(self, client: TestClient):
        assert client.get("/api/runs").json() == []
        r = client.post("/api/runs", json={"dry_run": True})
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        # The run fails fast (Plex unconfigured) but must exist with a terminal/queued status.
        for _ in range(50):
            runs = client.get("/api/runs").json()
            if runs and runs[0]["status"] in ("error", "ok"):
                break
            time.sleep(0.05)
        assert runs[0]["id"] == run_id
        assert runs[0]["status"] == "error"  # no plex configured in this app instance
        detail = client.get(f"/api/runs/{run_id}")
        assert detail.status_code == 200

    def test_a_skipped_users_reason_reaches_the_run_detail_without_looking_like_a_failure(self, client: TestClient):
        """The whole point of `reason` (issue #3): "skipped" has to explain itself in the UI. It is
        carried SEPARATELY from `error` because the run page counts every non-null error as a failed
        user — so a skip must arrive with a reason and a null error."""
        from shortlist.server.db.models import Run, RunUser, User

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="skipped",
                    reason="There are no per-person rows to build.",
                )
            )
            session.commit()
            run_id = run.id

        result = client.get(f"/api/runs/{run_id}").json()["users"][0]

        assert result["status"] == "skipped"
        assert result["reason"] == "There are no per-person rows to build."
        assert result["error"] is None

    def test_run_detail_shows_the_display_name_not_the_bare_username(self, client: TestClient):
        """The runs view must read a person the same way the Users page does — nickname → Tautulli
        friendly name → username (User.display_name). The bug: it only emitted `username`, so a
        Tautulli friendly name populated after the run still showed the raw Plex login (SFLIX)."""
        from shortlist.server.db.models import Run, RunUser, User

        with client.app.state.sessions() as session:
            user = session.query(User).first()
            user.nickname = ""  # no owner override — the Tautulli name should win
            user.friendly_name = "Joe - Richard's Mate"
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=user.id, status="ok"))
            session.commit()
            run_id, raw_username = run.id, user.username

        result = client.get(f"/api/runs/{run_id}").json()["users"][0]

        assert result["username"] == raw_username  # still carried, for search + avatar
        assert result["display_name"] == "Joe - Richard's Mate"

    def test_the_run_page_gets_provenance_on_the_breakdown_it_actually_renders(self, client: TestClient):
        """The run page renders the stored `breakdown` blob, NOT the picks list — so provenance
        added to the picks table alone was invisible on the one screen built to answer "why was
        this picked?". Backfilled from the picks rows so existing runs explain themselves too,
        instead of staying blank until they are rebuilt."""
        from shortlist.server.db.models import PickRow, Run, RunUser, User

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="ok",
                    # A blob written before provenance existed: picks without sources/affinity.
                    breakdown=[
                        {
                            "row_slug": "picked",
                            "row_title": "Picked",
                            "library_key": "1",
                            "picks": [{"rank": 1, "title": "Torchwood", "tmdb_id": 55, "media_type": "show"}],
                        }
                    ],
                )
            )
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=user_id,
                    tmdb_id=55,
                    media_type="show",
                    rating_key=1,
                    rank=1,
                    title="Torchwood",
                    sources="tmdb_similar",
                    affinity=0.28,
                )
            )
            session.commit()
            run_id = run.id

        entry = client.get(f"/api/runs/{run_id}").json()["users"][0]["breakdown"][0]["picks"][0]

        assert entry["sources"] == ["tmdb_similar"]
        assert entry["affinity"] == 0.28

    def test_a_breakdown_pick_with_no_matching_row_is_left_alone(self, client: TestClient):
        """Never invent provenance: a pick the picks table doesn't know about stays blank, which the
        UI renders as nothing rather than as a confident claim."""
        from shortlist.server.db.models import Run, RunUser, User

        with client.app.state.sessions() as session:
            user_id = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user_id,
                    status="ok",
                    breakdown=[
                        {
                            "row_slug": "picked",
                            "library_key": "1",
                            "picks": [{"rank": 1, "title": "Unknown", "tmdb_id": 999, "media_type": "movie"}],
                        }
                    ],
                )
            )
            session.commit()
            run_id = run.id

        entry = client.get(f"/api/runs/{run_id}").json()["users"][0]["breakdown"][0]["picks"][0]

        assert "sources" not in entry or entry["sources"] == []

    def test_a_failed_run_exposes_why_not_just_that_it_failed(self, client: TestClient):
        """The reason lived only inside `stats`, which no client read — so a run that failed for a
        run-level reason (a share filter Plex refused) surfaced as a bare "Failed" and the operator
        had to go read container logs (issue #1)."""
        from shortlist.server.db.models import Run

        blocker = "LisaPlex1234 (plex account 12345): plex.tv rejected the share-filter update: HTTP 400"
        with client.app.state.sessions() as session:
            run = Run(
                trigger="manual",
                status="error",
                stats={
                    "users_ok": 1,
                    "users_error": 0,
                    "error": "privacy sync failed",
                    "promotion_blockers": [blocker],
                },
            )
            session.add(run)
            session.commit()
            run_id = run.id

        detail = client.get(f"/api/runs/{run_id}").json()
        listed = next(r for r in client.get("/api/runs").json() if r["id"] == run_id)

        assert detail["error"] == "privacy sync failed"
        assert detail["promotion_blockers"] == [blocker]
        assert listed["error"] == "privacy sync failed"  # the list carries it too

    def test_cancel_a_run_that_isnt_running_returns_409(self, client: TestClient):
        # A run that already finished (or never existed) can't be cancelled — the endpoint says so
        # rather than pretending it stopped something.
        assert client.post("/api/runs/999999/cancel").status_code == 409

    def test_runs_can_be_filtered_to_one_row(self, client: TestClient):
        """?collection=<slug> narrows the list to runs that actually built that row (their picks carry
        its slug), so the Rows page can link a row to its own run history."""
        from shortlist.server.db.models import PickRow, Run, User

        with client.app.state.sessions() as session:
            uid = session.query(User).first().id
            built, other = Run(trigger="manual", status="ok"), Run(trigger="manual", status="ok")
            session.add_all([built, other])
            session.flush()
            # Only `built` produced a pick in the "hidden_gems" row.
            session.add(
                PickRow(
                    run_id=built.id,
                    user_id=uid,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="hidden_gems",
                    title="Dune",
                )
            )
            session.commit()
            built_id, other_id = built.id, other.id

        filtered = client.get("/api/runs?collection=hidden_gems").json()
        assert [r["id"] for r in filtered] == [built_id]  # only the run that built it
        all_ids = {r["id"] for r in client.get("/api/runs").json()}
        assert {built_id, other_id} <= all_ids  # unfiltered still shows both
        assert client.get("/api/runs?collection=nonexistent").json() == []

    def test_runs_summary_reports_counts(self, client: TestClient):
        from shortlist.server.db.models import Run

        with client.app.state.sessions() as session:
            session.add_all(
                [
                    Run(trigger="manual", status="ok"),
                    Run(trigger="scheduled", status="ok"),
                    Run(trigger="manual", status="error"),
                ]
            )
            session.commit()

        summary = client.get("/api/runs/summary").json()
        assert summary["total"] == 3
        assert summary["ok"] == 2
        assert summary["error"] == 1
        assert summary["last_status"] == "error"  # the newest run

    def test_clear_runs_deletes_every_run_its_picks_and_per_user_rows(self, client: TestClient):
        from shortlist.server.db.models import PickRow, Run, RunUser, User

        with client.app.state.sessions() as session:
            uid = session.query(User).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(RunUser(run_id=run.id, user_id=uid, status="ok"))
            session.add(
                PickRow(run_id=run.id, user_id=uid, tmdb_id=1, media_type="movie", rating_key=1, rank=1, title="Dune")
            )
            session.commit()

        assert client.delete("/api/runs").json() == {"deleted": 1}
        assert client.get("/api/runs").json() == []
        with client.app.state.sessions() as session:
            assert session.query(PickRow).count() == 0  # picks went too
            assert session.query(RunUser).count() == 0  # and the per-user rows (no ORM cascade on bulk delete)

    def test_retention_prunes_old_runs_beyond_keep_but_spares_the_hit_window(self, client: TestClient):
        """_prune_runs deletes a run past `keep` AND its picks + per-user rows — but ONLY once it's
        also older than the 30-day hit window (a recent run beyond `keep` is kept so the report keeps
        crediting its picks). Picks/run_users aren't ORM-cascaded off Run, so both go explicitly."""
        from datetime import UTC, datetime, timedelta

        from shortlist.server.db.models import PickRow, Run, RunUser, User
        from shortlist.server.services.run_service import RunService

        with client.app.state.sessions() as session:
            uid = session.query(User).first().id
            now = datetime.now(UTC)
            stale = Run(trigger="manual", status="ok", started_at=now - timedelta(days=40))  # beyond the window
            recent = Run(trigger="manual", status="ok", started_at=now - timedelta(days=2))  # inside the window
            newest = Run(trigger="manual", status="ok", started_at=now)
            session.add_all([stale, recent, newest])
            session.flush()
            stale_id, recent_id = stale.id, recent.id
            for i, run in enumerate((stale, recent, newest)):
                session.add(RunUser(run_id=run.id, user_id=uid, status="ok"))
                session.add(
                    PickRow(run_id=run.id, user_id=uid, tmdb_id=i, media_type="movie", rating_key=i, rank=1, title="X")
                )
            session.commit()

            RunService._prune_runs(session, keep=1)  # keep only the newest by count...
            session.commit()

            kept = {r.id for r in session.query(Run).all()}
            # ...but `recent` survives despite being beyond keep=1, because it's inside the hit window.
            assert stale_id not in kept and recent_id in kept
            assert session.query(PickRow).filter(PickRow.run_id == stale_id).count() == 0  # its pick pruned
            assert session.query(RunUser).filter(RunUser.run_id == stale_id).count() == 0  # its per-user row pruned
            assert session.query(PickRow).count() == 2  # recent + newest picks remain

    def test_trigger_forwards_row_scope_to_the_run(self, client: TestClient, monkeypatch):
        """A manual run can target specific rows — collection_ids must reach start_run (the engine's
        build_only scope), not be silently dropped."""
        captured: dict = {}

        async def fake_start_run(*, trigger, dry_run, user_ids, collection_ids):
            captured.update(trigger=trigger, dry_run=dry_run, user_ids=user_ids, collection_ids=collection_ids)
            return 123

        monkeypatch.setattr(client.app.state.run_service, "start_run", fake_start_run)
        r = client.post("/api/runs", json={"collection_ids": [4, 7], "dry_run": True})
        assert r.status_code == 202 and r.json()["run_id"] == 123
        assert captured["collection_ids"] == [4, 7]
        assert captured["dry_run"] is True and captured["trigger"] == "manual"

    def test_effectiveness_report_counts_hits(self, client: TestClient):
        from datetime import UTC, datetime

        from shortlist.server.db.models import PickRow, Run, User

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="picked",
                        title="A",
                        watched_at=now,
                    ),
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=2,
                        media_type="movie",
                        rating_key=2,
                        rank=2,
                        collection_slug="picked",
                        title="B",
                        watched_at=now,
                    ),
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=3,
                        media_type="movie",
                        rating_key=3,
                        rank=3,
                        collection_slug="picked",
                        title="C",
                    ),
                ]
            )
            session.commit()

        body = client.get("/api/report").json()
        assert body["overall"]["delivered"] == 3
        assert body["overall"]["watched"] == 2
        assert body["overall"]["hit_rate"] == round(2 / 3, 3)
        assert body["overall"]["watched_last_7d"] == 2  # both watched just now
        assert body["per_row"][0]["slug"] == "picked" and body["per_row"][0]["watched"] == 2
        assert any(u["watched"] == 2 for u in body["per_user"])
        assert len(body["recent"]) == 2 and body["recent"][0]["row"]
        assert body["coverage"]["users_with_picks"] == 1
        assert {t["title"] for t in body["top_titles"]} == {"A", "B"}  # the two watched titles
        assert body["runs"]["total"] >= 1

    def test_report_splits_a_multi_library_row_per_library(self, client: TestClient):
        """A row targeting >1 library is one Plex collection PER library, so the report tracks each
        library as its own line — with its own hit rate and the library's own {library_name} name."""
        from datetime import UTC, datetime

        from shortlist.server.db.models import PickRow, Run, User

        with client.app.state.sessions() as session:
            uid = session.query(User).filter_by(slug="sarah").first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    # The "picked" row delivered a movie into Movies (watched) and a show into TV (not).
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="picked",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                        watched_at=now,
                    ),
                    PickRow(
                        run_id=run.id,
                        user_id=uid,
                        tmdb_id=2,
                        media_type="show",
                        rating_key=2,
                        rank=1,
                        collection_slug="picked",
                        section_key="20",
                        library="TV",
                        title="Shogun",
                    ),
                ]
            )
            session.commit()

        body = client.get("/api/report").json()
        by_library = {r["library"]: r for r in body["per_row"]}
        assert by_library["Movies"]["delivered"] == 1 and by_library["Movies"]["watched"] == 1
        assert by_library["TV"]["delivered"] == 1 and by_library["TV"]["watched"] == 0
        # The {library_name} template renders each library's own name.
        assert by_library["Movies"]["name"] == "✨ Movies Picked for You"
        assert by_library["TV"]["name"] == "✨ TV Picked for You"
        # Overall still dedupes to distinct (user, title): two titles, one watched — not skewed by the split.
        assert body["overall"]["delivered"] == 2 and body["overall"]["watched"] == 1

    def test_report_tracks_a_title_moving_rows_and_a_second_watcher(self, client: TestClient):
        """The full lifecycle: a title watched in one row, later moved to another row (no re-credit,
        the watch predates it), then watched by a DIFFERENT person in that other row. Overall must not
        double-count; each row is credited only for the watches that happened while the title was in it."""
        from datetime import UTC, datetime

        from shortlist.server.db.models import PickRow, Run, User

        with client.app.state.sessions() as session:
            x = session.query(User).filter_by(slug="sarah").first().id
            y = session.query(User).filter_by(slug="mike").first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            now = datetime.now(UTC)
            session.add_all(
                [
                    # X got Dune in row A and watched it there.
                    PickRow(
                        run_id=run.id,
                        user_id=x,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="rowA",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                        watched_at=now,
                    ),
                    # Dune later moved to row B for X — but X already watched it, so row B gets no credit.
                    PickRow(
                        run_id=run.id,
                        user_id=x,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="rowB",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                    ),
                    # Y got Dune in row B and watched it there.
                    PickRow(
                        run_id=run.id,
                        user_id=y,
                        tmdb_id=1,
                        media_type="movie",
                        rating_key=1,
                        rank=1,
                        collection_slug="rowB",
                        section_key="10",
                        library="Movies",
                        title="Dune",
                        watched_at=now,
                    ),
                ]
            )
            session.commit()

        body = client.get("/api/report").json()
        # Distinct (user, title): (X, Dune) + (Y, Dune) = 2 delivered; both watched = 2. The move is not
        # a third recommendation.
        assert body["overall"]["delivered"] == 2 and body["overall"]["watched"] == 2
        by_slug = {r["slug"]: r for r in body["per_row"]}
        assert by_slug["rowA"]["delivered"] == 1 and by_slug["rowA"]["watched"] == 1  # X only
        # Row B holds both people's copies; only Y's was watched while the title lived there.
        assert by_slug["rowB"]["delivered"] == 2 and by_slug["rowB"]["watched"] == 1

    def test_unknown_run_404(self, client: TestClient):
        assert client.get("/api/runs/424242").status_code == 404

    def test_run_log_endpoint_returns_a_list(self, client: TestClient):
        # A run whose process never buffered a log returns an empty list, not a 404 — the page seeds
        # its activity feed from this and tops it up over SSE.
        r = client.get("/api/runs/424242/log")
        assert r.status_code == 200 and r.json() == []


class TestSettingsValidation:
    """PUT /api/settings validated the KEY but never the VALUE, so any non-UI client could push a
    value the engine then choked on — or, worse, one that quietly disabled a safety rule."""

    def test_the_plextv_throttle_floor_accepts_zero_and_rejects_out_of_range(self, client: TestClient):
        # `plextv.throttle_s` is now the FLOOR (min seconds) between plex.tv writes: 0 = as fast as
        # plex.tv accepts, safe because the client backs off adaptively on a 429 (rule 6). So 0 is
        # valid now — it's no longer an "off switch". Out-of-range values are still refused.
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": 0}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": 2.5}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": -1}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"plextv.throttle_s": 61}}).status_code == 422

    def test_a_bad_plex_timeout_is_refused(self, client: TestClient):
        # It's read unguarded as int(...) in build_context, so a bad stored value would crash every run.
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": 45}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": "abc"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": 0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"plex.timeout_s": 301}}).status_code == 422

    def test_a_non_numeric_row_size_is_refused(self, client: TestClient):
        # "abc" was stored happily, then raised ValueError inside every run and 500'd two endpoints.
        assert client.put("/api/settings", json={"values": {"row.size": "abc"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"row.size": 99}}).status_code == 422
        # The free number picker allows anything 5..40 (widened from 30; ceiling = pre-rank pool cap).
        assert client.put("/api/settings", json={"values": {"row.size": 40}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"row.size": 41}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"row.size": 4}}).status_code == 422

    def test_hub_anchor_shape_is_validated(self, client: TestClient):
        # Bad shapes used to reach the engine and skip ordering silently.
        bad = {"2": {"before": True}}  # missing 'anchor'
        assert client.put("/api/settings", json={"values": {"rows.hub_anchor": bad}}).status_code == 422
        assert (
            client.put("/api/settings", json={"values": {"rows.hub_anchor": {"2": {"anchor": ""}}}}).status_code == 422
        )
        good = {"2": {"anchor": "New Series (Unwatched)", "before": False}}
        assert client.put("/api/settings", json={"values": {"rows.hub_anchor": good}}).status_code == 200
        # A 'top' entry is valid without an anchor.
        assert (
            client.put("/api/settings", json={"values": {"rows.hub_anchor": {"2": {"top": True}}}}).status_code == 200
        )
        assert client.put("/api/settings", json={"values": {"rows.hub_anchor": {}}}).status_code == 200  # clears it

    def test_request_year_bounds_are_validated(self, client: TestClient):
        # Both ends of the request year window share the 0..2100 bound (0 = that end disabled).
        assert client.put("/api/settings", json={"values": {"requests.max_year": 3000}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"requests.max_year": 1990}}).status_code == 200
        assert client.put("/api/settings", json={"values": {"requests.min_year": 2000}}).status_code == 200

    def test_paused_all_must_be_a_real_boolean(self, client: TestClient):
        # A non-empty string is truthy in Python, so "false" PAUSED every run while the UI read "off".
        assert client.put("/api/settings", json={"values": {"paused_all": "false"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"paused_all": False}}).status_code == 200

    def test_an_unknown_candidate_source_is_refused(self, client: TestClient):
        # POST /collections already rejected this; the global key accepted it.
        r = client.put("/api/settings", json={"values": {"candidates.sources": ["totally_bogus"]}})
        assert r.status_code == 422
        ok = client.put("/api/settings", json={"values": {"candidates.sources": ["trakt", "tmdb_similar"]}})
        assert ok.status_code == 200

    def test_watched_cap_is_validated(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"recommendations.watched_pct": 1.5}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.watched_pct": 0.25}}).status_code == 200
        assert client.get("/api/settings").json()["recommendations.watched_pct"] == 0.25

    def test_an_unknown_curator_provider_is_refused(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"curator.provider": "bogus"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"curator.provider": "none"}}).status_code == 200

    def test_curator_models_is_empty_for_the_built_in_picker(self, client: TestClient):
        client.put("/api/settings", json={"values": {"curator.provider": "none"}})
        assert client.post("/api/settings/curator/models").json() == {"provider": "none", "models": []}

    def test_curator_models_lists_the_saved_providers_models(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace

        import shortlist.engine.curator as curator_mod

        client.put("/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-secret"}})
        captured: dict = {}

        def fake_make(provider, **kw):
            captured["provider"] = provider
            captured.update(kw)
            return SimpleNamespace(list_models=lambda: ["claude-a", "claude-b"])

        monkeypatch.setattr(curator_mod, "make_curator", fake_make)
        # No body → fall back to the SAVED provider + key server-side.
        body = client.post("/api/settings/curator/models").json()
        assert body == {"provider": "anthropic", "models": ["claude-a", "claude-b"]}
        assert captured["provider"] == "anthropic"
        assert captured["api_key"] == "sk-secret"

    def test_curator_models_lists_the_provider_being_edited_from_the_request(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace

        import shortlist.engine.curator as curator_mod

        # A DIFFERENT provider is saved; the form is editing OpenAI with a not-yet-saved key. The picker
        # must list what's in the request, so the dropdown updates before Save — the bug Steve hit.
        client.put("/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-saved"}})
        captured: dict = {}

        def fake_make(provider, **kw):
            captured["provider"] = provider
            captured.update(kw)
            return SimpleNamespace(list_models=lambda: ["gpt-x", "gpt-y"])

        monkeypatch.setattr(curator_mod, "make_curator", fake_make)
        body = client.post(
            "/api/settings/curator/models",
            json={"provider": "openai", "api_key": "sk-new-openai"},
        ).json()
        assert body == {"provider": "openai", "models": ["gpt-x", "gpt-y"]}
        # The edited provider + its typed key reached make_curator — not the saved anthropic ones.
        assert captured["provider"] == "openai"
        assert captured["api_key"] == "sk-new-openai"

    def test_curator_models_redacted_key_falls_back_to_saved(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace

        import shortlist.engine.curator as curator_mod

        client.put("/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-saved"}})
        captured: dict = {}

        def fake_make(provider, **kw):
            captured.update(kw)
            return SimpleNamespace(list_models=lambda: ["claude-a"])

        monkeypatch.setattr(curator_mod, "make_curator", fake_make)
        # The UI sends the redacted placeholder for an unchanged key → use the real saved key.
        client.post("/api/settings/curator/models", json={"provider": "anthropic", "api_key": "•••••"})
        assert captured["api_key"] == "sk-saved"

    def test_curator_models_degrades_to_empty_when_listing_fails(self, client: TestClient, monkeypatch):
        import shortlist.engine.curator as curator_mod

        client.put("/api/settings", json={"values": {"curator.provider": "anthropic"}})

        def boom(provider, **kw):  # a bad/absent key or an offline provider must never 500 the picker
            raise RuntimeError("unauthorized at http://api?X-Plex-Token=SEKRET")

        monkeypatch.setattr(curator_mod, "make_curator", boom)
        body = client.post("/api/settings/curator/models").json()
        assert body == {"provider": "anthropic", "models": []}

    def test_web_search_provider_is_validated(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"llm_web.search_provider": "bogus"}}).status_code == 422
        for mode in ("auto", "native", "exa"):
            assert client.put("/api/settings", json={"values": {"llm_web.search_provider": mode}}).status_code == 200

    def test_log_level_is_validated_and_applied(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"log.level": "LOUD"}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"log.level": "DEBUG"}}).status_code == 200
        assert client.get("/api/settings").json()["log.level"] == "DEBUG"

    def test_run_concurrency_is_bounded(self, client: TestClient):
        assert client.put("/api/settings", json={"values": {"run.concurrency": 0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"run.concurrency": 99}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"run.concurrency": 4}}).status_code == 200

    def test_a_valid_settings_payload_still_saves(self, client: TestClient):
        r = client.put(
            "/api/settings",
            json={"values": {"row.size": 15, "requests.min_rating": 7.5, "requests.max_per_run": 5}},
        )
        assert r.status_code == 200


class TestLogsApi:
    """The in-app Logs view. Owner-only, and redacted — it exists to be copied into bug reports."""

    def _write_log(self, client: TestClient, *lines: str) -> None:
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    LINE = "2026-07-21 07:27:18.100 | {level:<8} | shortlist.server.main:lifespan:168 - {message}"

    def test_returns_parsed_lines_filtered_by_level(self, client: TestClient):
        self._write_log(
            client,
            self.LINE.format(level="DEBUG", message="quiet"),
            self.LINE.format(level="ERROR", message="loud"),
        )

        body = client.get("/api/system/logs?level=ERROR").json()

        assert [x["message"] for x in body["lines"]] == ["loud"]
        assert body["file"] == "shortlist.log"

    def test_never_serves_a_credential(self, client: TestClient):
        """The whole point of the view is that it gets shared, so this is the load-bearing test."""
        self._write_log(client, self.LINE.format(level="INFO", message="GET /x?X-Plex-Token=LEAKME -> 200"))

        assert "LEAKME" not in client.get("/api/system/logs").text

    def test_the_zip_download_is_attached_and_redacted(self, client: TestClient):
        import io
        import zipfile

        self._write_log(client, self.LINE.format(level="INFO", message="token: X-Plex-Token: LEAKME"))

        r = client.get("/api/system/logs/download")

        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment; filename=" in r.headers["content-disposition"]
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        assert "LEAKME" not in archive.read("logs/shortlist.log").decode()

    def test_logs_are_owner_only(self, client: TestClient):
        """Logs describe the whole server and name every user on it — they are not public."""
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/system/logs").status_code == 401
        assert client.get("/api/system/logs/download").status_code == 401


class TestSettingsApi:
    def test_get_put_round_trip_and_unknown_key_rejected(self, client: TestClient):
        settings = client.get("/api/settings").json()
        assert settings["row.size"] == 15
        r = client.put("/api/settings", json={"values": {"row.size": 20}})
        assert r.status_code == 200
        assert r.json()["row.size"] == 20
        assert client.put("/api/settings", json={"values": {"evil.key": 1}}).status_code == 422

    def test_secret_set_then_redacted_and_placeholder_roundtrip_keeps_value(self, client: TestClient):
        client.put("/api/settings", json={"values": {"tmdb.apikey": "abc123"}})
        # tmdb.apikey isn't a SECRET_KEY; use the plex token which is.
        client.put("/api/settings", json={"values": {"plex.token": "real-token"}})
        settings = client.get("/api/settings").json()
        assert settings["plex.token"] == "•••••"
        client.put("/api/settings", json={"values": {"plex.token": "•••••"}})  # UI round-trip
        with client.app.state.sessions() as session:
            from shortlist.server.settings_store import SettingsStore

            assert SettingsStore(session, client.app.state.secrets).get("plex.token") == "real-token"

    def test_exa_key_is_encrypted_at_rest_and_redacted_in_the_api(self, client: TestClient):
        # The Exa web-search key is a secret: stored encrypted, shown as the redacted sentinel, and
        # a round-tripped sentinel must not overwrite the real value (rule 9).
        client.put("/api/settings", json={"values": {"exa.apikey": "exa-secret-123"}})
        assert client.get("/api/settings").json()["exa.apikey"] == "•••••"
        client.put("/api/settings", json={"values": {"exa.apikey": "•••••"}})  # UI round-trip
        with client.app.state.sessions() as session:
            from shortlist.server.settings_store import SettingsStore

            assert SettingsStore(session, client.app.state.secrets).get("exa.apikey") == "exa-secret-123"

    def test_exa_test_connection_probes_the_key(self, client: TestClient, monkeypatch):
        # No key → a plain-English error, not a crash.
        no_key = client.post("/api/settings/test/exa").json()
        assert no_key["ok"] is False and "Exa" in no_key["message"]
        # With a key, it pings Exa (mocked — no test may touch the network).
        client.put("/api/settings", json={"values": {"exa.apikey": "exa-secret-123"}})
        monkeypatch.setattr("shortlist.engine.clients.search.ExaClient.ping", lambda self: "ok — 1 result")
        ok = client.post("/api/settings/test/exa").json()
        assert ok["ok"] is True and "ok" in ok["message"]

    def test_connection_error_redacts_a_plex_token(self, client: TestClient, monkeypatch):
        # plexapi errors can embed the tokened request URL; the connection-test response must never
        # echo it back (plex-safety rule 9). Simulate a probe that raises with a token in the message.
        client.put("/api/settings", json={"values": {"plex.url": "http://pms:32400", "plex.token": "tok"}})

        def boom(self, *a, **k):
            raise RuntimeError("BadRequest: http://pms:32400/library?X-Plex-Token=super-secret-abc failed")

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient.__init__", boom)
        body = client.post("/api/settings/test/plex").json()
        assert body["ok"] is False
        assert "super-secret-abc" not in body["message"]
        assert "X-Plex-Token=REDACTED" in body["message"]


class _FakeStore:
    """Minimal SettingsStore stand-in: .get(key) returns the value or None."""

    def __init__(self, values: dict):
        self._values = values

    def get(self, key: str):
        return self._values.get(key)


class TestCollectionsSeed:
    def test_migration_seeds_the_default_picked_row(self, client: TestClient):
        """Upgrade must be behaviour-neutral: exactly one per-person 'picked' row for everyone."""
        from shortlist.server.db.models import Collection

        with client.app.state.sessions() as session:
            rows = session.query(Collection).all()
            assert len(rows) == 1
            row = rows[0]
            assert (row.slug, row.build, row.audience, row.enabled) == (
                "picked",
                "per_person",
                "everyone",
                True,
            )

    def test_collection_reports_its_last_run(self, client: TestClient):
        """The Rows UI links a row to its last run — last_run_id is the newest run that delivered it."""
        from shortlist.server.db.models import PickRow, Run, User

        with client.app.state.sessions() as session:
            uid = session.query(User).order_by(User.id).first().id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            run_id = run.id
            session.add(
                PickRow(
                    run_id=run_id,
                    user_id=uid,
                    tmdb_id=9,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="picked",
                    title="X",
                )
            )
            session.commit()

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        assert picked["last_run_id"] == run_id

    def test_default_rows_serialized_name_is_the_global_template_not_the_stale_column(self, client: TestClient):
        """The Rows UI must show the ACTUAL default title (the global template it delivers), not the
        seeded 'name' column — so the {library_name} default is visible in the editor and list."""
        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        assert picked["name"] == "✨ {library_name} Picked for You"

    def test_saving_the_default_row_never_overwrites_its_name_column(self, client: TestClient):
        """The editor sends the serialized name (now the template) back on save; the default row's name
        column must NOT be clobbered by it — it follows Settings, not this PATCH."""
        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        r = client.patch(f"/api/collections/{picked['id']}", json={"name": "✨ {library_name} Picked for You"})
        assert r.status_code == 200
        with client.app.state.sessions() as session:
            from shortlist.server.db.models import Collection

            assert session.query(Collection).filter_by(slug="picked").one().name == "✨ Picked for You"

    def test_editing_the_default_rows_name_writes_the_global_template_and_reconciles(
        self, client: TestClient, monkeypatch
    ):
        """The default row's editable name IS the global `row.name_template` (a per-collection value
        would beat each user's own `row_name_tpl` override). Editing it writes that setting and renames
        the collections already on Plex in place — the same reconcile a nickname change fires."""
        from shortlist.server.services import collection_reconcile as rec

        calls: list[tuple[str, str, str]] = []

        async def fake_rename(state, *, slug, new_template, scope):
            calls.append((slug, new_template, scope))
            return [], None

        monkeypatch.setattr(rec, "run_row_rename", fake_rename)

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        r = client.patch(f"/api/collections/{picked['id']}", json={"name": "✨ {library_name} Handpicked"})
        assert r.status_code == 200
        # The edit is surfaced as the row's name (read back from the global template) …
        assert r.json()["name"] == "✨ {library_name} Handpicked"
        # … persisted to the shared setting …
        assert client.get("/api/settings").json()["row.name_template"] == "✨ {library_name} Handpicked"
        # … and reconciled onto Plex for the default slug.
        assert calls == [("picked", "✨ {library_name} Handpicked", "collection.rename")]

    def test_saving_the_default_row_with_an_unchanged_name_does_no_plex_work(self, client: TestClient, monkeypatch):
        """A save that doesn't move the name (e.g. an enable toggle carrying the current name) must not
        touch Plex — the rename reconcile does real I/O and only fires on a real change."""
        from shortlist.server.services import collection_reconcile as rec

        calls: list = []

        async def fake_rename(state, **kwargs):
            calls.append(kwargs)
            return [], None

        monkeypatch.setattr(rec, "run_row_rename", fake_rename)

        client.put("/api/settings", json={"values": {"row.name_template": "✨ {library_name} Picked for You"}})
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        # The editor round-trips the current template as the name — an unchanged value, so no reconcile.
        r = client.patch(
            f"/api/collections/{picked['id']}",
            json={"name": "✨ {library_name} Picked for You", "enabled": True},
        )
        assert r.status_code == 200
        assert calls == [], "an unchanged default name must not reconcile onto Plex"

    def test_default_row_size_and_name_follow_the_global_setting(self, client: TestClient, tmp_path):
        """The wizard/Settings set row.size and row.name_template; the default 'picked' row must
        deliver at those values, not a size frozen into the collection at migration time."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        client.put("/api/settings", json={"values": {"row.size": 10}})
        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        picked = next(spec for spec in specs if spec.slug == "picked")
        assert picked.size == 10  # follows the setting, not the collection's seeded 15
        assert picked.name_template == ""  # falls through to the global row name

    def test_per_row_watched_pct_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Rewatch Row", "watched_pct": 0.5})
        assert created.status_code == 201 and created.json()["watched_pct"] == 0.5
        # Out of the 0..1 range is rejected.
        assert client.post("/api/collections", json={"name": "X", "watched_pct": 2.0}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "rewatch_row").watched_pct == 0.5

    def test_per_row_freshness_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Fresh Row", "freshness": 0.75})
        assert created.status_code == 201 and created.json()["freshness"] == 0.75
        # Out of the 0..1 range is rejected.
        assert client.post("/api/collections", json={"name": "X", "freshness": 1.5}).status_code == 422
        # And the global freshness setting is range-checked too.
        assert client.put("/api/settings", json={"values": {"recommendations.freshness": 2.0}}).status_code == 422
        assert client.put("/api/settings", json={"values": {"recommendations.freshness": 0.3}}).status_code == 200

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "fresh_row").freshness == 0.75

    def test_per_row_placement_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Top Row", "placement": "library", "pin_top": True})
        assert created.status_code == 201
        assert created.json()["placement"] == "library" and created.json()["pin_top"] is True
        # An unknown placement is rejected.
        assert client.post("/api/collections", json={"name": "X", "placement": "bogus"}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == "top_row")
        assert spec.placement == "library" and spec.pin_top is True
        assert spec.show_library and not spec.show_home  # library-only

    def test_per_row_hub_anchor_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.engine.models import HubAnchor
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        body = {"name": "Gems Row", "hub_anchor": {"2": {"anchor": "New Series", "before": True}}}
        created = client.post("/api/collections", json=body)
        assert created.status_code == 201
        assert created.json()["hub_anchor"] == {"2": {"anchor": "New Series", "before": True, "top": False}}
        # A blank anchor with no top is rejected by the shape.
        blank = client.post("/api/collections", json={"name": "X", "hub_anchor": {"2": {"anchor": ""}}})
        assert blank.status_code == 422
        # A 'top' entry needs no anchor.
        top = client.post("/api/collections", json={"name": "Top Gems", "hub_anchor": {"2": {"top": True}}})
        assert top.status_code == 201 and top.json()["hub_anchor"]["2"]["top"] is True

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        assert next(s for s in specs if s.slug == "gems_row").hub_anchors == {
            "2": HubAnchor(anchor_title="New Series", before=True)
        }
        assert next(s for s in specs if s.slug == "top_gems").hub_anchors == {"2": HubAnchor(to_top=True)}

    def test_a_disabled_row_becomes_a_retired_row_for_cleanup(self, client: TestClient):
        """A row switched off is not delivered (dropped from _build_rows) AND handed to the engine as
        a retired row, so its lingering collection is removed from its owner's Home on the next run."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus

        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid = created.json()["id"]
        client.patch(f"/api/collections/{cid}", json={"name": "Hidden Gems", "enabled": False})

        from shortlist.server.settings_store import SettingsStore

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            retired = builder._retired_rows(session, store)
            built = builder._build_rows(session, store)

        assert "hidden_gems" not in {s.slug for s in built}  # not delivered
        assert "hidden_gems" in {s.slug for s in retired}  # but queued for removal
        assert next(s for s in retired if s.slug == "hidden_gems").name_template == "Hidden Gems"

    def test_a_disabled_dynamic_title_row_is_not_retired(self, client: TestClient):
        """A {top_seed} title renders to the DEFAULT row's title when there are no picks, and all of a
        user's per-person rows share one label (told apart by title only). Retiring such a row would
        match and DELETE the user's live default row — so it must be skipped, not queued for removal."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Because You Watched"})
        cid = created.json()["id"]
        # Give it a dynamic title, then disable it.
        client.patch(
            f"/api/collections/{cid}",
            json={"name": "Because You Watched", "name_template": "Because you watched {top_seed}", "enabled": False},
        )

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        assert "because_you_watched" not in {s.slug for s in retired}, "a dynamic-title row must not be auto-removed"

    def test_a_disabled_whitespace_title_row_is_not_retired(self, client: TestClient):
        """A whitespace-only template also renders to the DEFAULT title (strip -> empty), so it would
        collide with the live default row just like {top_seed}. The guard tests the RENDERED title,
        not a substring, so this must be skipped too."""
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        created = client.post("/api/collections", json={"name": "Blankish"})
        cid = created.json()["id"]
        client.patch(f"/api/collections/{cid}", json={"name": "Blankish", "name_template": "   ", "enabled": False})

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            retired = builder._retired_rows(session, SettingsStore(session, client.app.state.secrets))

        assert "blankish" not in {s.slug for s in retired}, "a whitespace-title row must not be auto-removed"

    def test_poster_config_round_trips_and_reaches_the_spec(self, client: TestClient):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.sse import EventBus
        from shortlist.server.settings_store import SettingsStore

        body = {"name": "Poster Row", "poster": {"mode": "generate", "title": "{user}'s Picks", "style": "neon"}}
        created = client.post("/api/collections", json=body)
        assert created.status_code == 201
        poster = created.json()["poster"]
        assert poster["mode"] == "generate" and poster["title"] == "{user}'s Picks" and poster["has_image"] is False
        # An unknown mode is rejected.
        assert client.post("/api/collections", json={"name": "X", "poster": {"mode": "bogus"}}).status_code == 422

        builder = ContextBuilder(client.app.state.sessions, client.app.state.secrets, EventBus())
        with client.app.state.sessions() as session:
            specs = builder._build_rows(session, SettingsStore(session, client.app.state.secrets))
        spec = next(s for s in specs if s.slug == "poster_row")
        assert spec.poster is not None and spec.poster.mode == "generate" and spec.poster.style == "neon"

    def test_poster_upload_stores_switches_mode_and_serves_the_image(self, client: TestClient):
        import base64

        # A genuine 1x1 PNG — normalize_upload (when Pillow is present) rejects non-images.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
        )
        created = client.post("/api/collections", json={"name": "Uploaded Poster"})
        cid = created.json()["id"]
        # No image yet.
        assert client.get(f"/api/collections/{cid}/poster/image").status_code == 404

        upload = client.post(
            f"/api/collections/{cid}/poster/upload",
            files={"file": ("poster.png", png, "image/png")},
        )
        assert upload.status_code == 200 and upload.json()["mode"] == "upload"

        # The row is now in upload mode and reports an image; the image endpoint serves it.
        got = next(c for c in client.get("/api/collections").json() if c["id"] == cid)
        assert got["poster"]["mode"] == "upload" and got["poster"]["has_image"] is True
        image = client.get(f"/api/collections/{cid}/poster/image")
        assert image.status_code == 200 and image.headers["content-type"].startswith("image/") and image.content

        # A non-image upload is rejected (only when Pillow can tell).
        bad = client.post(
            f"/api/collections/{cid}/poster/upload", files={"file": ("x.png", b"not an image", "image/png")}
        )
        assert bad.status_code in (200, 422)  # 422 with Pillow, 200 (stored as-is) without

        # Deleting the image removes it (mode stays "upload", so nothing is served afterwards).
        assert client.delete(f"/api/collections/{cid}/poster/image").status_code == 204
        assert client.get(f"/api/collections/{cid}/poster/image").status_code == 404

    def test_dropping_a_custom_poster_triggers_a_reset(self, client: TestClient, monkeypatch):
        from shortlist.server.services import collection_reconcile as rec

        calls: list[tuple[str, str, str]] = []

        async def fake_reset(state, *, slug, build, scope):
            calls.append((slug, build, scope))
            return [], None

        monkeypatch.setattr(rec, "run_poster_reset", fake_reset)
        created = client.post("/api/collections", json={"name": "Art Row", "poster": {"mode": "text", "title": "Hi"}})
        cid = created.json()["id"]
        # Switching back to Plex default must reconcile a revert onto Plex.
        client.patch(
            f"/api/collections/{cid}",
            json={"name": "Art Row", "poster": {"mode": "", "title": "", "subtitle": "", "style": ""}},
        )
        assert calls and calls[0][2] == "collection.poster"
        # A no-op poster save (still default) does NOT trigger a reset.
        calls.clear()
        client.patch(
            f"/api/collections/{cid}",
            json={"name": "Art Row", "poster": {"mode": "", "title": "", "subtitle": "", "style": ""}},
        )
        assert calls == []

    def test_image_provider_status_reports_incapable_without_an_image_provider(self, client: TestClient):
        status = client.get("/api/system/image-provider")
        assert status.status_code == 200
        # The test config has no OpenAI/Google curator, so generation is not available.
        assert status.json()["capable"] is False


class TestCollectionsApi:
    def test_list_starts_with_the_seeded_default(self, client: TestClient):
        cols = client.get("/api/collections").json()
        assert [c["slug"] for c in cols] == ["picked"]

    def test_create_update_delete_per_person(self, client: TestClient):
        created = client.post(
            "/api/collections",
            json={"name": "Hidden Gems", "size": 10},
        )
        assert created.status_code == 201
        cid = created.json()["id"]
        assert created.json()["slug"] == "hidden_gems"
        assert created.json()["build"] == "per_person"

        updated = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Hidden Gems", "size": 20, "enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["size"] == 20 and updated.json()["enabled"] is False

        assert client.delete(f"/api/collections/{cid}").status_code == 204
        assert [c["slug"] for c in client.get("/api/collections").json()] == ["picked"]

    def _fake_plex_ctx(self, monkeypatch, client, *, collections):
        """Point run_service.build_context at a fake Plex that records deletions."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        deleted: list[str] = []
        section = SimpleNamespace(title="Movies")
        plex = MagicMock()
        plex.sections.return_value = [section]
        # Return objects with a .title for each (title, label) pair whose label matches.
        plex.find_owned_collections.side_effect = lambda s, label: [
            SimpleNamespace(title=title) for (title, lbl) in collections if lbl == label
        ]
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)
        return deleted

    def test_cleanup_removes_a_shared_rows_collection_by_its_label(self, client: TestClient, monkeypatch):
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")],
        )

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})
        assert r.status_code == 200
        assert r.json()["removed"] == ["🔥 Popular"]  # marker stripped for the audit
        assert len(deleted) == 1

    def test_cleanup_dry_run_reports_without_deleting(self, client: TestClient, monkeypatch):
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")]
        )

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": True})
        assert r.status_code == 200
        assert r.json()["removed"] == ["🔥 Popular"] and r.json()["dry_run"] is True
        assert deleted == []  # nothing actually removed

    def test_cleanup_removes_a_per_person_row_for_each_user_in_the_breakdown(self, client: TestClient, monkeypatch):
        """The complex branch: pin each user's collection by the exact title the last run delivered,
        under that user's own label — and skip a user whose breakdown has no entry for this row."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        created = client.post("/api/collections", json={"name": "Hidden Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]

        with client.app.state.sessions() as session:
            users = session.query(User).order_by(User.id).all()
            assert len(users) >= 2, "fixture must seed at least two users"
            u1, u2 = users[0], users[1]
            u1_slug, u1_acct = u1.slug, u1.plex_account_id
            u2_slug, u2_acct = u2.slug, u2.plex_account_id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            # Both users got this row last run (only u2's breakdown lacks it below stays skipped);
            # here BOTH have it, and any third user has none.
            for uid in (u1.id, u2.id):
                session.add(
                    RunUser(
                        run_id=run.id,
                        user_id=uid,
                        status="ok",
                        breakdown=[{"row_slug": slug, "row_title": "Gems", "library_key": "1"}],
                    )
                )
            session.commit()

        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[
                ("Gems" + row_marker(u1_acct), f"shortlist_{u1_slug}"),
                ("Gems" + row_marker(u2_acct), f"shortlist_{u2_slug}"),
            ],
        )

        r = client.post(f"/api/collections/{cid}/cleanup", json={"dry_run": False})
        assert r.status_code == 200
        assert set(r.json()["removed"]) == {"Gems"}  # marker stripped; both users' collections
        assert len(deleted) == 2  # one per user WITH a breakdown entry for this row

    def test_deleting_a_row_also_removes_its_plex_collection(self, client: TestClient, monkeypatch):
        """Delete now cleans Plex first (while the slug still exists), THEN drops the DB row.

        Shared build only: delete adds a build-agnostic reconcile STEP; the per-person branch itself
        is covered by test_cleanup_removes_a_per_person_row_for_each_user_in_the_breakdown.
        """
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")]
        )

        assert client.delete(f"/api/collections/{cid}").status_code == 204
        assert len(deleted) == 1  # its Plex collection was removed
        assert slug not in {c["slug"] for c in client.get("/api/collections").json()}  # and the DB row is gone

    def test_shrinking_a_rows_audience_removes_only_the_dropped_users_collection(self, client: TestClient, monkeypatch):
        """Dropping a user from a subset audience removes THAT user's collection; the kept user's is
        left untouched (only_user_ids scopes the sweep). Adding a user is a create → left for a run."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        u_ids = [u["id"] for u in client.get("/api/users").json()]
        created = client.post(
            "/api/collections", json={"name": "Gems", "audience": "subset", "audience_user_ids": u_ids}
        )
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            by_id = {u.id: u for u in session.query(User).all()}
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for uid in u_ids:
                session.add(
                    RunUser(
                        run_id=run.id, user_id=uid, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}]
                    )
                )
            session.commit()
            slugs = {uid: by_id[uid].slug for uid in u_ids}
            accts = {uid: by_id[uid].plex_account_id for uid in u_ids}

        keep, drop = u_ids[0], u_ids[1]
        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[
                ("Gems" + row_marker(accts[keep]), f"shortlist_{slugs[keep]}"),
                ("Gems" + row_marker(accts[drop]), f"shortlist_{slugs[drop]}"),
            ],
        )

        r = client.patch(
            f"/api/collections/{cid}", json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep]}
        )
        assert r.status_code == 200
        # Exactly the DROPPED user's collection (its account marker), never the kept user's.
        assert deleted == ["Gems" + row_marker(accts[drop])]

    def test_widening_from_everyone_to_a_subset_removes_the_complement(self, client: TestClient, monkeypatch):
        """everyone → subset: the audience state flips from the 'everyone' branch (old = all ids) to a
        subset, so every user NOT in the new subset is dropped and their row removed — the largest
        removal in the matrix, and the one where old_users resolves via 'everyone', not CollectionAudience."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        u_ids = [u["id"] for u in client.get("/api/users").json()]
        assert len(u_ids) >= 2, "fixture must seed at least two users"
        created = client.post("/api/collections", json={"name": "Gems", "audience": "everyone"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            by_id = {u.id: u for u in session.query(User).all()}
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for uid in u_ids:
                session.add(
                    RunUser(
                        run_id=run.id, user_id=uid, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}]
                    )
                )
            session.commit()
            slugs = {uid: by_id[uid].slug for uid in u_ids}
            accts = {uid: by_id[uid].plex_account_id for uid in u_ids}

        keep, dropped = u_ids[0], u_ids[1:]
        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[("Gems" + row_marker(accts[uid]), f"shortlist_{slugs[uid]}") for uid in u_ids],
        )

        r = client.patch(
            f"/api/collections/{cid}", json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep]}
        )
        assert r.status_code == 200
        assert set(deleted) == {"Gems" + row_marker(accts[uid]) for uid in dropped}
        assert "Gems" + row_marker(accts[keep]) not in deleted  # the kept user's row is untouched

    def test_widening_a_subset_to_everyone_removes_nothing(self, client: TestClient, monkeypatch):
        """subset → everyone: the audience only grew (old ⊆ new), so dropped = ∅ and nothing is removed.
        A newly included user's row is a create, left for the next gated run — never removed here."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        u_ids = [u["id"] for u in client.get("/api/users").json()]
        keep = u_ids[0]
        created = client.post(
            "/api/collections", json={"name": "Gems", "audience": "subset", "audience_user_ids": [keep]}
        )
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            by_id = {u.id: u for u in session.query(User).all()}
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(run_id=run.id, user_id=keep, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}])
            )
            session.commit()
            slugs = {uid: by_id[uid].slug for uid in u_ids}
            accts = {uid: by_id[uid].plex_account_id for uid in u_ids}

        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("Gems" + row_marker(accts[keep]), f"shortlist_{slugs[keep]}")]
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "audience": "everyone"})
        assert r.status_code == 200
        assert deleted == []  # audience only widened → no reconcile removal

    def test_patching_a_non_audience_field_never_touches_plex(self, client: TestClient, monkeypatch):
        """A size-only PATCH on a per-person row must NOT enter the audience reconcile at all — no Plex
        round-trip. build_context is the sole entry to Plex here, so a spy that must-not-be-called guards
        the touching_audience gate directly (asserting deleted==[] alone couldn't tell a skip from a
        run-that-found-nothing)."""
        from unittest.mock import MagicMock

        created = client.post("/api/collections", json={"name": "Gems", "audience": "everyone"})
        cid = created.json()["id"]
        spy = MagicMock()
        monkeypatch.setattr(client.app.state.run_service, "build_context", spy)

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "size": 15})
        assert r.status_code == 200 and r.json()["size"] == 15
        spy.assert_not_called()

    def _fake_rename_ctx(self, monkeypatch, client, *, titles_by_label, fail=False):
        """Point build_context at a fake Plex whose collections record editTitle() renames.

        titles_by_label: {label -> current title}. Returns the `renames` list of (old, new) titles.
        When `fail`, editTitle raises — to exercise the best-effort/audit failure path (rule 5/9)."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from shortlist.engine.models import EngineConfig

        renames: list[tuple[str, str]] = []
        cols = {}
        for label, title in titles_by_label.items():
            col = MagicMock(title=title)
            if fail:
                col.editTitle.side_effect = RuntimeError("PMS 500 at http://pms:32400/library?X-Plex-Token=SEKRET")
            else:
                col.editTitle.side_effect = lambda new, c=col: renames.append((c.title, new))
            cols[label] = col
        section = SimpleNamespace(title="Movies")
        plex = MagicMock()
        plex.sections.return_value = [section]
        plex.find_owned_collections.side_effect = lambda s, label: [cols[label]] if label in cols else []
        ctx = SimpleNamespace(plex=plex, config=EngineConfig())
        monkeypatch.setattr(client.app.state.run_service, "build_context", lambda **kw: ctx)
        return renames

    def test_renaming_a_row_retitles_each_users_collection_in_place(self, client: TestClient, monkeypatch):
        """Rename → every user who has the row gets their collection retitled in place (multi-row users
        would otherwise keep the old-named copy). New human title, same per-account marker."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            users = session.query(User).order_by(User.id).all()[:2]
            info = [(u.slug, u.plex_account_id) for u in users]
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for u in users:
                session.add(
                    RunUser(
                        run_id=run.id,
                        user_id=u.id,
                        status="ok",
                        breakdown=[{"row_slug": slug, "row_title": "Old Gems"}],
                    )
                )
            session.commit()

        renames = self._fake_rename_ctx(
            monkeypatch,
            client,
            titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct) for uslug, acct in info},
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Buried Treasure"})
        assert r.status_code == 200
        expected = {("Old Gems" + row_marker(acct), "Buried Treasure" + row_marker(acct)) for _, acct in info}
        assert set(renames) == expected  # each account's row retitled, marker preserved

        # The audit records WHOSE row went from what to what, in which libraries (rule 10).
        from shortlist.server.db.models import Event

        with client.app.state.sessions() as session:
            audit = session.query(Event).filter_by(scope="collection.rename").order_by(Event.id.desc()).first()
        by_user = {e["user"]: e for e in audit.message["renames"]}
        assert set(by_user) == {uslug for uslug, _ in info}
        for uslug, _ in info:
            assert by_user[uslug]["old"] == "Old Gems" and by_user[uslug]["new"] == "Buried Treasure"
            assert by_user[uslug]["libraries"] == ["Movies"]

    def test_renaming_to_a_library_name_template_retitles_per_library(self, client: TestClient, monkeypatch):
        """A {library_name} rename renders in the SAME library the old title was delivered in — the
        library is read from the run breakdown, so 'Movies' fills the placeholder with that name."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=user.id,
                    status="ok",
                    breakdown=[{"row_slug": slug, "row_title": "Old Gems", "library_title": "Movies"}],
                )
            )
            session.commit()

        renames = self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct)}
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "✨ {library_name} Fresh"})
        assert r.status_code == 200
        # The Movies library's name fills {library_name}, so the old row is retitled to its Movies form.
        assert renames == [("Old Gems" + row_marker(acct), "✨ Movies Fresh" + row_marker(acct))]

    def test_renaming_via_a_static_name_template_also_reconciles(self, client: TestClient, monkeypatch):
        """A name_template-only change (name untouched) is a rename too — the effective title is the
        template, so changing it must retitle the collection in place."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        created = client.post("/api/collections", json={"name": "Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id, user_id=user.id, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}]
                )
            )
            session.commit()

        renames = self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Gems" + row_marker(acct)}
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "name_template": "Buried Treasure"})
        assert r.status_code == 200
        assert renames == [("Gems" + row_marker(acct), "Buried Treasure" + row_marker(acct))]

    def test_rename_reconcile_survives_a_plex_error(self, client: TestClient, monkeypatch):
        """A PMS failure mid-rename is best-effort: the PATCH still returns 200, and the failure is
        audited with the token redacted (rules 5 + 9) — never surfaced raw or fatal."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Event, Run, RunUser, User

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id, user_id=user.id, status="ok", breakdown=[{"row_slug": slug, "row_title": "Old Gems"}]
                )
            )
            session.commit()

        self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct)}, fail=True
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Buried Treasure"})
        assert r.status_code == 200  # best-effort: the rename failure never fails the PATCH
        with client.app.state.sessions() as session:
            audit = session.query(Event).filter_by(scope="collection.rename").order_by(Event.id.desc()).first()
        assert audit.message["error"] is not None
        assert "SEKRET" not in str(audit.message) and "REDACTED" in audit.message["error"]  # rule 9

    def test_renaming_to_a_dynamic_template_is_left_for_the_next_run(self, client: TestClient, monkeypatch):
        """A {top_seed} template renders to the default title with no picks, so the reconcile skips it
        rather than retitle to the wrong name — the next run's delivery renames the sole-row case."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        created = client.post("/api/collections", json={"name": "Old Gems"})
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            user = session.query(User).order_by(User.id).first()
            uslug, acct = user.slug, user.plex_account_id
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                RunUser(
                    run_id=run.id, user_id=user.id, status="ok", breakdown=[{"row_slug": slug, "row_title": "Old Gems"}]
                )
            )
            session.commit()

        renames = self._fake_rename_ctx(
            monkeypatch, client, titles_by_label={f"shortlist_{uslug}": "Old Gems" + row_marker(acct)}
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Old Gems", "name_template": "{top_seed} Picks"})
        assert r.status_code == 200
        assert renames == []  # dynamic new title → skipped, not retitled to the default name

    def test_renaming_the_default_row_leaves_a_users_own_name_override_untouched(self, client: TestClient, monkeypatch):
        """The default row resolves each user's title as their own `row_name_tpl` or the global template.
        Renaming the global template must retitle a user on the default, but NOT one who set a personal
        name — the reconcile re-renders the override user from THEIR template, sees no change, skips them."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        # Two users on the default row: one on the global template, one with a personal name override.
        with client.app.state.sessions() as session:
            plain, custom = session.query(User).order_by(User.id).all()[:2]
            custom.prefs = {"row_name_tpl": "🌟 My Own Picks"}
            plain_info = (plain.slug, plain.plex_account_id)
            custom_info = (custom.slug, custom.plex_account_id)
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            # Each user's LAST delivered title reflects the template that applied to THEM.
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=plain.id,
                    status="ok",
                    breakdown=[{"row_slug": "picked", "row_title": "✨ Picked for You"}],
                )
            )
            session.add(
                RunUser(
                    run_id=run.id,
                    user_id=custom.id,
                    status="ok",
                    breakdown=[{"row_slug": "picked", "row_title": "🌟 My Own Picks"}],
                )
            )
            session.commit()

        renames = self._fake_rename_ctx(
            monkeypatch,
            client,
            titles_by_label={
                f"shortlist_{plain_info[0]}": "✨ Picked for You" + row_marker(plain_info[1]),
                f"shortlist_{custom_info[0]}": "🌟 My Own Picks" + row_marker(custom_info[1]),
            },
        )

        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        r = client.patch(f"/api/collections/{picked['id']}", json={"name": "✨ Handpicked"})
        assert r.status_code == 200
        # Only the plain user is retitled; the override user's collection is left exactly as it was.
        plain_marker = row_marker(plain_info[1])
        assert renames == [("✨ Picked for You" + plain_marker, "✨ Handpicked" + plain_marker)]

    def test_changing_a_rows_build_removes_the_old_builds_collections(self, client: TestClient, monkeypatch):
        """Flipping per-person → shared removes the old per-person per-user collections, so both builds
        don't live on Home at once. A removal, so gate-exempt."""
        from shortlist.engine.delivery import row_marker
        from shortlist.server.db.models import Run, RunUser, User

        created = client.post("/api/collections", json={"name": "Gems"})  # per_person by default
        cid, slug = created.json()["id"], created.json()["slug"]
        with client.app.state.sessions() as session:
            users = session.query(User).order_by(User.id).all()[:2]
            info = [(u.slug, u.plex_account_id) for u in users]
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            for u in users:
                session.add(
                    RunUser(
                        run_id=run.id, user_id=u.id, status="ok", breakdown=[{"row_slug": slug, "row_title": "Gems"}]
                    )
                )
            session.commit()

        deleted = self._fake_plex_ctx(
            monkeypatch,
            client,
            collections=[("Gems" + row_marker(acct), f"shortlist_{uslug}") for uslug, acct in info],
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Gems", "build": "shared"})
        assert r.status_code == 200 and r.json()["build"] == "shared"
        # Every user's OLD per-person collection was removed (the new shared row builds on the next run).
        assert set(deleted) == {"Gems" + row_marker(acct) for _, acct in info}

    def test_changing_a_shared_row_to_per_person_removes_the_shared_collection(self, client: TestClient, monkeypatch):
        """The other direction of the flip: shared → per-person removes the OLD shared collection (found
        by its own shared label), so it doesn't linger while the new per-person rows build."""
        from shortlist.engine.delivery import row_marker

        created = client.post("/api/collections", json={"name": "Popular", "build": "shared"})
        cid, slug = created.json()["id"], created.json()["slug"]
        deleted = self._fake_plex_ctx(
            monkeypatch, client, collections=[("🔥 Popular" + row_marker(0), f"shortlist__shared_{slug}")]
        )

        r = client.patch(f"/api/collections/{cid}", json={"name": "Popular", "build": "per_person"})
        assert r.status_code == 200 and r.json()["build"] == "per_person"
        assert deleted == ["🔥 Popular" + row_marker(0)]  # the old shared collection removed by its label

    def test_shared_collection_with_subset_audience(self, client: TestClient):
        users = client.get("/api/users").json()
        ids = [u["id"] for u in users]
        created = client.post(
            "/api/collections",
            json={
                "name": "Staff Picks",
                "build": "shared",
                "audience": "subset",
                "audience_user_ids": ids,
                "min_watchers": 3,
            },
        )
        assert created.status_code == 201
        body = created.json()
        assert body["build"] == "shared"
        assert sorted(body["audience_user_ids"]) == sorted(ids)
        assert body["min_watchers"] == 3

    def test_default_picked_cannot_be_deleted(self, client: TestClient):
        picked = next(c for c in client.get("/api/collections").json() if c["slug"] == "picked")
        assert client.delete(f"/api/collections/{picked['id']}").status_code == 422

    def test_validation_rejects_bad_enums(self, client: TestClient):
        assert client.post("/api/collections", json={"name": "X", "build": "nonsense"}).status_code == 422
        assert client.post("/api/collections", json={"name": "X", "media": "vinyl"}).status_code == 422

    def test_candidate_sources_round_trip_and_reject_unknown(self, client: TestClient):
        # Empty by default (inherit the global setting).
        created = client.post("/api/collections", json={"name": "Trakt Row"})
        assert created.status_code == 201 and created.json()["candidate_sources"] == []
        cid = created.json()["id"]
        # A per-row override round-trips through PATCH and GET (the client sends the full body).
        patched = client.patch(
            f"/api/collections/{cid}",
            json={"name": "Trakt Row", "candidate_sources": ["trakt", "tmdb_discover"]},
        )
        assert patched.status_code == 200
        assert patched.json()["candidate_sources"] == ["trakt", "tmdb_discover"]
        # An unknown source id is rejected with a helpful 422, not silently stored.
        bad = client.post("/api/collections", json={"name": "Bad Row", "candidate_sources": ["imdb_magic"]})
        assert bad.status_code == 422

    def test_library_keys_round_trip(self, client: TestClient):
        # Empty by default (every library); a per-row selection round-trips as strings.
        created = client.post("/api/collections", json={"name": "4K Only"})
        assert created.status_code == 201 and created.json()["library_keys"] == []
        cid = created.json()["id"]
        patched = client.patch(f"/api/collections/{cid}", json={"name": "4K Only", "library_keys": ["3", "5"]})
        assert patched.status_code == 200
        assert patched.json()["library_keys"] == ["3", "5"]

    def test_slug_collision_gets_suffixed(self, client: TestClient):
        # Different names (duplicates are rejected) that slugify to the same base collide on slug.
        first = client.post("/api/collections", json={"name": "Date Night"}).json()
        second = client.post("/api/collections", json={"name": "Date-Night!"}).json()
        assert first["slug"] == "date_night"
        assert second["slug"] == "date_night_2"

    def test_duplicate_names_are_rejected(self, client: TestClient):
        assert client.post("/api/collections", json={"name": "Movie Night"}).status_code == 201
        assert client.post("/api/collections", json={"name": "Movie Night"}).status_code == 422


class TestEventsApi:
    def test_audit_log_empty(self, client: TestClient):
        assert client.get("/api/events/log").json() == []


class TestSetupApi:
    def test_wizard_state_round_trip(self, client: TestClient):
        r = client.get("/api/setup/state").json()
        assert r["completed"] is False
        client.put("/api/setup/state", json={"step": 3, "state": {"picked": [1, 2]}, "completed": False})
        r = client.get("/api/setup/state").json()
        assert r["step"] == 3
        assert r["state"] == {"picked": [1, 2]}


class TestUninstall:
    def test_wrong_confirmation_rejected(self, client: TestClient):
        r = client.post("/api/system/uninstall", json={"confirm": "yes"})
        assert r.status_code == 422

    def test_owned_collections_audit_lists_plex_rows_and_flags_orphans(self, client: TestClient, monkeypatch):
        """The cleanup audit lists every shortlist-labelled collection ON PLEX (not from the DB) and
        flags any whose user/row no longer exists — the drift a cleanup exists to catch."""
        from shortlist.server.settings_store import SettingsStore

        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "tok")
            session.commit()

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def list_owned_collections(self, prefix="shortlist"):
                # Plex title-cases labels; users sarah/mike exist, 'ghost' does not.
                return [
                    {"library": "Movies", "title": "Picked for You", "label": "Shortlist_sarah", "rating_key": 1},
                    {"library": "Movies", "title": "Old Row", "label": "Shortlist_ghost", "rating_key": 2},
                    {"library": "TV", "title": "Everyone", "label": "Shortlist__shared_allpicks", "rating_key": 3},
                ]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        data = client.get("/api/system/owned-collections").json()
        assert data["total"] == 3
        by_slug = {c["slug"]: c for c in data["collections"]}
        assert by_slug["sarah"]["orphan"] is False and by_slug["sarah"]["kind"] == "user"
        assert by_slug["ghost"]["orphan"] is True  # no such user -> drift, safe to remove
        assert by_slug["allpicks"]["kind"] == "shared"
        # Orphans are surfaced and listed first.
        assert data["orphans"] == 2
        assert data["collections"][0]["orphan"] is True

    def test_owned_collections_audit_409_when_plex_not_connected(self, client: TestClient):
        # No plex.url/token configured on a fresh app -> a clear 409, not a crash.
        assert client.get("/api/system/owned-collections").status_code == 409


class TestNotifications:
    def test_surface_paused_and_failed_run_most_severe_first(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif
        from shortlist.server.db.models import Run
        from shortlist.server.settings_store import SettingsStore

        monkeypatch.setattr(notif, "check_for_update", lambda _v: None)  # never touch GitHub in a test
        with client.app.state.sessions() as session:
            SettingsStore(session).set("paused_all", True)
            session.add(Run(trigger="manual", status="error"))
            session.commit()

        items = client.get("/api/notifications").json()["notifications"]
        ids = {n["id"] for n in items}
        assert "runs-paused" in ids
        assert any(i.startswith("run-failed-") for i in ids)
        order = {"error": 0, "warning": 1, "info": 2}
        severities = [order[n["severity"]] for n in items]
        assert severities == sorted(severities)  # error before warning before info

    def test_a_partial_run_and_recent_errors_surface_as_warnings(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif
        from shortlist.server.db.models import Event, Run

        monkeypatch.setattr(notif, "check_for_update", lambda _v: None)
        with client.app.state.sessions() as session:
            session.add(Run(trigger="manual", status="ok", stats={"users_ok": 1, "users_error": 2}))
            session.add(Event(scope="requests.send", level="error", message={"detail": "arr down"}))
            session.commit()

        items = client.get("/api/notifications").json()["notifications"]
        by_id = {n["id"]: n for n in items}
        partial = next(n for k, n in by_id.items() if k.startswith("run-partial-"))
        assert "2 people failed" in partial["title"]  # pluralized
        assert "recent-errors" in by_id and by_id["recent-errors"]["severity"] == "warning"

    def test_update_notification_can_be_dismissed_per_version(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif

        monkeypatch.setattr(notif, "check_for_update", lambda _v: {"latest": "9.9.9", "url": "https://example/rel"})
        first = client.get("/api/notifications").json()["notifications"]
        assert any(n["id"] == "update-9.9.9" and n["dismissable"] for n in first)

        assert client.post("/api/notifications/dismiss", json={"id": "update-9.9.9"}).json() == {"ok": True}
        after = client.get("/api/notifications").json()["notifications"]
        assert not any(n["id"] == "update-9.9.9" for n in after)  # dismissed by id

    def test_debug_bundle_reports_facts_but_never_a_secret(self, client: TestClient):
        from shortlist.server.settings_store import SettingsStore

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "SUPERSECRETTOKEN")
            SettingsStore(session).set("plex.url", "http://pms")
            session.commit()

        r = client.get("/api/system/debug")
        assert r.status_code == 200
        text = r.text
        assert "Shortlist debug bundle" in text and "db migration head:" in text
        assert "plex=True" in text  # connection reported as configured...
        assert "SUPERSECRETTOKEN" not in text  # ...but the token itself is never in the bundle
