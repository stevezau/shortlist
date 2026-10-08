"""Two app roles stay compact while real grants and OAuth enforce their authority."""

from __future__ import annotations

import os
import re
from base64 import urlsafe_b64encode
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect
from sqlalchemy import func, select

from shortlist.server.assistant.budgets import AssistantBudget
from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.assistant_auth.destinations import ConfiguredDestination
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.auth import SESSION_COOKIE, session_serializer
from shortlist.server.db.models import Job, Run
from shortlist.server.db.session import make_engine, make_session_factory
from tests.db_helpers import disposing_engine
from tests.e2e.conftest import OWNER_ACCOUNT_ID, ShortlistApp

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _enable_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHORTLIST_MCP_URL", "http://127.0.0.1/mcp")


@pytest.fixture
def nine_services(monkeypatch: pytest.MonkeyPatch) -> list[ConfiguredDestination]:
    """A busy owner catalog must not turn the approval form into a service picker."""
    labels = ["Plex", "TMDB", "AI provider", "Exa search", "SearXNG search", "Radarr", "Sonarr", "Seerr", "Trakt"]
    choices = [
        ConfiguredDestination(
            service_id=f"fixture-{index}",
            label=label,
            purposes=["configured service"],
            destination_id=f"http://127.0.0.1:{61000 + index}",
            host="127.0.0.1",
        )
        for index, label in enumerate(labels)
    ]
    # Only the owner catalog boundary is mocked; forms and grant/OAuth writes stay real.
    monkeypatch.setattr("shortlist.server.assistant_auth.routes.configured_destinations", lambda _session: choices)
    return choices


def _grant_payload(name: str) -> dict:
    return {
        "client_id": f"e2e-{name.lower().replace(' ', '-')}",
        "name": name,
        "preset": "inspect",
        "constraints": {
            "row_ids": [],
            "library_keys": [],
            "setting_groups": [],
            "destination_ids": [],
            "include_future_rows": True,
            "include_future_libraries": True,
            "max_batch_size": 25,
            "max_work_per_operation": None,
            "max_provider_calls": 0,
        },
        "expires_in_days": None,
    }


def _create_grant(app: ShortlistApp, name: str, **overrides) -> dict:
    response = app.api("POST", "/assistant/grants", json={**_grant_payload(name), **overrides})
    assert response.status_code == 201, response.text
    return _saved(app, response.json()["id"])


def _saved(app: ShortlistApp, grant_id: str) -> dict:
    return next(grant for grant in app.api("GET", "/assistant/grants").json() if grant["id"] == grant_id)


def _assert_preserved(actual: dict, original: dict) -> None:
    for key in (
        "id",
        "client_id",
        "owner_account_id",
        "capabilities",
        "preset",
        "constraints",
        "revision",
        "access_role",
        "provider_call_quota",
    ):
        assert actual[key] == original[key], key
    for key in ("expires_at",):
        assert (datetime.fromisoformat(actual[key]) if actual[key] else None) == (
            datetime.fromisoformat(original[key]) if original[key] else None
        )


def _make_legacy(app: ShortlistApp, grant_id: str) -> None:
    """Model the historical all-people approval marker without changing current grants."""
    with disposing_engine(make_engine(Path(app.config_dir))) as engine:
        sessions = make_session_factory(engine)
        with sessions() as session:
            row = session.scalar(select(AssistantGrant).where(AssistantGrant.id == grant_id))
            assert row is not None
            row.constraints = {**row.constraints, "include_future_people": False}
            session.commit()


def _reserve_historical_call(app: ShortlistApp, grant_id: str) -> None:
    """Model consumed/uncertain accounting without calling any provider."""
    with disposing_engine(make_engine(Path(app.config_dir))) as engine, make_session_factory(engine)() as session:
        session.add(AssistantBudget(grant_id=grant_id, provider_calls_reserved=1))
        session.commit()


def _owner_context(browser: Browser, app: ShortlistApp, *, width: int):
    cookie = session_serializer(app.session_secret).dumps({"account_id": OWNER_ACCOUNT_ID, "username": "owner"})
    context = browser.new_context(
        base_url=app.url,
        viewport={"width": width, "height": 900},
        is_mobile=width < 500,
        has_touch=width < 500,
    )
    context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": app.url}])
    return context


def _open_assistant_access(page: Page) -> None:
    page.goto("/settings/system")
    page.get_by_role("link", name="AI assistants").click()
    expect(page).to_have_url(re.compile(r"/assistant-access$"))
    expect(page.get_by_role("heading", name="AI assistants", level=1)).to_be_visible()


def _capture_requested_view(page: Page, filename: str) -> None:
    if directory := os.environ.get("ASSISTANT_ACCESS_SCREENSHOTS_DIR"):
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(target / filename), full_page=True)


VIEW_CAPABILITIES = {
    "instance.read",
    "config.read",
    "catalog.read",
    "people.read",
    "activity.read",
    "history.use",
    "history.export",
    "requests.read",
}


def _assert_compact_approval(page: Page, width: int) -> None:
    expect(page.get_by_role("checkbox")).to_have_count(0)
    expect(page.get_by_role("radio", name=re.compile(r"^View only(?:\s|$)"))).to_be_visible()
    expect(page.get_by_role("radio", name=re.compile(r"^Manage Shortlist(?:\s|$)"))).to_be_visible()
    expect(page.get_by_role("button", name=re.compile("Advanced|Select all|Paid access"))).to_have_count(0)
    expect(page.locator("textarea")).to_have_count(0)
    expect(page.get_by_role("spinbutton")).to_have_count(0)
    expect(
        page.get_by_label(re.compile("Max batch|Max work|Connection expires|Lifetime call allowance"))
    ).to_have_count(0)
    expect(page.locator("body")).to_have_js_property("scrollWidth", width)


def _mcp_request(app: ShortlistApp, token: str, method: str, params: dict) -> dict:
    """Use only the issued bearer, with the fixture's canonical MCP Host."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Host": "127.0.0.1",
    }
    with httpx.Client(base_url=app.url, headers=headers, timeout=30) as client:
        initialized = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "browser-proof", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
        )
        assert response.status_code == 200
        return response.json()


def _mcp_call(app: ShortlistApp, token: str, name: str, arguments: dict) -> dict:
    response = _mcp_request(app, token, "tools/call", {"name": name, "arguments": arguments})
    assert "error" not in response, response
    return response["result"]


def _mcp_capabilities(app: ShortlistApp, token: str) -> set[str]:
    result = _mcp_call(app, token, "shortlist_get_instance", {})
    assert result.get("isError") is not True
    return set(result["structuredContent"]["data"]["capabilities"])


def _work_counts(app: ShortlistApp) -> tuple[int, ...]:
    with disposing_engine(make_engine(Path(app.config_dir))) as engine, make_session_factory(engine)() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (
                AssistantChange,
                AssistantOperation,
                Job,
                Run,
            )
        )


def _assert_read_only_bearer(app: ShortlistApp, token: str) -> None:
    before = _work_counts(app)
    read = _mcp_call(app, token, "shortlist_list_rows", {"request": {}})
    assert read.get("isError") is not True
    for name, request in (
        ("shortlist_plan_row", {"action": "update", "row_id": 1, "values": {"enabled": False}}),
        ("shortlist_plan_run", {"row_ids": [1], "person_ids": [1], "dry_run": False}),
        ("shortlist_preview_row", {"row_id": 1, "person_ids": [1]}),
    ):
        result = _mcp_call(app, token, name, {"request": request})
        assert result.get("isError") is True, (name, result)
        # A real role rejection must not be mistaken for a malformed-input pass.
        text = str(result).lower()
        assert any(word in text for word in ("permission", "access role", "read-only", "view only")), result
    assert _work_counts(app) == before


def _assert_standalone_generation_removed(app: ShortlistApp, token: str) -> None:
    inventory = _mcp_request(app, token, "tools/list", {})
    assert "error" not in inventory
    assert "shortlist_generate_theme" not in {tool["name"] for tool in inventory["result"]["tools"]}
    before = _work_counts(app)
    response = _mcp_request(
        app,
        token,
        "tools/call",
        {
            "name": "shortlist_generate_theme",
            "arguments": {
                "request": {
                    "action": "prepare",
                    "definition": {
                        "brief": "A public uplifting film night",
                        "media": "movie",
                        "max_output_tokens": 256,
                    },
                }
            },
        },
    )
    assert "error" in response or response["result"].get("isError") is True
    assert _work_counts(app) == before


def _change_role(page: Page, app: ShortlistApp, original: dict, role: str) -> dict:
    card = page.get_by_role("article").filter(has_text=original["name"])
    card.get_by_role("button", name="Change access", exact=True).click()
    form = page.locator("form").filter(
        has=page.get_by_role("heading", name=f"Change access for {original['name']}", exact=True)
    )
    form.get_by_role(
        "radio", name=re.compile(r"^View only(?:\s|$)" if role == "view" else r"^Manage Shortlist(?:\s|$)")
    ).check()
    with page.expect_request(
        lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{original['id']}")
    ) as update:
        form.get_by_role("button", name="Save access", exact=True).click()
    assert update.value.post_data_json == {"expected_revision": original["revision"], "access_role": role}
    expect(page.get_by_role("status")).to_contain_text("Connection updated")
    return _saved(app, original["id"])


def test_owner_explicitly_changes_legacy_access_and_removes_revoked_connection(app: ShortlistApp, page: Page) -> None:
    legacy = _create_grant(app, "Legacy owner")
    _make_legacy(app, legacy["id"])
    legacy = _saved(app, legacy["id"])
    revoked = _create_grant(app, "Disconnected owner")
    assert app.api("POST", f"/assistant/grants/{revoked['id']}/revoke").status_code == 200
    _open_assistant_access(page)
    card = page.get_by_role("article").filter(has_text=legacy["name"])
    expect(card.get_by_text("Existing access", exact=False)).to_be_visible()
    _assert_preserved(_saved(app, legacy["id"]), legacy)
    managed = _change_role(page, app, legacy, "manage")
    assert managed["access_role"] == "manage"
    assert managed["requires_access_approval"] is False
    assert managed["constraints"]["max_provider_calls"] == 0
    assert "ai.generate" not in managed["capabilities"]
    page.get_by_role("article").filter(has_text=revoked["name"]).get_by_role(
        "button", name="Remove", exact=True
    ).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "DELETE" and response.url.endswith(f"/assistant/grants/{revoked['id']}")
        )
    ) as removed:
        page.get_by_role("dialog").get_by_role("button", name="Remove connection", exact=True).click()
    assert removed.value.status == 204
    expect(page.get_by_role("article").filter(has_text=revoked["name"])).to_have_count(0)
    assert all(grant["id"] != revoked["id"] for grant in app.api("GET", "/assistant/grants").json())


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("role", ["view", "manage"])
def test_compact_roles_create_and_change_only_the_explicitly_selected_connection(
    browser: Browser, app: ShortlistApp, nine_services: list[ConfiguredDestination], width: int, role: str
) -> None:
    assert len(app.api("GET", "/assistant/destinations").json()) == len(nine_services) == 9
    constraints = {
        **_grant_payload("unused")["constraints"],
        "row_ids": [1],
        "library_keys": ["1"],
        "include_future_rows": False,
        "include_future_libraries": False,
        "max_batch_size": 7,
        "max_work_per_operation": 13,
        "destination_ids": ["https://approved.example"],
    }
    restricted = _create_grant(app, "Restricted connection", constraints=constraints, expires_in_days=14)
    with _owner_context(browser, app, width=width) as context:
        page = context.new_page()
        _open_assistant_access(page)
        _assert_preserved(_saved(app, restricted["id"]), restricted)
        page.get_by_role("button", name="New connection", exact=True).click()
        _assert_compact_approval(page, width)
        expect(page.get_by_role("radio", name=re.compile(r"^Manage Shortlist(?:\s|$)"))).to_be_checked()
        page.get_by_role(
            "radio", name=re.compile(r"^View only(?:\s|$)" if role == "view" else r"^Manage Shortlist(?:\s|$)")
        ).check()
        if role == "manage":
            expect(
                page.get_by_text(re.compile(r"Runs may incur provider charges under your Shortlist settings\."))
            ).to_be_visible()
        for service in nine_services:
            expect(page.get_by_role("checkbox", name=service.label, exact=True)).to_have_count(0)
        page.get_by_label("Connection name", exact=True).fill("Simple connection")
        _capture_requested_view(page, f"assistant-create-{role}-{width}.png")
        button = page.get_by_role("button", name="Connect", exact=True)
        box = button.bounding_box()
        assert box and box["y"] + box["height"] <= 900
        with page.expect_request(
            lambda request: request.method == "POST" and request.url.endswith("/assistant/grants")
        ) as creation:
            button.click()
        body = creation.value.post_data_json
        assert body["access_role"] == role
        assert (
            not {"capabilities", "constraints", "paid_enabled", "max_provider_calls", "expires_in_days"} & body.keys()
        )
        expect(page.get_by_role("article").filter(has_text="Simple connection")).to_be_visible()
        saved = next(
            grant for grant in app.api("GET", "/assistant/grants").json() if grant["name"] == "Simple connection"
        )
        assert saved["access_role"] == role
        assert saved["constraints"]["include_future_rows"] and saved["constraints"]["include_future_libraries"]
        assert saved["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 0, "remaining": 0}
        assert {"ai.generate", "secrets.read", "grants.manage"}.isdisjoint(saved["capabilities"])
        if role == "view":
            assert set(saved["capabilities"]) == VIEW_CAPABILITIES
        else:
            assert {"history.providers", "requests.send", "maintenance.execute", "runs.execute", "rows.update"} <= set(
                saved["capabilities"]
            )
        card = page.get_by_role("article").filter(has_text=restricted["name"])
        expect(card.get_by_text("Existing access", exact=False)).to_be_visible()
        card.get_by_role("button", name="Change access", exact=True).click()
        _assert_compact_approval(page, width)
        form = page.locator("form").filter(
            has=page.get_by_role("heading", name=f"Change access for {restricted['name']}", exact=True)
        )
        expect(form).to_have_count(1)
        other_card = page.get_by_role("article").filter(has_text="Simple connection")
        expect(other_card.locator("form")).to_have_count(0)
        expect(other_card).not_to_contain_text(restricted["name"])
        if role == "manage":
            _capture_requested_view(page, f"assistant-change-{role}-{width}.png")
        form.get_by_role("button", name="Cancel", exact=True).click()
        _assert_preserved(_saved(app, restricted["id"]), restricted)
        card.get_by_role("button", name="New local credential", exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_role("button", name="I saved it", exact=True).click()
        _assert_preserved(_saved(app, restricted["id"]), restricted)
        changed = _change_role(page, app, restricted, role)
        assert changed["revision"] == restricted["revision"] + 1
        assert changed["access_role"] == role
        assert changed["provider_call_quota"] == restricted["provider_call_quota"]
        for key in ("id", "client_id", "owner_account_id"):
            assert changed[key] == restricted[key]
        assert datetime.fromisoformat(changed["expires_at"]) == datetime.fromisoformat(restricted["expires_at"])
        _assert_preserved(_saved(app, saved["id"]), saved)


@pytest.mark.parametrize("limit,had_ai", [(0, True), (3, False)])
def test_role_changes_preserve_historical_accounting_without_paid_controls(
    page: Page, app: ShortlistApp, limit: int, had_ai: bool
) -> None:
    original = _create_grant(
        app,
        "Historical connection",
        capabilities=["instance.read", *(["ai.generate"] if had_ai else [])],
        constraints={**_grant_payload("unused")["constraints"], "max_provider_calls": limit},
    )
    _reserve_historical_call(app, original["id"])
    original = _saved(app, original["id"])
    _open_assistant_access(page)
    _assert_preserved(_saved(app, original["id"]), original)
    for role in ("view", "manage"):
        changed = _change_role(page, app, original, role)
        assert changed["provider_call_quota"] == {
            "lifetime_limit": limit,
            "reserved": 1,
            "remaining": max(0, limit - 1),
        }
        assert "ai.generate" not in changed["capabilities"]
        assert changed["access_role"] == role
        assert changed["revision"] == original["revision"] + 1
        expect(page.get_by_role("checkbox")).to_have_count(0)
        expect(page.get_by_role("spinbutton")).to_have_count(0)
        original = changed


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("mode", ["view", "manage", "readonly-request", "partial-manage", "existing-custom"])
def test_oauth_roles_enforce_real_bearer_authority_and_keep_existing_grants(
    browser: Browser, app: ShortlistApp, nine_services: list[ConfiguredDestination], width: int, mode: str
) -> None:
    assert len(app.api("GET", "/assistant/destinations").json()) == len(nine_services) == 9
    callback = "http://127.0.0.1:9999/callback"
    registration = app.api(
        "POST",
        "/assistant/oauth/register",
        json={
            "client_name": "Browser consent",
            "redirect_uris": [callback],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "application_type": "native",
        },
    )
    assert registration.status_code == 201, registration.text
    client_id = registration.json()["client_id"]
    verifier = "browser-consent-proof-" + "x" * 43
    challenge = urlsafe_b64encode(sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    advertised = app.api("GET", "/.well-known/oauth-authorization-server")
    assert advertised.status_code == 200
    requested = set(advertised.json()["scopes_supported"])
    assert {"rows.update", "runs.execute", "history.export", "requests.send"} <= requested
    role = "view" if mode in {"view", "readonly-request"} else "manage"
    expected = VIEW_CAPABILITIES if role == "view" else requested - {"ai.generate", "secrets.read", "grants.manage"}
    if mode == "readonly-request":
        requested = {"instance.read", "config.read"}
        expected = requested
    if mode == "partial-manage":
        requested = {"instance.read", "config.read", "rows.update"}
        expected = requested
    original = None
    if mode == "existing-custom":
        requested = {"instance.read", "config.read", "rows.update", "history.export", "requests.send"}
        expected = {"instance.read", "config.read", "rows.update"}
        original = _create_grant(
            app,
            "Existing custom client",
            client_id=client_id,
            capabilities=sorted(expected | {"history.providers"}),
            constraints={
                **_grant_payload("unused")["constraints"],
                "row_ids": [1],
                "library_keys": ["1"],
                "setting_groups": ["system"],
                "destination_ids": ["https://previously-approved.example"],
                "include_future_rows": False,
                "include_future_libraries": False,
                "max_batch_size": 7,
            },
        )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": callback,
            "resource": "http://127.0.0.1/mcp",
            "scope": " ".join(sorted(requested)),
            "state": "browser-consent-proof",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    with _owner_context(browser, app, width=width) as context:
        page = context.new_page()
        writes = []
        page.on(
            "request",
            lambda request: (
                writes.append(request)
                if request.method in {"POST", "PATCH"} and "/assistant/grants" in request.url
                else None
            ),
        )
        page.route(callback + "*", lambda route: route.fulfill(status=200, body="Authorization received"))
        page.goto(f"/assistant/oauth/authorize?{query}")
        expect(page.get_by_role("button", name="Allow connection", exact=True)).to_be_enabled()
        if original:
            expect(page.get_by_role("radio", name=re.compile("^Existing custom client"))).to_be_checked()
            expect(page.get_by_role("checkbox")).to_have_count(0)
        else:
            _assert_compact_approval(page, width)
            page.get_by_role(
                "radio", name=re.compile(r"^View only(?:\s|$)" if role == "view" else r"^Manage Shortlist(?:\s|$)")
            ).check()
        if (mode, width) in {("view", 390), ("manage", 1280)}:
            _capture_requested_view(page, f"assistant-oauth-{mode}-{width}.png")
        page.get_by_role("button", name="Allow connection", exact=True).click()
        page.wait_for_url(callback + "*")
        values = parse_qs(urlsplit(page.url).query)
        assert values["state"] == ["browser-consent-proof"]
        saved = next(grant for grant in app.api("GET", "/assistant/grants").json() if grant["client_id"] == client_id)
        if original:
            assert writes == []
            _assert_preserved(saved, original)
        else:
            assert len(writes) == 1
            assert writes[0].post_data_json["access_role"] == role
            assert set(writes[0].post_data_json["capabilities"]) == requested
            assert not {"constraints", "paid_enabled", "max_provider_calls"} & writes[0].post_data_json.keys()
            assert saved["access_role"] == role
            assert set(saved["capabilities"]) == expected
            assert saved["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 0, "remaining": 0}
        token = app.api(
            "POST",
            "/assistant/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": callback,
                "resource": "http://127.0.0.1/mcp",
                "code": values["code"][0],
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        assert set(token.json()["scope"].split()) == expected
        credential = token.json()["access_token"]
        assert _mcp_capabilities(app, credential) == expected
        if role == "view":
            _assert_read_only_bearer(app, credential)
        _assert_standalone_generation_removed(app, credential)
        if original or role == "view" or mode == "partial-manage":
            _open_assistant_access(page)
            updated = _change_role(page, app, saved, "manage")
            assert "requests.send" in updated["capabilities"]
            assert updated["provider_call_quota"] == saved["provider_call_quota"]
            assert updated["expires_at"] == saved["expires_at"]
            assert _mcp_capabilities(app, credential) == expected
            if role == "view":
                _assert_read_only_bearer(app, credential)
