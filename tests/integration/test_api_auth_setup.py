"""API contract tests: auth boundary, the owner API token, the setup wizard state, and uninstall."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Setting, User
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration


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

    def test_health_is_the_only_unauthenticated_system_route(self, client: TestClient):
        """`/api/system` is the router with uninstall, the debug bundle and the API token on it.

        It used to be the ONE router that opted INTO auth per endpoint — seventeen individual
        `dependencies=[Depends(require_owner)]` arguments, where every sibling declares it once at
        construction. All of them were correct, so this was never a live hole; it was a structural
        trap on the worst possible router, where one forgotten argument ships unauthenticated with
        nothing to catch it.

        Asserted against the ROUTE TABLE rather than by calling each endpoint, so a route added
        tomorrow is covered by this test the day it is written, not the day someone remembers to
        exercise it.
        """
        from fastapi.routing import APIRoute

        from shortlist.server.api import system
        from shortlist.server.auth import require_owner

        def flatten(router, prefix=""):
            """(path, route) for every endpoint, walking through deferred `include_router` wrappers."""
            for route in router.routes:
                if isinstance(route, APIRoute):
                    yield prefix + route.path, route
                    continue
                inner = getattr(route, "original_router", None)
                assert inner is not None, f"unrecognised route object {type(route)}"
                context = getattr(route, "include_context", None)
                yield from flatten(inner, prefix + (getattr(context, "prefix", "") or ""))

        gates = {
            f"{method} {path}": require_owner in [d.call for d in route.dependant.dependencies]
            for path, route in flatten(system.router)
            for method in sorted(route.methods)
        }
        assert sorted(name for name, gated in gates.items() if not gated) == ["GET /system/health"], gates
        # …and the set is non-trivial, so a walk that found nothing can't pass vacuously.
        assert len(gates) > 15
        for worst in ("POST /system/uninstall", "GET /system/debug", "GET /system/api-token"):
            assert gates[worst] is True

        # The same three, behaviourally, so the assertion above can never be true of a route table
        # that the app does not actually serve.
        anonymous = TestClient(client.app)
        with anonymous:
            assert anonymous.get("/api/system/health").status_code == 200
            assert anonymous.get("/api/system/debug").status_code == 401
            assert anonymous.get("/api/system/api-token").status_code == 401
            # 403, not 401: a mutation without a session fails the CSRF check first. Either way it
            # is refused before the handler — which is the whole point of this endpoint being gated.
            assert anonymous.post("/api/system/uninstall", json={"confirm": "UNINSTALL"}).status_code == 403


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
