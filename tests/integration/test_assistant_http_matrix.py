"""Direct HTTP contracts omitted by the SDK tool matrix.

All state is disposable: owner browser provenance is a signed synthetic cookie
and the only credential issued is a local test grant credential.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select

from shortlist.server.assistant.changes import ChangeError
from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.models import AssistantOAuthCode
from shortlist.server.assistant_auth.policy import basic_role_capabilities, owner_managed_capabilities
from shortlist.server.assistant_auth.types import ASSISTANT_CAPABILITIES
from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Event, Server, Setting
from shortlist.server.main import create_app

pytestmark = pytest.mark.integration

HTTP_CONTRACTS = frozenset(
    {
        ("GET", "/mcp"),
        ("POST", "/mcp"),
        ("DELETE", "/mcp"),
        ("GET", "/api/catalogs/settings"),
        ("GET", "/api/catalogs/templates"),
        ("GET", "/api/assistant/status"),
        ("GET", "/api/assistant/changes/{change_id}"),
        ("POST", "/api/assistant/changes/{change_id}/approve"),
        ("GET", "/assistant/grants"),
        ("GET", "/assistant/destinations"),
        ("POST", "/assistant/grants"),
        ("PATCH", "/assistant/grants/{grant_id}"),
        ("POST", "/assistant/grants/{grant_id}/credentials"),
        ("POST", "/assistant/grants/{grant_id}/revoke"),
        ("DELETE", "/assistant/grants/{grant_id}"),
        ("GET", "/assistant/oauth/authorize"),
        ("POST", "/assistant/oauth/authorize"),
        ("POST", "/assistant/oauth/consent"),
        ("POST", "/assistant/oauth/token"),
        ("POST", "/assistant/oauth/revoke"),
        ("POST", "/assistant/oauth/register"),
        ("GET", "/.well-known/oauth-authorization-server"),
        ("GET", "/.well-known/oauth-authorization-server/{issuer_path:path}"),
        ("GET", "/.well-known/oauth-protected-resource/mcp"),
        ("GET", "/.well-known/oauth-protected-resource/{resource_path:path}"),
    }
)


@contextmanager
def _owner_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, base_path: str = ""
) -> Iterator[tuple[TestClient, FastAPI]]:
    monkeypatch.setenv("APP_BASE_PATH", base_path)
    monkeypatch.setenv("SHORTLIST_MCP_URL", f"http://localhost{base_path}/mcp")
    app = create_app(config_dir=tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        with app.state.sessions() as session:
            session.add(Server(machine_id="http-matrix", url="http://plex.invalid", token_enc="x", owner_account_id=42))
            session.commit()
        client.cookies.set(SESSION_COOKIE, session_serializer(app.state.session_secret).dumps({"account_id": 42}))
        client.headers[CSRF_HEADER] = "1"
        yield client, app


def _routes(app: FastAPI) -> set[tuple[str, str]]:
    prefixes = ("/mcp", "/api/catalogs", "/api/assistant", "/assistant/", "/.well-known/oauth-")
    routes = [
        effective
        for route in app.routes
        for effective in (route.effective_route_contexts() if hasattr(route, "effective_route_contexts") else (route,))
    ]
    return {
        (method, route.path)
        for route in routes
        if getattr(route, "path", "").startswith(prefixes)
        for method in getattr(route, "methods", ())
        if method in {"GET", "POST", "PATCH", "DELETE"}
    }


def test_assistant_http_route_inventory_and_discovery_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        assert _routes(app) == HTTP_CONTRACTS
        settings = client.get("/api/catalogs/settings")
        assert settings.status_code == 200
        assert any(item["key"] == "runs.retention" and item["assistant_writable"] for item in settings.json())
        templates = client.get("/api/catalogs/templates")
        assert templates.status_code == 200
        assert len(templates.json()) > 1
        assert all(item["id"] and item["effective_values"] for item in templates.json())
        status = client.get("/api/assistant/status")
        assert status.status_code == 200 and status.json()["resource"] == "http://localhost/mcp"
        authorization = client.get("/.well-known/oauth-authorization-server")
        assert authorization.status_code == 200
        metadata = authorization.json()
        assert metadata["issuer"] == "http://localhost/assistant/oauth"
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        resource = client.get("/.well-known/oauth-protected-resource/mcp")
        assert resource.status_code == 200 and resource.json()["resource"] == "http://localhost/mcp"


def test_configured_service_catalog_requires_owner_browser_and_returns_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        with app.state.sessions() as session:
            session.add_all(
                [
                    Setting(key="curator.provider", value={"v": "openai"}),
                    Setting(key="curator.api_key", value={"v": "encrypted-secret"}),
                    Setting(key="searxng.url", value={"v": "http://localhost:8080/search/"}),
                ]
            )
            session.commit()
        response = client.get("/assistant/destinations")
        assert response.status_code == 200
        by_id = {choice["service_id"]: choice for choice in response.json()}
        assert by_id["curator"]["destination_id"] == "https://api.openai.com/v1"
        assert by_id["searxng"]["destination_id"] == "http://localhost:8080/search"
        assert "encrypted-secret" not in response.text
        assert "tmdb" not in by_id
        assert client.get("/assistant/destinations", headers={"Authorization": "Bearer fake"}).status_code == 403
        client.cookies.clear()
        assert client.get("/assistant/destinations").status_code == 401


def test_owner_profile_create_and_upgrade_are_explicit_browser_only_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        ordinary = client.post(
            "/assistant/grants",
            json={"client_id": "legacy", "name": "Earlier", "preset": "inspect"},
        )
        assert ordinary.status_code == 201
        legacy = ordinary.json()
        assert legacy["constraints"]["owner_managed"] is False
        assert (
            client.patch(
                f"/assistant/grants/{legacy['id']}",
                json={"expected_revision": legacy["revision"], "constraints": {"owner_managed": True}},
            ).status_code
            == 422
        )
        assert (
            client.patch(
                f"/assistant/grants/{legacy['id']}",
                json={"expected_revision": legacy["revision"], "upgrade_owner_managed": True},
                headers={"Authorization": "Bearer fake"},
            ).status_code
            == 403
        )
        assert app.state.assistant_auth.repository.get_grant_context(legacy["id"]).constraints.owner_managed is False
        upgraded = client.patch(
            f"/assistant/grants/{legacy['id']}",
            json={"expected_revision": legacy["revision"], "upgrade_owner_managed": True},
        )
        assert upgraded.status_code == 200, upgraded.text
        assert upgraded.json()["constraints"]["owner_managed"] is True
        assert (
            client.patch(
                f"/assistant/grants/{legacy['id']}",
                json={"expected_revision": legacy["revision"], "upgrade_owner_managed": True},
            ).status_code
            == 409
        )
        new = client.post(
            "/assistant/grants",
            json={
                "client_id": "new-owner-managed",
                "name": "New",
                "preset": "owner_automation",
                "owner_managed": True,
                "constraints": {"max_provider_calls": 0},
            },
        )
        assert new.status_code == 201, new.text
        assert new.json()["constraints"]["owner_managed"] is True
        assert new.json()["constraints"]["destination_ids"] == []
        assert "maintenance.execute" in new.json()["capabilities"]
        assert "secrets.read" not in new.json()["capabilities"]
        assert (
            client.post(
                "/assistant/grants",
                json={
                    "client_id": "forged",
                    "name": "Forged",
                    "preset": "owner_automation",
                    "owner_managed": True,
                    "constraints": {"destination_ids": ["https://unregistered.invalid"]},
                },
            ).status_code
            == 422
        )


def test_grant_credential_and_stateless_mcp_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        repository = app.state.assistant_auth.repository
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="http-matrix",
            name="HTTP matrix",
            preset=GrantPreset.INSPECT,
            capabilities=frozenset({Capability.INSTANCE_READ}),
            constraints=GrantConstraints(include_future_people=True),
        )
        issued = client.post(f"/assistant/grants/{grant.grant_id}/credentials", json={"expires_in_days": 1})
        assert issued.status_code == 201
        credential = issued.json()["credential"]
        headers = {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/json",
            "MCP-Protocol-Version": "2025-11-25",
        }
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "matrix", "version": "1"},
            },
        }
        assert client.post("/mcp", json=body, headers=headers).status_code == 200
        # This stateless runtime has no session to terminate.  A GET without
        # SSE acceptance is rejected rather than opening an unbounded stream.
        assert client.get("/mcp", headers=headers).status_code == 406
        status, response_headers = client.portal.call(_open_and_disconnect_sse, app, credential)
        assert status == 200
        assert response_headers[b"content-type"].startswith(b"text/event-stream")
        assert client.delete("/mcp", headers=headers).status_code == 405
        assert (
            client.post(
                "/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers
            ).status_code
            == 202
        )
        bad = {**headers, "MCP-Protocol-Version": "unsupported"}
        assert (
            client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=bad).status_code
            == 400
        )
        assert (
            client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                headers={**headers, "Origin": "https://evil.invalid"},
            ).status_code
            == 403
        )


async def _open_and_disconnect_sse(app: FastAPI, credential: str) -> tuple[int, dict[bytes, bytes]]:
    """Read GET's real response headers, then disconnect without waiting for an SSE event."""
    started = asyncio.Event()
    response: dict = {}
    request_sent = False

    async def receive() -> dict:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            response.update(message)
            started.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "root_path": "",
        "client": ("127.0.0.1", 1234),
        "server": ("localhost", 80),
        "headers": [
            (b"host", b"localhost"),
            (b"accept", b"text/event-stream"),
            (b"authorization", f"Bearer {credential}".encode()),
            (b"mcp-protocol-version", b"2025-11-25"),
        ],
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    return response["status"], dict(response["headers"])


def _create_grant(client: TestClient, *, client_id: str = "http-matrix") -> dict:
    response = client.post(
        "/assistant/grants",
        json={"client_id": client_id, "name": "HTTP matrix", "preset": "inspect", "expires_in_days": 1},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mcp_initialize(client: TestClient, credential: str) -> Response:
    return client.post(
        "/mcp",
        headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "matrix", "version": "1"},
            },
        },
    )


@pytest.mark.parametrize("requested_scope", ["all_advertised", "instance.read", "config.write"])
def test_oauth_owner_narrowed_consent_binds_code_and_token_to_approved_intersection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requested_scope: str
) -> None:
    """Owner-selected narrower consent must not silently expand the code or bearer authority."""
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        redirect_uri = "http://127.0.0.1:43219/callback"
        registration = client.post(
            "/assistant/oauth/register", json={"client_name": "Reduced consent", "redirect_uris": [redirect_uri]}
        )
        assert registration.status_code == 201
        client_id = registration.json()["client_id"]
        preset = "owner_automation" if requested_scope in {"all_advertised", "instance.read"} else "inspect"
        create_body = {"client_id": client_id, "name": "Reduced owner approval", "preset": preset}
        if requested_scope == "instance.read":
            create_body.update(owner_managed=True, capabilities=["instance.read"])
        created = client.post("/assistant/grants", json=create_body)
        assert created.status_code == 201
        grant = created.json()
        original_capabilities = set(grant["capabilities"])
        if requested_scope == "instance.read":
            assert grant["constraints"]["owner_managed"] is True
            assert grant["full_management"] is False
        requested = (
            {capability.value for capability in ASSISTANT_CAPABILITIES}
            if requested_scope == "all_advertised"
            else {requested_scope}
        )
        approved_scopes = requested & original_capabilities
        if requested_scope == "all_advertised":
            assert len(requested) == 28 and len(approved_scopes) == 23
        verifier = "reduced-consent-verifier-" + "v" * 48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        authorization = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": "http://localhost/mcp",
            "scope": " ".join(sorted(requested)),
            "state": "reduced-owner-consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        flow = client.post("/assistant/oauth/authorize", json=authorization)
        assert flow.status_code == 200, flow.text
        decision = client.post(
            "/assistant/oauth/consent",
            json={
                "flow_id": flow.json()["flow_id"],
                "csrf_token": flow.json()["csrf_token"],
                "approved": True,
                "grant_id": grant["id"],
            },
        )
        if not approved_scopes:
            assert decision.status_code == 400, decision.text
            assert decision.json()["error"] == "invalid_scope"
            with app.state.sessions() as session:
                assert session.scalars(select(AssistantOAuthCode)).all() == []
        else:
            assert decision.status_code == 200, decision.text
            query = parse_qs(urlsplit(decision.json()["redirect_to"]).query)
            assert query["state"] == [authorization["state"]]
            if approved_scopes != requested:
                assert set(query["scope"][0].split()) == approved_scopes
            with app.state.sessions() as session:
                codes = session.scalars(select(AssistantOAuthCode)).all()
                assert len(codes) == 1 and set(codes[0].scope.split()) == approved_scopes
            token = client.post(
                "/assistant/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "resource": authorization["resource"],
                    "code": query["code"][0],
                    "code_verifier": verifier,
                },
            )
            assert token.status_code == 200, token.text
            assert set(token.json()["scope"].split()) == approved_scopes
            credential = token.json()["access_token"]
            verified = app.state.assistant_auth.oauth.verify_access_token(credential)
            assert verified is not None
            assert set(verified.scopes) == approved_scopes
            assert _mcp_initialize(client, credential).status_code == 200
            name, arguments = (
                ("shortlist_plan_maintenance", {"request": {"task": "cache.refresh"}})
                if requested_scope == "all_advertised"
                else ("shortlist_get_configuration", {"request": {"group": "recommendations"}})
            )
            denied = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )
            assert denied.status_code == 200
            result = denied.json()["result"]
            if requested_scope == "all_advertised":
                data = result["structuredContent"]["data"]
                assert "maintenance.execute" in data["required_capabilities"]
                assert data["authorization"]["can_apply"] is False
            else:
                assert result["isError"] is True
                assert "missing_permission" in str(result)
                if requested_scope == "instance.read":
                    widened = client.patch(
                        f"/assistant/grants/{grant['id']}",
                        json={
                            "expected_revision": grant["revision"],
                            "upgrade_owner_managed": True,
                        },
                    )
                    assert widened.status_code == 200, widened.text
                    assert widened.json()["constraints"]["owner_managed"] is True
                    assert widened.json()["full_management"] is True
                    assert "config.write" in widened.json()["capabilities"]
                    still_denied = client.post(
                        "/mcp",
                        headers={"Authorization": f"Bearer {credential}", "Accept": "application/json"},
                        json={
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/call",
                            "params": {"name": name, "arguments": arguments},
                        },
                    )
                    assert still_denied.status_code == 200
                    assert still_denied.json()["result"]["isError"] is True
                    assert "missing_permission" in str(still_denied.json()["result"])
        expected_capabilities = (
            {capability.value for capability in owner_managed_capabilities()}
            if requested_scope == "instance.read"
            else original_capabilities
        )
        assert set(client.get("/assistant/grants").json()[0]["capabilities"]) == expected_capabilities


@pytest.mark.parametrize("base_path", ["", "/shortlist"])
def test_discovery_aliases_preserve_canonical_resource_and_reject_other_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_path: str
) -> None:
    with _owner_client(tmp_path, monkeypatch, base_path=base_path) as (client, _app):
        client.cookies.clear()
        issuer = f"http://localhost{base_path}/assistant/oauth"
        resource = f"http://localhost{base_path}/mcp"
        authorization = client.get(f"{base_path}/.well-known/oauth-authorization-server")
        alias = client.get(f"/.well-known/oauth-authorization-server{base_path}/assistant/oauth")
        assert authorization.status_code == alias.status_code == 200
        assert authorization.json() == alias.json()
        assert alias.json()["issuer"] == issuer
        assert alias.json()["token_endpoint"] == f"{issuer}/token"
        assert alias.json()["code_challenge_methods_supported"] == ["S256"]
        protected = client.get(f"{base_path}/.well-known/oauth-protected-resource/mcp")
        protected_alias = client.get(f"/.well-known/oauth-protected-resource{base_path}/mcp")
        assert protected.status_code == protected_alias.status_code == 200
        assert protected.json() == protected_alias.json()
        assert protected.json()["resource"] == resource
        assert protected.json()["authorization_servers"] == [issuer]
        assert client.get("/.well-known/oauth-authorization-server/other").status_code == 404
        assert client.get("/.well-known/oauth-protected-resource/other").status_code == 404


def test_owner_grant_lifecycle_changes_live_access_and_removes_revoked_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        assert client.get("/assistant/grants").json() == []
        grant = _create_grant(client)
        grant_id = grant["id"]
        assert app.state.assistant_auth.repository.get_grant_context(grant_id).owner_account_id == 42
        assert client.get("/assistant/grants").json()[0]["id"] == grant_id
        issued = client.post(f"/assistant/grants/{grant_id}/credentials", json={"expires_in_days": 1})
        assert issued.status_code == 201
        credential = issued.json()["credential"]
        assert _mcp_initialize(client, credential).status_code == 200
        assert client.get("/assistant/grants").json()[0]["local_credential_count"] == 1
        patch = {"expected_revision": grant["revision"], "constraints": {"max_batch_size": 3}}
        updated = client.patch(f"/assistant/grants/{grant_id}", json=patch)
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == grant["revision"] + 1
        assert app.state.assistant_auth.repository.get_grant_context(grant_id).constraints.max_batch_size == 3
        assert client.patch(f"/assistant/grants/{grant_id}", json=patch).status_code == 409
        assert client.delete(f"/assistant/grants/{grant_id}").status_code == 409
        revoked = client.post(f"/assistant/grants/{grant_id}/revoke")
        assert revoked.status_code == 200 and revoked.json() == {"revoked": True}
        assert _mcp_initialize(client, credential).status_code == 401
        assert client.get("/assistant/grants").json()[0]["revoked_at"] is not None
        assert client.delete(f"/assistant/grants/{grant_id}").status_code == 204
        assert client.get("/assistant/grants").json() == []
        assert _mcp_initialize(client, credential).status_code == 401


@pytest.mark.parametrize("token_hint", ["access_token", "refresh_token"])
def test_oauth_pkce_consent_token_rotation_and_revoke_invalidate_bearer_and_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token_hint: str
) -> None:
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        redirect_uri = "http://127.0.0.1:43219/callback"
        registration = client.post(
            "/assistant/oauth/register", json={"client_name": "HTTP matrix", "redirect_uris": [redirect_uri]}
        )
        assert registration.status_code == 201, registration.text
        client_id = registration.json()["client_id"]
        assert app.state.assistant_auth.repository.get_oauth_client(client_id).redirect_uris == [redirect_uri]
        assert client.get("/assistant/grants").json() == []
        grant = _create_grant(client, client_id=client_id)
        verifier = "http-matrix-verifier-" + "v" * 48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        authorization = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "resource": "http://localhost/mcp",
            "scope": "instance.read",
            "state": "matrix-state",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        opened = client.get("/assistant/oauth/authorize", params=authorization, follow_redirects=False)
        assert opened.status_code == 303
        assert urljoin(str(opened.request.url), opened.headers["location"]) == (
            f"http://localhost/assistant/consent?{urlencode(authorization)}"
        )
        flow = client.post("/assistant/oauth/authorize", json=authorization)
        assert flow.status_code == 200, flow.text
        assert flow.json()["client"]["id"] == client_id
        assert flow.json()["requested_scopes"] == ["instance.read"]
        decision = {
            "flow_id": flow.json()["flow_id"],
            "csrf_token": flow.json()["csrf_token"],
            "approved": True,
            "grant_id": grant["id"],
        }
        assert client.post("/assistant/oauth/consent", json={**decision, "csrf_token": "wrong"}).status_code == 400
        consent = client.post("/assistant/oauth/consent", json=decision)
        assert consent.status_code == 200, consent.text
        target = urlsplit(consent.json()["redirect_to"])
        assert f"{target.scheme}://{target.netloc}{target.path}" == redirect_uri
        query = parse_qs(target.query)
        assert query["state"] == ["matrix-state"]
        exchange = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code": query["code"][0],
            "code_verifier": verifier,
            "resource": "http://localhost/mcp",
        }
        token = client.post("/assistant/oauth/token", data=exchange)
        assert token.status_code == 200, token.text
        assert token.json()["scope"] == "instance.read"
        assert _mcp_initialize(client, token.json()["access_token"]).status_code == 200
        assert client.post("/assistant/oauth/token", data=exchange).status_code == 400
        rotated = client.post(
            "/assistant/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": token.json()["refresh_token"],
                "resource": "http://localhost/mcp",
            },
        )
        assert rotated.status_code == 200, rotated.text
        tokens = rotated.json()
        assert tokens["refresh_token"] != token.json()["refresh_token"]
        assert _mcp_initialize(client, tokens["access_token"]).status_code == 200
        revoked = client.post(
            "/assistant/oauth/revoke",
            data={
                "client_id": client_id,
                "token": tokens[token_hint],
                "token_type_hint": token_hint,
            },
        )
        assert revoked.status_code == 200, revoked.text
        assert _mcp_initialize(client, tokens["access_token"]).status_code == 401
        if token_hint == "access_token":
            # Access-only revocation preserves the refresh authorization. Revoking
            # a refresh credential is the operation that closes the whole family.
            refreshed = client.post(
                "/assistant/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": tokens["refresh_token"],
                    "resource": "http://localhost/mcp",
                },
            )
            assert refreshed.status_code == 200, refreshed.text
            tokens = refreshed.json()
            assert (
                client.post(
                    "/assistant/oauth/revoke",
                    data={"client_id": client_id, "token": tokens["refresh_token"], "token_type_hint": "refresh_token"},
                ).status_code
                == 200
            )
            assert _mcp_initialize(client, tokens["access_token"]).status_code == 401
        rejected = client.post(
            "/assistant/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": tokens["refresh_token"],
                "resource": "http://localhost/mcp",
            },
        )
        assert rejected.status_code == 400 and rejected.json()["error"] == "invalid_grant"
        assert client.get("/assistant/grants").json()[0]["revoked_at"] is None


def test_consent_flow_names_the_read_only_scopes_from_the_view_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consent page used to keep its own copy of this list; the server's View role is the one source."""
    with _owner_client(tmp_path, monkeypatch) as (client, _app):
        redirect_uri = "http://127.0.0.1:43219/callback"
        registered = client.post(
            "/assistant/oauth/register", json={"client_name": "Read only", "redirect_uris": [redirect_uri]}
        )
        client_id = registered.json()["client_id"]
        verifier = "read-only-verifier-" + "v" * 48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        flow = client.post(
            "/assistant/oauth/authorize",
            json={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "resource": "http://localhost/mcp",
                "scope": "instance.read",
                "state": "s",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert flow.status_code == 200, flow.text
        expected = sorted(capability.value for capability in basic_role_capabilities("view"))
        assert flow.json()["read_only_scopes"] == expected
        assert "rows.update" not in expected


def test_owner_change_review_approval_is_exact_csrf_protected_and_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner_client(tmp_path, monkeypatch) as (client, app):
        grant = _create_grant(client)
        principal = app.state.assistant_auth.repository.get_grant_context(grant["id"])
        service = app.state.assistant_changes
        change = service.prepare(principal, "configuration", {"values": {"runs.retention": 17}})
        change_id = change["change_id"]
        assert change["authorization"]["can_apply"] is False
        with pytest.raises(ChangeError):
            service.apply(principal, change_id, "unapproved")
        reviewed = client.get(f"/api/assistant/changes/{change_id}")
        assert reviewed.status_code == 200
        assert reviewed.json()["summary"]["configuration_diff"]["runs.retention"]["to"] == 17
        assert reviewed.json()["approved"] is False
        assert "config.write" in reviewed.json()["requirements"]["capabilities"]
        del client.headers[CSRF_HEADER]
        assert client.post(f"/api/assistant/changes/{change_id}/approve").status_code == 403
        client.headers[CSRF_HEADER] = "1"
        assert (
            client.post(
                f"/api/assistant/changes/{change_id}/approve", headers={"Authorization": "Bearer unrelated"}
            ).status_code
            == 403
        )
        with app.state.sessions() as session:
            assert session.get(AssistantChange, change_id).approved_by is None
        approved = client.post(f"/api/assistant/changes/{change_id}/approve")
        assert approved.status_code == 200, approved.text
        assert approved.json()["authorization"] == {"can_apply": True, "approved": True}
        with app.state.sessions() as session:
            stored = session.get(AssistantChange, change_id)
            assert stored.approved_by == 42
            assert stored.approved_hash == reviewed.json()["content_hash"]
            assert session.scalars(select(Event).where(Event.scope == "assistant.approved")).one()
        receipt = service.apply(principal, change_id, "owner-approved")
        assert receipt["authorization_basis"] == "operation_approval"
        with app.state.sessions() as session:
            assert session.get(Setting, "runs.retention").value == {"v": 17}
            assert session.get(AssistantOperation, receipt["operation_id"]).change_id == change_id
        reread = client.get(f"/api/assistant/changes/{change_id}").json()
        assert reread["approved"] is True and reread["operation_id"] == receipt["operation_id"]
        assert client.post(f"/api/assistant/changes/{change_id}/approve").status_code == 409
        assert client.get("/api/assistant/changes/missing").status_code == 404
