"""Real ASGI lifespan and legacy MCP wire contracts with no external service calls."""

import base64
import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.auth import CSRF_HEADER, SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Server
from shortlist.server.main import create_app
from tests.assistant_oauth import issue_pair
from tests.shared_app import app_for
from tests.uvicorn_thread import UvicornThread

pytestmark = pytest.mark.integration


@contextmanager
def _client(tmp_path, monkeypatch, *, prefix="", enabled=True, capabilities=None, constraints=None):
    monkeypatch.setenv("APP_BASE_PATH", prefix)
    if enabled:
        monkeypatch.setenv("SHORTLIST_MCP_URL", f"http://localhost{prefix}/mcp")
    else:
        monkeypatch.delenv("SHORTLIST_MCP_URL", raising=False)
    app = app_for(tmp_path)
    with TestClient(app, base_url="http://localhost") as client:
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="mcp-test",
                    url="http://unreachable.invalid",
                    token_enc="not-a-token",
                    owner_account_id=42,
                )
            )
            session.commit()
        if enabled:
            repository = app.state.assistant_auth.repository
            grant = repository.create_grant(
                owner_account_id=42,
                client_id="mcp-test-client",
                name="MCP test",
                preset=GrantPreset.INSPECT,
                constraints=constraints or GrantConstraints(),
                capabilities=capabilities,
            )
            credential = repository.issue_local_credential(grant.grant_id).take()
            client.headers["Authorization"] = f"Bearer {credential}"
            client.headers["Accept"] = "application/json, text/event-stream"
            client.headers["MCP-Protocol-Version"] = "2025-06-18"
        yield client, app
    assert app.state.assistant_transport is None


def _rpc(client, path, method, params=None, *, message_id=1):
    return client.post(path, json={"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})


@pytest.mark.parametrize("prefix", ["", "/shortlist"])
def test_legacy_handshake_schema_and_structured_tool_result(tmp_path, monkeypatch, prefix):
    with _client(tmp_path, monkeypatch, prefix=prefix) as (client, app):
        path = f"{prefix}/mcp"
        initialized = _rpc(
            client,
            path,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "legacy-contract-test", "version": "1.0"},
            },
        )
        assert initialized.status_code == 200, initialized.text
        assert initialized.json()["result"]["protocolVersion"] == "2025-06-18"
        listed = _rpc(client, path, "tools/list", message_id=2)
        assert listed.status_code == 200, listed.text
        tools = {tool["name"]: tool for tool in listed.json()["result"]["tools"]}
        assert {
            "shortlist_get_instance",
            "shortlist_plan_row",
            "shortlist_plan_run",
            "shortlist_apply_change",
        } <= tools.keys()
        assert tools["shortlist_apply_change"]["annotations"]["readOnlyHint"] is False
        assert "outputSchema" in tools["shortlist_get_instance"]
        result = _rpc(client, path, "tools/call", {"name": "shortlist_get_instance", "arguments": {}}, message_id=3)
        assert result.status_code == 200, result.text
        body = result.json()["result"]
        assert not body.get("isError", False), body
        assert body["structuredContent"]["summary"]
        assert body["structuredContent"]["data"]
        assert app.state.assistant_transport is not None


def test_missing_credential_and_untrusted_origin_are_rejected(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as (client, _app):
        authorization = client.headers.pop("Authorization")
        response = _rpc(client, "/mcp", "tools/list")
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        client.headers["Authorization"] = authorization
        client.headers["Origin"] = "https://attacker.invalid"
        assert _rpc(client, "/mcp", "tools/list").status_code in {400, 403}


def test_permission_denial_is_a_structured_tool_error(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, capabilities={Capability.INSTANCE_READ}) as (client, _app):
        result = _rpc(
            client,
            "/mcp",
            "tools/call",
            {
                "name": "shortlist_list_people",
                "arguments": {"request": {"limit": 10, "offset": 0}},
            },
        )
        assert result.status_code == 200, result.text
        body = result.json()["result"]
        assert body["isError"] is True
        assert "missing_permission" in str(body)
        assert "Traceback" not in str(body)


def test_revoked_bearer_is_rejected_on_next_http_request(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from shortlist.server.assistant_auth.models import AssistantGrant

    with _client(tmp_path, monkeypatch) as (client, app):
        with app.state.sessions() as session:
            session.query(AssistantGrant).one().revoked_at = datetime.now(UTC)
            session.commit()
        assert _rpc(client, "/mcp", "tools/list").status_code == 401


def test_browser_owner_can_remove_only_already_revoked_grants_idempotently(tmp_path, monkeypatch):
    from shortlist.server.assistant_auth.models import AssistantGrant
    from shortlist.server.db.models import Event

    with _client(tmp_path, monkeypatch) as (client, app):
        repository = app.state.assistant_auth.repository
        target = repository.create_grant(
            owner_account_id=42,
            client_id="remove-target",
            name="Old revoked connection",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        repository.issue_local_credential(target.grant_id)
        repository.revoke_grant(target.grant_id)
        active = repository.create_grant(
            owner_account_id=42,
            client_id="remove-active",
            name="Still active",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        expired = repository.create_grant(
            owner_account_id=42,
            client_id="remove-expired",
            name="Expired but not revoked",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        foreign = repository.create_grant(
            owner_account_id=7,
            client_id="remove-foreign",
            name="Foreign revoked connection",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        repository.revoke_grant(foreign.grant_id)
        client.cookies.set(
            SESSION_COOKIE,
            session_serializer(app.state.session_secret).dumps({"account_id": 42, "username": "owner"}),
        )
        path = f"/assistant/grants/{target.grant_id}"
        browser_headers = {CSRF_HEADER: "1"}

        assert client.delete(path, headers=browser_headers).status_code == 403
        client.headers.pop("Authorization")
        assert client.delete(path).status_code == 403
        assert client.delete(f"/assistant/grants/{foreign.grant_id}", headers=browser_headers).status_code == 204
        with app.state.sessions() as session:
            assert session.get(AssistantGrant, foreign.grant_id) is not None
        assert client.delete(f"/assistant/grants/{active.grant_id}", headers=browser_headers).status_code == 409
        assert client.delete(f"/assistant/grants/{expired.grant_id}", headers=browser_headers).status_code == 409
        assert client.delete(path, headers=browser_headers).status_code == 204
        assert client.delete(path, headers=browser_headers).status_code == 204
        with app.state.sessions() as session:
            assert session.get(AssistantGrant, target.grant_id) is None
            audits = session.query(Event).filter_by(scope="assistant.grant.removed").all()
            assert len(audits) == 1
            assert audits[0].message == {
                "grant_id": target.grant_id,
                "actor": {"via": "browser", "account_id": 42},
            }


@pytest.mark.parametrize("utc_offset_hours", [10, -7])
def test_browser_owner_can_patch_only_explicit_grant_constraints_without_reconnecting_bearers(
    tmp_path, monkeypatch, utc_offset_hours
):
    from shortlist.server.assistant_auth.models import AssistantGrant
    from shortlist.server.db.models import Event, User

    with _client(tmp_path, monkeypatch) as (client, app):
        repository = app.state.assistant_auth.repository
        original_expiry = datetime(2027, 1, 3, 15, 0, tzinfo=timezone(timedelta(hours=utc_offset_hours)))
        expected_expiry = original_expiry.astimezone(UTC)
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="constraint-patch-client",
            name="Original grant name",
            preset=GrantPreset.OWNER_AUTOMATION,
            capabilities={
                Capability.INSTANCE_READ,
                Capability.CATALOG_READ,
                Capability.PEOPLE_READ,
                Capability.CHANGES_PREPARE,
                Capability.ROWS_CREATE,
                Capability.AUDIENCES_WRITE,
                Capability.SCHEDULES_WRITE,
            },
            constraints=GrantConstraints(
                row_ids=frozenset({1}),
                person_ids=frozenset({10}),
                library_keys=frozenset({"1"}),
                destination_ids=frozenset({"https://provider.example/v1"}),
                max_batch_size=4,
                max_work_per_operation=9,
                max_provider_calls=3,
            ),
            expires_at=original_expiry,
        )
        assert grant.expires_at == expected_expiry
        local_credential = repository.issue_local_credential(grant.grant_id).take()
        oauth = app.state.assistant_auth.oauth
        repository.register_oauth_client(
            client_id=grant.client_id,
            client_name="Constraint patch test",
            redirect_uris=["http://127.0.0.1:49152/callback"],
        )
        oauth_credential = issue_pair(
            oauth,
            owner_account_id=42,
            grant_id=grant.grant_id,
            client_id=grant.client_id,
            redirect_uri="http://127.0.0.1:49152/callback",
            scopes={capability.value for capability in grant.capabilities},
        ).access_token

        client.cookies.set(
            SESSION_COOKIE,
            session_serializer(app.state.session_secret).dumps({"account_id": 42, "username": "owner"}),
        )
        path = f"/assistant/grants/{grant.grant_id}"
        browser_headers = {CSRF_HEADER: "1"}
        client.headers["Authorization"] = f"Bearer {local_credential}"
        denied_bearer = client.patch(
            path,
            headers=browser_headers,
            json={"expected_revision": grant.revision, "constraints": {"row_ids": []}},
        )
        assert denied_bearer.status_code == 403
        client.headers.pop("Authorization")
        assert client.patch(path, json={"expected_revision": grant.revision, "constraints": {}}).status_code == 403
        listing = client.get("/assistant/grants")
        assert listing.status_code == 200, listing.json()
        listed = next(item for item in listing.json() if item["id"] == grant.grant_id)
        listed_expiry = datetime.fromisoformat(listed["expires_at"])
        assert listed_expiry == expected_expiry
        assert listed_expiry == original_expiry
        assert listed_expiry.utcoffset() == timedelta(0)

        response = client.patch(
            path,
            headers=browser_headers,
            json={
                "expected_revision": grant.revision,
                "constraints": {"row_ids": [], "max_batch_size": None, "max_provider_calls": 0},
            },
        )

        assert response.status_code == 200, response.json()
        updated = response.json()
        assert updated["revision"] == grant.revision + 1
        assert updated["constraints"] == {
            "row_ids": [],
            "library_keys": ["1"],
            "setting_groups": [],
            "destination_ids": ["https://provider.example/v1"],
            "include_future_rows": False,
            "include_future_libraries": False,
            "max_batch_size": None,
            "max_work_per_operation": 9,
            "max_provider_calls": 0,
            "owner_managed": False,
        }
        assert updated["requires_access_approval"] is False
        authority_fields = (
            "owner_account_id",
            "client_id",
            "name",
            "preset",
            "capabilities",
        )
        assert {field: updated[field] for field in authority_fields} == {
            "owner_account_id": grant.owner_account_id,
            "client_id": grant.client_id,
            "name": grant.name,
            "preset": grant.preset.value,
            "capabilities": sorted(capability.value for capability in grant.capabilities),
        }
        updated_expiry = datetime.fromisoformat(updated["expires_at"])
        assert updated_expiry == original_expiry
        assert updated_expiry.utcoffset() == timedelta(0)
        with app.state.sessions() as session:
            audit = session.query(Event).filter_by(scope="assistant.grant.update").one()
            assert audit.message["actor"] == {"via": "browser", "account_id": 42}
            assert audit.message["changed_fields"] == ["max_batch_size", "max_provider_calls", "row_ids"]
            assert audit.message["revision"] == updated["revision"]
            assert "https://provider.example/v1" not in str(audit.message)

        for payload in (
            {"expected_revision": updated["revision"], "constraints": {"unknown": 1}},
            {"expected_revision": updated["revision"], "constraints": {}, "unknown": 1},
            {"expected_revision": 0, "constraints": {}},
        ):
            assert client.patch(path, headers=browser_headers, json=payload).status_code == 422
        assert (
            client.patch(
                path,
                headers=browser_headers,
                json={"expected_revision": grant.revision, "constraints": {"person_ids": []}},
            ).status_code
            == 422
        )

        foreign = repository.create_grant(
            owner_account_id=7,
            client_id="foreign-client",
            name="Foreign",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        assert (
            client.patch(
                f"/assistant/grants/{foreign.grant_id}",
                headers=browser_headers,
                json={"expected_revision": 1, "constraints": {"row_ids": []}},
            ).status_code
            == 404
        )
        assert (
            client.patch(
                "/assistant/grants/grt_missing",
                headers=browser_headers,
                json={"expected_revision": 1, "constraints": {"row_ids": []}},
            ).status_code
            == 404
        )
        revoked = repository.create_grant(
            owner_account_id=42,
            client_id="revoked-client",
            name="Revoked",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        repository.revoke_grant(revoked.grant_id)
        assert (
            client.patch(
                f"/assistant/grants/{revoked.grant_id}",
                headers=browser_headers,
                json={"expected_revision": 2, "constraints": {"row_ids": []}},
            ).status_code
            == 409
        )
        expired = repository.create_grant(
            owner_account_id=42,
            client_id="expired-client",
            name="Expired",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        assert (
            client.patch(
                f"/assistant/grants/{expired.grant_id}",
                headers=browser_headers,
                json={"expected_revision": expired.revision, "constraints": {"row_ids": []}},
            ).status_code
            == 409
        )

        with app.state.sessions() as session:
            session.add(User(id=10, username="recipient", slug="recipient", plex_account_id=10))
            session.add(User(id=11, username="new-recipient", slug="new-recipient", plex_account_id=11))
            session.commit()
        client.headers["Authorization"] = f"Bearer {oauth_credential}"
        people_before_patch = _tool_call(client, "shortlist_list_people", {"limit": 100})["data"]
        assert {person["id"] for person in people_before_patch["items"]} == {10, 11}
        client.headers.update(
            {
                "Authorization": f"Bearer {local_credential}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            }
        )
        prepared = _tool_call(
            client,
            "shortlist_plan_row",
            {
                "action": "create",
                "template_id": "picked-for-you",
                "values": {
                    "name": "Stale after grant edit",
                    "enabled": False,
                    "schedule": "",
                    "audience": "subset",
                    "audience_user_ids": [10],
                    "library_keys": ["1"],
                },
            },
        )["data"]
        client.headers.pop("Authorization")
        latest = client.patch(
            path,
            headers=browser_headers,
            json={
                "expected_revision": updated["revision"],
                "constraints": {"library_keys": []},
            },
        )
        assert latest.status_code == 200, latest.json()
        client.headers["Authorization"] = f"Bearer {local_credential}"
        stale = _tool_call(
            client,
            "shortlist_apply_change",
            {"change_id": prepared["change_id"], "idempotency_key": "grant-edit-makes-plan-stale"},
            expected_error=True,
        )
        assert stale["isError"] is True
        _prefix, separator, error_json = stale["content"][0]["text"].partition(": ")
        assert separator == ": "
        assert json.loads(error_json)["code"] == "stale_plan"
        assert _rpc(client, "/mcp", "tools/list").status_code == 200
        client.headers["Authorization"] = f"Bearer {oauth_credential}"
        instance = _tool_call(client, "shortlist_get_instance")["data"]
        assert instance["grant_revision"] == latest.json()["revision"]
        assert instance["constraints"]["library_keys"] == []
        people_after_patch = _tool_call(client, "shortlist_list_people", {"limit": 100})["data"]
        assert {person["id"] for person in people_after_patch["items"]} == {10, 11}
        with app.state.sessions() as session:
            stored = session.get(AssistantGrant, grant.grant_id)
            assert stored.revision == latest.json()["revision"]


def test_disabled_endpoint_returns_json_instead_of_spa(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch, enabled=False) as (client, _app):
        for method in ("get", "post", "delete"):
            response = getattr(client, method)("/mcp")
            assert response.status_code == 503
            assert response.json() == {"error": "assistant_access_disabled"}


@pytest.mark.parametrize(
    ("constraints", "expected_rows", "expected_future_rows", "expected_libraries", "expected_future_libraries"),
    [
        ({}, [], True, [], True),
        ({"row_ids": [7]}, [7], False, [], True),
        ({"library_keys": ["7"]}, [], True, ["7"], False),
        ({"row_ids": [7], "include_future_rows": True}, [7], True, [], True),
        ({"library_keys": ["7"], "include_future_libraries": True}, [], True, ["7"], True),
    ],
)
def test_owner_create_grant_defaults_only_unselected_resource_types_to_future_access(
    tmp_path,
    monkeypatch,
    constraints,
    expected_rows,
    expected_future_rows,
    expected_libraries,
    expected_future_libraries,
):
    with _client(tmp_path, monkeypatch) as (client, app):
        client.headers.pop("Authorization")
        client.cookies.set(
            SESSION_COOKIE,
            session_serializer(app.state.session_secret).dumps({"account_id": 42, "username": "owner"}),
        )
        response = client.post(
            "/assistant/grants",
            headers={CSRF_HEADER: "1"},
            json={
                "client_id": "owner-create",
                "name": "Owner create",
                "preset": "inspect",
                "constraints": constraints,
            },
        )

    assert response.status_code == 201, response.json()
    saved = response.json()["constraints"]
    assert saved["row_ids"] == expected_rows
    assert saved["include_future_rows"] is expected_future_rows
    assert saved["library_keys"] == expected_libraries
    assert saved["include_future_libraries"] is expected_future_libraries


@pytest.mark.parametrize(("mode", "bridge"), [("auto", False), ("legacy", False), ("auto", True), ("legacy", True)])
def test_official_sdk_client_round_trip_over_loopback(tmp_path, monkeypatch, mode, bridge):
    """Exercise real streaming HTTP framing and SDK lifecycle, beyond ASGI request simulation."""
    import asyncio
    import os
    import socket
    import sys

    import httpx2
    from mcp import Client, StdioServerParameters
    from mcp.client.streamable_http import streamable_http_client

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    monkeypatch.setenv("SHORTLIST_MCP_URL", url)
    monkeypatch.delenv("APP_BASE_PATH", raising=False)
    app = create_app(config_dir=tmp_path)
    server = UvicornThread(app, sock=sock, log_level="error", access_log=False)
    try:
        server.start()
        with app.state.sessions() as session:
            session.add(
                Server(machine_id="sdk-test", url="http://unused.invalid", token_enc="unused", owner_account_id=42)
            )
            session.commit()
        repository = app.state.assistant_auth.repository
        grant = repository.create_grant(
            owner_account_id=42,
            client_id="sdk-test",
            name="SDK test",
            preset=GrantPreset.INSPECT,
            constraints=GrantConstraints(),
        )
        credential = repository.issue_local_credential(grant.grant_id).take()

        async def exercise():
            async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {credential}"}) as http:
                endpoint = (
                    StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "shortlist.server.assistant.stdio_bridge"],
                        env={**os.environ, "SHORTLIST_MCP_URL": url, "SHORTLIST_MCP_CREDENTIAL": credential},
                    )
                    if bridge
                    else streamable_http_client(url, http_client=http)
                )
                async with Client(endpoint, mode=mode) as session:
                    await check_client(session, http)

        async def check_client(session, http):
            assert session.protocol_version
            listing = await session.list_tools()
            assert any(tool.name == "shortlist_get_instance" for tool in listing.tools)
            result = await session.call_tool("shortlist_get_instance", {})
            assert not result.is_error
            assert result.structured_content["summary"]
            malformed = await session.call_tool(
                "shortlist_plan_people",
                {
                    "request": {
                        "person_id": 1,
                        "patch": {"prefs": {"parental_controls": "off"}},
                    }
                },
            )
            assert malformed.is_error
            resources = await session.list_resources()
            templates = await session.list_resource_templates()
            assert templates.resource_templates
            resource = await session.read_resource("shortlist://guides/setup")
            assert resource.contents
            if bridge:
                async with Client(streamable_http_client(url, http_client=http), mode=mode) as direct:
                    original = await direct.list_tools()
                    assert [tool.model_dump(mode="json") for tool in listing.tools] == [
                        tool.model_dump(mode="json") for tool in original.tools
                    ]
                    assert resources.resources == (await direct.list_resources()).resources
                    assert templates.resource_templates == (await direct.list_resource_templates()).resource_templates

        asyncio.run(exercise())
    finally:
        server.stop()
    assert not server.is_alive()
    assert app.state.assistant_transport is None


def _tool_call(client, name, request=None, *, expected_error=False):
    response = _rpc(
        client,
        "/mcp",
        "tools/call",
        {"name": name, "arguments": {} if request is None else {"request": request}},
    )
    assert response.status_code == 200, response.text
    body = response.json()["result"]
    assert bool(body.get("isError")) is expected_error, (name, body)
    return body if expected_error else body["structuredContent"]


def test_dynamic_registration_requires_explicit_consent_before_narrow_oauth_mcp_access(tmp_path, monkeypatch):
    requested_scopes = {Capability.INSTANCE_READ.value, Capability.CATALOG_READ.value, Capability.CONFIG_READ.value}
    redirect_uri = "http://127.0.0.1:49152/callback"
    code_verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
    monkeypatch.setenv("SHORTLIST_MCP_URL", "http://localhost/mcp")
    monkeypatch.delenv("APP_BASE_PATH", raising=False)
    app = create_app(config_dir=tmp_path)

    with TestClient(app, base_url="http://localhost") as client:
        with app.state.sessions() as session:
            session.add(
                Server(
                    machine_id="dcr-integration-test",
                    url="http://unreachable.invalid",
                    token_enc="not-a-token",
                    owner_account_id=42,
                )
            )
            session.commit()
        repository = app.state.assistant_auth.repository
        registration = client.post(
            "/assistant/oauth/register",
            json={
                "client_name": "Codex",
                "redirect_uris": [redirect_uri],
                "scope": " ".join(sorted(requested_scopes)),
                "application_type": "native",
            },
        )
        assert registration.status_code == 201, registration.json()
        registered = registration.json()
        assert set(registered).isdisjoint({"scope", "application_type"})
        assert repository.list_grant_summaries(42) == []

        client.cookies.set(
            SESSION_COOKIE,
            session_serializer(app.state.session_secret).dumps({"account_id": 42, "username": "owner"}),
        )
        owner_headers = {CSRF_HEADER: "1"}
        authorization = client.post(
            "/assistant/oauth/authorize",
            headers=owner_headers,
            json={
                "client_id": registered["client_id"],
                "redirect_uri": redirect_uri,
                "resource": app.state.assistant_auth.oauth.resource,
                "scope": " ".join(sorted(requested_scopes)),
                "state": "test-state",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert authorization.status_code == 200, authorization.json()
        flow = authorization.json()
        grant = client.post(
            "/assistant/grants",
            headers=owner_headers,
            json={
                "client_id": registered["client_id"],
                "name": "Synthetic Codex read grant",
                "preset": "inspect",
                "capabilities": sorted(requested_scopes),
                "constraints": {},
            },
        )
        assert grant.status_code == 201, grant.json()
        approved = client.post(
            "/assistant/oauth/consent",
            headers=owner_headers,
            json={
                "flow_id": flow["flow_id"],
                "csrf_token": flow["csrf_token"],
                "approved": True,
                "grant_id": grant.json()["id"],
            },
        )
        assert approved.status_code == 200, approved.json()
        code = parse_qs(urlsplit(approved.json()["redirect_to"]).query)["code"][0]
        token = client.post(
            "/assistant/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": registered["client_id"],
                "redirect_uri": redirect_uri,
                "resource": app.state.assistant_auth.oauth.resource,
                "code_verifier": code_verifier,
            },
        )
        assert token.status_code == 200, token.json()
        assert set(token.json()["scope"].split()) == requested_scopes

        client.headers.update(
            {
                "Authorization": f"Bearer {token.json()['access_token']}",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            }
        )
        initialized = _rpc(
            client,
            "/mcp",
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Codex", "version": "0.160"}},
        )
        assert initialized.status_code == 200, initialized.text
        assert _tool_call(client, "shortlist_get_instance", None)["data"]
        denied = _tool_call(
            client,
            "shortlist_plan_configuration",
            {"values": {"row.name_template": "Synthetic denied change"}},
            expected_error=True,
        )
        assert "missing_permission" in str(denied)


def test_rich_discovery_tools_serialize_and_enforce_row_library_selection(tmp_path, monkeypatch):
    from shortlist.server.db.models import Collection, CollectionAudience, Setting, Theme, User

    constraints = GrantConstraints(
        row_ids=frozenset({10}),
        person_ids=frozenset({10}),
        library_keys=frozenset({"1"}),
        setting_groups=frozenset({"metadata"}),
        include_future_rows=True,
        include_future_people=True,
        include_future_libraries=True,
    )
    with _client(
        tmp_path,
        monkeypatch,
        constraints=constraints,
        capabilities={
            Capability.INSTANCE_READ,
            Capability.CONFIG_READ,
            Capability.CATALOG_READ,
            Capability.PEOPLE_READ,
        },
    ) as (client, app):
        with app.state.sessions() as session:
            session.add(Theme(id=20, slug="editorial", name="Editorial theme", genres=["Thriller"]))
            session.flush()
            session.add_all(
                [
                    User(id=10, username="permitted", slug="permitted", plex_account_id=10),
                    User(id=11, username="private-person", slug="private-person", plex_account_id=11),
                    Collection(
                        id=10, slug="permitted", name="Permitted", audience="subset", library_keys=["1"], theme_id=20
                    ),
                    Collection(id=11, slug="private-row", name="Private row", audience="subset", library_keys=["2"]),
                    Setting(key="tmdb.apikey", value={"v": "fake-never-disclose"}),
                ]
            )
            session.flush()
            session.add_all(
                [CollectionAudience(collection_id=10, user_id=10), CollectionAudience(collection_id=11, user_id=11)]
            )
            session.commit()
        calls = [
            ("shortlist_get_instance", None),
            ("shortlist_get_setup_status", None),
            ("shortlist_get_guide", {"topic": "setup"}),
            ("shortlist_describe_settings", {"group": "metadata", "limit": 100}),
            ("shortlist_get_configuration", {"group": "metadata"}),
            ("shortlist_list_templates", None),
            ("shortlist_list_people", {"limit": 100}),
            ("shortlist_list_rows", {"limit": 100}),
            ("shortlist_get_row", {"id": 10}),
            ("shortlist_list_themes", {"limit": 100}),
            ("shortlist_get_theme", {"id": 20}),
            ("shortlist_list_seasons", None),
            ("shortlist_diagnose", {"topic": "row", "row_id": 10}),
        ]
        results = {name: _tool_call(client, name, request) for name, request in calls}
        assert results["shortlist_list_seasons"]["data"]["items"]
        assert results["shortlist_list_templates"]["data"]["items"]
        assert results["shortlist_get_configuration"]["data"]["values"]["tmdb.apikey"]["configured"] is True
        assert "fake-never-disclose" not in str(results)
        people = {item["id"] for item in results["shortlist_list_people"]["data"]["items"]}
        rows = {item["id"] for item in results["shortlist_list_rows"]["data"]["items"]}
        assert people == {10, 11}
        assert {10, 11} <= rows
        _tool_call(client, "shortlist_get_configuration", {"group": "requests"}, expected_error=True)
        _tool_call(client, "shortlist_list_people", {"limit": 1, "unexpected": True}, expected_error=True)
        _tool_call(client, "shortlist_unknown_tool", expected_error=True)
        resource = _rpc(client, "/mcp", "resources/read", {"uri": "shortlist://guides/setup"})
        assert resource.status_code == 200
        assert resource.json()["result"]["contents"]


@pytest.mark.parametrize("credential_kind", ["local", "oauth"])
def test_created_row_is_available_with_same_bearer_and_without_scope_widening(tmp_path, monkeypatch, credential_kind):
    from shortlist.server.assistant.operation_models import AssistantOperation
    from shortlist.server.assistant_auth.types import ASSISTANT_CAPABILITIES
    from shortlist.server.db.models import User

    constraints = GrantConstraints(person_ids=frozenset({10}), library_keys=frozenset({"1"}))
    capabilities = set(ASSISTANT_CAPABILITIES)
    with _client(tmp_path, monkeypatch, capabilities=capabilities, constraints=constraints) as (client, app):
        with app.state.sessions() as session:
            session.add(User(id=10, username="recipient", slug="recipient", plex_account_id=10))
            session.commit()
        effective = {capability.value for capability in capabilities}
        if credential_kind == "oauth":
            oauth = app.state.assistant_auth.oauth
            repository = app.state.assistant_auth.repository
            grant = repository.find_grant_for_client("mcp-test-client")
            redirect = "http://127.0.0.1:49152/callback"
            repository.register_oauth_client(
                client_id=grant.client_id,
                client_name="Scoped test",
                redirect_uris=[redirect],
                token_endpoint_auth_method="none",
            )
            effective.remove(Capability.AI_GENERATE.value)
            issued = issue_pair(
                oauth,
                owner_account_id=42,
                grant_id=grant.grant_id,
                client_id=grant.client_id,
                redirect_uri=redirect,
                scopes=effective,
            )
            assert issued.ok, issued.body
            client.headers["Authorization"] = f"Bearer {issued.access_token}"
        # Save a second proposal first: creation must invalidate old plans but preserve the connection.
        intent = {
            "action": "create",
            "template_id": "picked-for-you",
            "values": {
                "name": "New owned row",
                "enabled": False,
                "schedule": "",
                "audience": "subset",
                "audience_user_ids": [10],
                "library_keys": ["1"],
            },
        }
        stale = _tool_call(client, "shortlist_plan_row", intent)["data"]
        plan = _tool_call(client, "shortlist_plan_row", intent)["data"]
        receipt = _tool_call(
            client,
            "shortlist_apply_change",
            {
                "change_id": plan["change_id"],
                "idempotency_key": "create-owned-row-once",
            },
        )["data"]
        assert receipt["result"]["grant_revision"] == 2
        row_id = receipt["result"]["row_id"]
        operation = _tool_call(client, "shortlist_get_operation", {"operation_id": receipt["operation_id"]})
        assert operation["data"]["operation_id"] == receipt["operation_id"]
        assert _tool_call(client, "shortlist_get_row", {"id": row_id})["data"]["name"] == "New owned row"
        with app.state.sessions() as session:
            stored = session.get(AssistantOperation, receipt["operation_id"])
            assert set(stored.result["_effective_capabilities"]) == effective
        _tool_call(
            client,
            "shortlist_apply_change",
            {
                "change_id": stale["change_id"],
                "idempotency_key": "stale-create-must-fail",
            },
            expected_error=True,
        )
        second_intent = {**intent, "values": {**intent["values"], "name": "Another owned row"}}
        second = _tool_call(client, "shortlist_plan_row", second_intent)["data"]
        _tool_call(
            client,
            "shortlist_apply_change",
            {
                "change_id": second["change_id"],
                "idempotency_key": "create-another-owned-row",
            },
        )
        original = _tool_call(client, "shortlist_get_operation", {"operation_id": receipt["operation_id"]})
        assert original["data"]["result"]["row_id"] == row_id
        from shortlist.server.assistant_auth.models import AssistantGrant

        with app.state.sessions() as session:
            current = session.query(AssistantGrant).one()
            current.constraints = {**current.constraints, "library_keys": []}
            current.revision += 1
            session.commit()
        progress = _tool_call(client, "shortlist_get_operation", {"operation_id": receipt["operation_id"]})["data"]
        assert progress["details_available"] is False
        assert "result" not in progress and "job_ids" not in progress


def test_previous_owner_bearer_cannot_read_tools_or_resources_after_relink(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as (client, app):
        before = _rpc(client, "/mcp", "tools/call", {"name": "shortlist_get_instance", "arguments": {}})
        assert not before.json()["result"].get("isError", False)
        with app.state.sessions() as session:
            session.query(Server).one().owner_account_id = 999
            session.commit()
        after = _rpc(client, "/mcp", "tools/call", {"name": "shortlist_get_instance", "arguments": {}})
        assert after.json()["result"]["isError"]
        assert "grant_id" not in str(after.json()["result"])
        resource = _rpc(client, "/mcp", "resources/read", {"uri": "shortlist://guides/setup"})
        assert "error" in resource.json()
        assert "Complete Plex ownership" not in resource.text


@pytest.mark.parametrize("kind", ["row", "person", "settings"])
def test_proposed_changes_do_not_disclose_ungranted_current_values(tmp_path, monkeypatch, kind):
    from shortlist.server.assistant_auth.models import AssistantGrant
    from shortlist.server.db.models import Collection, Setting, User

    with _client(tmp_path, monkeypatch) as (client, app):
        with app.state.sessions() as session:
            session.add(
                User(
                    id=1,
                    plex_account_id=101,
                    slug="private-person",
                    username="private-person",
                    nickname="PRIVATE_PERSON_VALUE",
                )
            )
            session.add(Collection(id=999, slug="private-row", name="Private row", description="PRIVATE_ROW_VALUE"))
            session.merge(Setting(key="row.name_template", value={"v": "PRIVATE_SETTINGS_VALUE"}))
            session.commit()
        name, definition = {
            "row": ("shortlist_plan_row", {"action": "update", "row_id": 999, "values": {"description": "New"}}),
            "person": ("shortlist_plan_people", {"person_id": 1, "patch": {"nickname": "New nickname"}}),
            "settings": ("shortlist_plan_configuration", {"values": {"row.name_template": "New row"}}),
        }[kind]
        response = _rpc(client, "/mcp", "tools/call", {"name": name, "arguments": {"request": definition}})
        result = response.json()["result"]
        assert not result.get("isError"), result
        change = result["structuredContent"]["data"]
        assert change["authorization"]["can_apply"] is False
        for stage in ("prepared", "approved", "narrowed"):
            if stage == "approved":
                app.state.assistant_changes.approve(change["change_id"], owner_account_id=42)
            elif stage == "narrowed":
                with app.state.sessions() as session:
                    session.query(AssistantGrant).one().revision += 1
                    session.commit()
            reply = _rpc(
                client,
                "/mcp",
                "tools/call",
                {"name": "shortlist_get_change", "arguments": {"request": {"change_id": change["change_id"]}}},
            )
            assert "PRIVATE_" not in reply.text
            if stage != "narrowed":
                view = reply.json()["result"]["structuredContent"]["data"]
                assert view["summary"] == {"message": "This change requires owner review."}
                assert "effects" not in view and "requirements" not in view
            else:
                assert reply.json()["result"]["isError"]
        assert "PRIVATE_" not in response.text
