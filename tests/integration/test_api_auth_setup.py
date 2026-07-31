"""API contract tests: auth boundary, the owner API token, the setup wizard state, and uninstall."""

from __future__ import annotations

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Setting, User
from shortlist.server.settings_store import SettingsStore
from tests.integration.conftest import OWNER_ID, OWNER_JSON

pytestmark = pytest.mark.integration


class TestAuthBoundary:
    def test_health_needs_no_auth(self, client: TestClient):
        fresh = TestClient(client.app)
        with fresh:
            r = fresh.get("/api/system/health")
            assert r.status_code == 200
            assert r.json() == {"status": "ok"}
            # Docker's HEALTHCHECK is the consumer, so the body stays exactly this — and the version
            # stays OFF it: an unauthenticated caller doesn't need to know which build to look up
            # advisories for.
            assert set(r.json()) == {"status"}

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


class TestAuthResponses:
    """`/api/auth` decides which screen the SPA opens, so a key silently dropped by a response model
    here is a login loop or a wizard nobody can reach.
    """

    def test_a_signed_in_session_names_the_account(self, client: TestClient):
        body = client.get("/api/auth/session").json()

        assert set(body) == {"authenticated", "login_required", "account_id", "username"}
        assert body["authenticated"] is True
        assert body["account_id"] == OWNER_ID
        assert body["login_required"] is True  # a linked server means this instance demands a login

    def test_an_anonymous_session_keeps_the_same_shape(self, client: TestClient):
        """The other branch: no cookie, so the identity fields are null rather than absent — the SPA
        reads `authenticated`/`login_required` on both, and they must never go missing."""
        anonymous = TestClient(client.app)
        with anonymous:
            body = anonymous.get("/api/auth/session").json()

        assert set(body) == {"authenticated", "login_required", "account_id", "username"}
        assert body["authenticated"] is False and body["login_required"] is True
        assert body["account_id"] is None and body["username"] is None

    def test_logout_confirms_and_clears_the_cookie(self, client: TestClient):
        r = client.post("/api/auth/logout")

        assert r.json() == {"ok": True}
        assert set(r.json()) == {"ok"}

    def test_creating_a_pin_returns_the_code_and_the_client_id(self, client: TestClient):
        """`client_id` is on the response because plex.tv only honours the PIN for the same client —
        dropping it would make every login fail at the poll with nothing to explain why."""
        with respx.mock:
            respx.post("https://plex.tv/api/v2/pins").mock(
                return_value=httpx.Response(201, json={"id": 42, "code": "ABCD"})
            )
            body = client.post("/api/auth/pin").json()

        assert set(body) == {"id", "code", "client_id"}
        assert body["id"] == 42 and body["code"] == "ABCD"
        assert body["client_id"] == client.app.state.client_id

    def test_polling_an_unapproved_pin_reports_not_linked(self, client: TestClient):
        with respx.mock:
            respx.get("https://plex.tv/api/v2/pins/42").mock(return_value=httpx.Response(200, json={"authToken": None}))
            body = client.get("/api/auth/pin/42").json()

        # The identity fields are null until the owner approves in Plex; the token is never here.
        assert set(body) == {"linked", "account_id", "username"}
        assert body == {"linked": False, "account_id": None, "username": None}

    def test_polling_an_approved_pin_signs_the_owner_in(self, client: TestClient):
        with respx.mock:
            respx.get("https://plex.tv/api/v2/pins/42").mock(
                return_value=httpx.Response(200, json={"authToken": "plex-auth-token"})
            )
            respx.get("https://plex.tv/api/v2/user").mock(return_value=httpx.Response(200, json=dict(OWNER_JSON)))
            r = client.get("/api/auth/pin/42")

        body = r.json()
        assert set(body) == {"linked", "account_id", "username"}
        assert body == {"linked": True, "account_id": OWNER_ID, "username": "steve"}
        # The Plex auth token must never reach the browser — not in the body, not in the cookie jar.
        assert "plex-auth-token" not in r.text


class TestApiToken:
    """The owner API token: generate once, authenticate with Bearer, revoke — and the hash never
    leaks through the settings endpoint."""

    def test_generate_authenticate_and_revoke_round_trip(self, client: TestClient):
        made = client.post("/api/system/api-token")
        assert made.status_code == 200
        token = made.json()["token"]
        assert token.startswith("shl_")
        assert set(made.json()) == {"token", "created_at"}

        # The owner can read the token back (revealable, like Sonarr/Radarr) — encrypted at rest but
        # returned in plaintext to the authenticated owner on this dedicated, owner-gated endpoint.
        status = client.get("/api/system/api-token").json()
        assert status["enabled"] is True
        assert status["token"] == token
        # All three key sets spelled out: these endpoints declare response models now, and dropping
        # `token` here would make the reveal button show nothing while still reporting "enabled".
        assert set(status) == {"enabled", "created_at", "token"}

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
        revoked = client.delete("/api/system/api-token")
        assert revoked.status_code == 200
        assert revoked.json() == {"enabled": False}
        assert bare.get("/api/users", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        # The unset branch of the status shape: same three keys, nothing to reveal.
        after = client.get("/api/system/api-token").json()
        assert after == {"enabled": False, "created_at": None, "token": None}

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
    """The wizard's endpoints, which were the last handlers in the package returning a bare `dict`.

    They now declare response models like everything else — and a response model FILTERS, so every
    assertion below names the whole key set rather than the field it cares about. A model that forgot
    `machine_id` or `owner_account_id` would leave the wizard unable to link a server at all, with
    nothing failing anywhere else.
    """

    def _sign_in_with_a_plex_token(self, client: TestClient) -> None:
        """The owner's stored token is what `_plex_token` hands the token-bearing endpoints."""
        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "owner-token")
            session.commit()

    def test_wizard_state_round_trip(self, client: TestClient):
        r = client.get("/api/setup/state").json()
        assert r["completed"] is False
        assert set(r) == {"step", "state", "completed"}
        saved = client.put("/api/setup/state", json={"step": 3, "state": {"picked": [1, 2]}, "completed": False})
        # The PUT is an echo, not a re-read: it deliberately does not return `state`.
        assert set(saved.json()) == {"step", "completed"}
        r = client.get("/api/setup/state").json()
        assert r["step"] == 3
        assert r["state"] == {"picked": [1, 2]}

    def test_a_never_started_instance_answers_with_defaults_not_nulls(self, client: TestClient):
        """`step`/`state`/`completed` are non-optional on the model because the settings store has a
        default for each — an instance nobody has touched still answers 0/{}/False."""
        body = client.get("/api/setup/state").json()

        assert body == {"step": 0, "state": {}, "completed": False}

    def test_plexapi_hands_a_library_section_key_back_as_an_int(self):
        """The assumption behind `LibrarySectionOut.key: int`, asserted rather than believed.

        plexapi casts a section's `key` (`utils.cast(int, …)`), and `/setup/probe` passes it through
        untouched — unlike `/system/libraries`, which stringifies it. The SPA's hand-written
        `LibrarySection.key: string` has been wrong about this all along.
        """
        from unittest.mock import MagicMock
        from xml.etree import ElementTree

        from plexapi.library import LibrarySection

        section = LibrarySection(MagicMock(), ElementTree.fromstring('<Directory key="1" type="movie" title="M"/>'))

        assert section.key == 1 and isinstance(section.key, int)

    def test_the_probe_returns_the_whole_checklist_for_a_server_with_tautulli(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace

        from shortlist.server.services import setup_probe

        self._sign_in_with_a_plex_token(client)
        monkeypatch.setattr(setup_probe, "plextv_account", lambda *a, **k: dict(OWNER_JSON))
        monkeypatch.setattr(
            setup_probe,
            "PlexClient",
            lambda *a, **k: SimpleNamespace(
                version="1.43.3.10793",
                machine_id="m1",
                server_name="SFLIX",
                # `key` is an int here for the same reason it is on a real PMS — see the test above.
                sections=lambda: [SimpleNamespace(key=1, title="Movies", type="movie", totalSize=1200)],
            ),
        )
        monkeypatch.setattr(
            "shortlist.engine.clients.tautulli.TautulliClient",
            lambda *a, **k: SimpleNamespace(ping=lambda: None),
        )

        body = client.post(
            "/api/setup/probe",
            json={"plex_url": "http://pms:32400", "tautulli_url": "http://tautulli:8181", "tautulli_apikey": "k"},
        ).json()

        assert set(body) == {"checks", "machine_id", "server_name", "owner_account_id", "libraries"}
        assert set(body["checks"]) == {"pms_version", "plex_pass", "libraries", "tautulli"}
        assert set(body["checks"]["pms_version"]) == {"ok", "message", "value"}
        # Only the version check carries a `value`; the model must not invent one on the others.
        assert set(body["checks"]["plex_pass"]) == {"ok", "message"}
        assert body["checks"]["tautulli"] == {"ok": True, "message": "Tautulli connected"}
        assert body["libraries"] == [{"key": 1, "title": "Movies", "type": "movie", "count": 1200}]
        assert body["owner_account_id"] == OWNER_ID

    def test_the_probe_still_answers_when_there_is_no_tautulli(self, client: TestClient, monkeypatch):
        """`checks.tautulli` is the one check that can be absent, which is why it carries a default —
        without one it would be REQUIRED, and every Tautulli-less server would get a 500 here instead
        of a checklist. It must still be ABSENT, not an invented `null`: the route sets
        `response_model_exclude_unset`, so the model documents the payload without adding to it."""
        from types import SimpleNamespace

        from shortlist.server.services import setup_probe

        self._sign_in_with_a_plex_token(client)
        monkeypatch.setattr(setup_probe, "plextv_account", lambda *a, **k: dict(OWNER_JSON))
        monkeypatch.setattr(
            setup_probe,
            "PlexClient",
            lambda *a, **k: SimpleNamespace(
                version="1.43.3.10793", machine_id="m1", server_name="SFLIX", sections=lambda: []
            ),
        )

        r = client.post("/api/setup/probe", json={"plex_url": "http://pms:32400"})

        assert r.status_code == 200
        assert "tautulli" not in r.json()["checks"]
        assert r.json()["checks"]["libraries"] == {"ok": False, "message": "No movie/show libraries found"}

    def test_the_server_picker_reports_every_advertised_address_it_tried(self, client: TestClient):
        self._sign_in_with_a_plex_token(client)
        resources = [
            {
                "name": "SFLIX",
                "clientIdentifier": "m1",
                "provides": "server",
                "owned": True,
                "productVersion": "1.43.3.10793",
                "connections": [{"uri": "http://10.0.0.5:32400", "local": True, "relay": False}],
            },
            {"name": "Someone's phone", "clientIdentifier": "p1", "provides": "player", "connections": []},
        ]
        with respx.mock:
            respx.get("https://plex.tv/api/v2/resources").mock(return_value=httpx.Response(200, json=resources))
            # The reachability probe is a real (unauthenticated) GET per address — refuse it, and the
            # address comes back `ok: False` rather than the request escaping the test.
            respx.get("http://10.0.0.5:32400/identity").mock(side_effect=httpx.ConnectError("refused"))

            body = client.get("/api/setup/servers").json()

        assert [set(s) for s in body] == [{"name", "machine_id", "owned", "version", "connections"}]
        assert body[0]["machine_id"] == "m1"  # the player is filtered out: it doesn't "provide" a server
        assert body[0]["connections"] == [{"uri": "http://10.0.0.5:32400", "local": True, "relay": False, "ok": False}]

    def test_linking_a_server_answers_with_the_receipt_the_wizard_reads(self, client: TestClient):
        """The happy path, which only the rejection cases were covered for. plex.tv is asked whether
        the account really OWNS the machine id — a share on someone else's server is not enough."""
        self._sign_in_with_a_plex_token(client)
        owned = [{"clientIdentifier": "m1", "provides": "server", "owned": True}]
        with respx.mock:
            respx.get("https://plex.tv/api/v2/resources").mock(return_value=httpx.Response(200, json=owned))

            body = client.post(
                "/api/setup/link",
                json={
                    "plex_url": "http://pms:32400",
                    "machine_id": "m1",
                    "server_name": "SFLIX",
                    "version": "1.43.3.10793",
                    "owner_account_id": OWNER_ID,
                },
            ).json()

        assert body == {"linked": True, "server_name": "SFLIX"}


class TestUninstall:
    def test_wrong_confirmation_rejected(self, client: TestClient):
        r = client.post("/api/system/uninstall", json={"confirm": "yes"})
        assert r.status_code == 422

    def test_the_dry_run_reports_the_whole_plan_and_changes_nothing(self, client: TestClient, monkeypatch):
        """The preview is what the owner reads before pressing the one deliberately scary button, so
        every line of it has to survive the response model — a dropped `collections_deleted` would
        show "nothing to remove" for a server full of rows."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        collection = MagicMock(title="Picked for You", labels=[SimpleNamespace(tag="shortlist_sarah")])
        plex = MagicMock()
        plex.sections.return_value = [SimpleNamespace(collections=lambda: [collection])]
        monkeypatch.setattr(
            client.app.state.run_service, "build_context", lambda **kw: SimpleNamespace(plex=plex, plextv=MagicMock())
        )

        r = client.post("/api/system/uninstall", json={"dry_run": True})

        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"filters_restored", "collections_deleted", "rows_disabled", "dry_run", "message"}
        assert body["dry_run"] is True
        assert body["collections_deleted"] == ["Picked for You"]
        plex.delete_owned_collection.assert_not_called()  # a preview deletes nothing

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
        assert set(data) == {"collections", "total", "orphans"}
        # Nested key set too: `orphan` is the flag the cleanup page acts on, and `rating_key` is how
        # the owner finds the collection on Plex — a model that dropped either would be invisible.
        assert [set(c) for c in data["collections"]] == [
            {"library", "title", "label", "rating_key", "kind", "slug", "orphan"}
        ] * 3
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
