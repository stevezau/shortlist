"""Owner approval stays compact while real grant, OAuth and paid boundaries hold."""

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
from sqlalchemy import select

from shortlist.server.assistant.budgets import AssistantBudget
from shortlist.server.assistant_auth.destinations import ConfiguredDestination
from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.auth import SESSION_COOKIE, session_serializer
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
    return response.json()


def _saved(app: ShortlistApp, grant_id: str) -> dict:
    return next(grant for grant in app.api("GET", "/assistant/grants").json() if grant["id"] == grant_id)


def _assert_preserved(actual: dict, original: dict) -> None:
    for key in ("id", "client_id", "owner_account_id", "capabilities", "preset", "constraints", "revision"):
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


def _assert_compact_approval(page: Page, width: int, *, paid_available: bool = True) -> None:
    expect(page.get_by_role("checkbox")).to_have_count(1 if paid_available else 0)
    expect(
        page.get_by_role("button", name=re.compile("Advanced|Manage Shortlist|Suggest changes|Select all"))
    ).to_have_count(0)
    expect(page.get_by_role("textbox")).to_have_count(
        1 if page.get_by_label("Connection name", exact=True).count() else 0
    )
    expect(page.locator("textarea")).to_have_count(0)
    expect(page.get_by_label(re.compile("Max batch|Max work|Connection expires"))).to_have_count(0)
    expect(page.get_by_text("Passwords and API keys stay in Shortlist.", exact=False)).to_be_visible()
    expect(page.locator("body")).to_have_js_property("scrollWidth", width)


def _mcp_capabilities(app: ShortlistApp, token: str) -> set[str]:
    """Use only the bearer, without the fixture's browser cookie, for actual MCP authority."""
    # The fixture publishes the canonical MCP URL without the internal uvicorn port.
    # Match that Host when connecting to its ephemeral local listener, as a proxy does.
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
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "shortlist_get_instance", "arguments": {}},
            },
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result.get("isError") is not True
        return set(result["structuredContent"]["data"]["capabilities"])


def test_owner_explicitly_upgrades_legacy_access_and_removes_revoked_connection(app: ShortlistApp, page: Page) -> None:
    legacy = _create_grant(app, "Legacy owner")
    _make_legacy(app, legacy["id"])
    revoked = _create_grant(app, "Disconnected owner")
    assert app.api("POST", f"/assistant/grants/{revoked['id']}/revoke").status_code == 200
    _open_assistant_access(page)
    card = page.get_by_role("article").filter(has_text=legacy["name"])
    expect(card.get_by_text("Approval required", exact=True)).to_be_visible()
    card.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
    with page.expect_request(
        lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{legacy['id']}")
    ) as approval:
        page.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
    assert approval.value.post_data_json == {"expected_revision": legacy["revision"], "upgrade_owner_managed": True}
    expect(page.get_by_role("status")).to_contain_text("Connection updated")
    upgraded = _saved(app, legacy["id"])
    assert upgraded["constraints"]["owner_managed"] is True
    assert upgraded["requires_access_approval"] is False
    assert upgraded["constraints"]["max_provider_calls"] == 0
    assert "ai.generate" not in upgraded["capabilities"]
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
def test_compact_owner_consent_creates_full_access_and_requires_explicit_upgrade(
    browser: Browser, app: ShortlistApp, nine_services: list[ConfiguredDestination], width: int
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
        paid = page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
        expect(paid).not_to_be_checked()
        expect(page.get_by_label("Lifetime call allowance", exact=True)).to_have_count(0)
        for service in nine_services:
            expect(page.get_by_role("checkbox", name=service.label, exact=True)).to_have_count(0)
        page.get_by_label("Connection name", exact=True).fill("Simple connection")
        _capture_requested_view(page, f"assistant-create-{width}.png")
        button = page.get_by_role("button", name="Connect", exact=True)
        assert button.bounding_box()["y"] + button.bounding_box()["height"] <= 900
        with page.expect_request(
            lambda request: request.method == "POST" and request.url.endswith("/assistant/grants")
        ) as creation:
            button.click()
        body = creation.value.post_data_json
        assert body["owner_managed"] is True
        assert body["preset"] == "owner_automation"
        assert body["constraints"] == {"max_provider_calls": 0}
        assert "capabilities" not in body and "expires_in_days" not in body
        expect(page.get_by_role("article").filter(has_text="Simple connection")).to_be_visible()
        saved = next(
            grant for grant in app.api("GET", "/assistant/grants").json() if grant["name"] == "Simple connection"
        )
        assert saved["constraints"]["owner_managed"] is True
        assert saved["constraints"]["include_future_rows"] and saved["constraints"]["include_future_libraries"]
        assert saved["constraints"]["max_batch_size"] is None and saved["constraints"]["max_work_per_operation"] is None
        assert saved["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 0, "remaining": 0}
        assert {"history.export", "history.providers", "requests.send", "maintenance.execute"} <= set(
            saved["capabilities"]
        )
        assert {"ai.generate", "secrets.read", "grants.manage"}.isdisjoint(saved["capabilities"])
        assert saved["expires_at"] is not None

        card = page.get_by_role("article").filter(has_text=restricted["name"])
        card.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
        _assert_compact_approval(page, width)
        form = page.locator("form").filter(
            has=page.get_by_role("heading", name=f"Upgrade {restricted['name']} to full Shortlist access", exact=True)
        )
        expect(form).to_have_count(1)
        other_card = page.get_by_role("article").filter(has_text="Simple connection")
        expect(other_card).to_have_count(1)
        expect(other_card.locator("form")).to_have_count(0)
        expect(form).not_to_contain_text("Simple connection")
        expect(other_card).not_to_contain_text(restricted["name"])
        _capture_requested_view(page, f"assistant-upgrade-{width}.png")
        page.get_by_role("button", name="Cancel", exact=True).click()
        _assert_preserved(_saved(app, restricted["id"]), restricted)
        card.get_by_role("button", name="New local credential", exact=True).click()
        expect(page.get_by_role("dialog")).to_be_visible()
        page.get_by_role("button", name="I saved it", exact=True).click()
        _assert_preserved(_saved(app, restricted["id"]), restricted)
        card.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
        with page.expect_request(
            lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{restricted['id']}")
        ) as upgrade:
            page.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
        assert upgrade.value.post_data_json == {
            "expected_revision": restricted["revision"],
            "upgrade_owner_managed": True,
        }
        expect(page.get_by_role("status")).to_contain_text("Connection updated")
        upgraded = _saved(app, restricted["id"])
        assert upgraded["revision"] == restricted["revision"] + 1
        assert upgraded["constraints"]["owner_managed"] is True
        assert upgraded["constraints"]["include_future_rows"] and upgraded["constraints"]["include_future_libraries"]
        assert upgraded["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 0, "remaining": 0}
        for key in ("id", "client_id", "owner_account_id", "expires_at"):
            assert upgraded[key] == restricted[key]
        _assert_preserved(_saved(app, saved["id"]), saved)


def test_paid_validation_and_explicit_upgrade_preserve_historical_usage(page: Page, app: ShortlistApp) -> None:
    original = _create_grant(app, "Historical paid connection", capabilities=["instance.read", "ai.generate"])
    _reserve_historical_call(app, original["id"])
    _open_assistant_access(page)
    card = page.get_by_role("article").filter(has_text=original["name"])
    card.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
    paid = page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
    expect(paid).not_to_be_checked()
    expect(page.get_by_text("Used or uncertain: 1 · Remaining: 0", exact=True)).to_be_visible()
    page.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Connection updated")
    upgraded = _saved(app, original["id"])
    assert "ai.generate" in upgraded["capabilities"]
    assert upgraded["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 1, "remaining": 0}
    card.get_by_role("button", name="Paid access", exact=True).click()
    paid.check()
    allowance = page.get_by_label("Lifetime call allowance", exact=True)
    expect(allowance).to_have_value("2")
    for invalid in ("", "1", "1.5", "101"):
        allowance.fill(invalid)
        expect(allowance).to_be_visible()
        page.get_by_role("button", name="Save paid access", exact=True).click()
        expect(allowance).to_be_focused()
        assert allowance.evaluate("element => element.validity.valid") is False
        _assert_preserved(_saved(app, original["id"]), upgraded)
    allowance.fill("2")
    page.get_by_role("button", name="Save paid access", exact=True).click()
    expect(card.get_by_role("button", name="Paid access", exact=True)).to_be_visible()
    enabled = _saved(app, original["id"])
    assert enabled["provider_call_quota"] == {"lifetime_limit": 2, "reserved": 1, "remaining": 1}
    assert enabled["capabilities"] == upgraded["capabilities"]
    card.get_by_role("button", name="Paid access", exact=True).click()
    paid.uncheck()
    page.get_by_role("button", name="Save paid access", exact=True).click()
    expect(card.get_by_role("button", name="Paid access", exact=True)).to_be_visible()
    disabled = _saved(app, original["id"])
    assert disabled["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 1, "remaining": 0}
    assert set(disabled["capabilities"]) == set(upgraded["capabilities"]) - {"ai.generate"}


def test_legacy_allowance_without_ai_permission_stays_off_through_upgrade(page: Page, app: ShortlistApp) -> None:
    original = _create_grant(
        app,
        "Legacy unused allowance",
        capabilities=["instance.read"],
        constraints={**_grant_payload("unused")["constraints"], "max_provider_calls": 3},
    )
    _reserve_historical_call(app, original["id"])
    _open_assistant_access(page)
    card = page.get_by_role("article").filter(has_text=original["name"])
    expect(card.get_by_text("0 paid calls available", exact=False)).to_be_visible()
    card.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
    expect(
        page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
    ).not_to_be_checked()
    page.get_by_role("button", name="Cancel", exact=True).click()
    _assert_preserved(_saved(app, original["id"]), original)
    card.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
    page.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Connection updated")
    updated = _saved(app, original["id"])
    assert "ai.generate" not in updated["capabilities"]
    assert updated["provider_call_quota"] == {"lifetime_limit": 3, "reserved": 1, "remaining": 2}
    expect(card.get_by_text("0 paid calls available", exact=False)).to_be_visible()


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize("mode", ["limited", "full", "existing-custom"])
def test_oauth_consent_keeps_actual_bearer_scopes_and_existing_grants(
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
    requested = {"instance.read", "config.read"}
    if mode == "full":
        advertised = app.api("GET", "/.well-known/oauth-authorization-server")
        assert advertised.status_code == 200, advertised.text
        requested = set(advertised.json()["scopes_supported"])
        assert {"rows.update", "ai.generate", "history.export", "requests.send"} <= requested
    elif mode == "existing-custom":
        requested.update({"rows.update", "ai.generate", "history.export", "requests.send"})
    expected = requested - {"ai.generate"}
    original = None
    if mode == "existing-custom":
        expected = {"instance.read", "config.read", "rows.update"}
        constraints = {
            **_grant_payload("unused")["constraints"],
            "row_ids": [1],
            "library_keys": ["1"],
            "setting_groups": ["system"],
            "destination_ids": ["https://previously-approved.example"],
            "include_future_rows": False,
            "include_future_libraries": False,
            "max_batch_size": 7,
        }
        original = _create_grant(
            app,
            "Existing custom client",
            client_id=client_id,
            capabilities=sorted(expected | {"history.providers"}),
            constraints=constraints,
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
            expect(page.get_by_text("Your saved connection keeps its existing access.", exact=False)).to_be_visible()
            expect(
                page.get_by_text("Approving this sign-in does not upgrade the connection.", exact=False)
            ).to_be_visible()
            expect(page.get_by_role("checkbox")).to_have_count(0)
        else:
            _assert_compact_approval(page, width, paid_available=mode != "limited")
            paid = page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
            if mode == "limited":
                expect(paid).to_have_count(0)
                expect(page.get_by_text("It cannot change your setup.", exact=False)).to_be_visible()
            else:
                expect(paid).not_to_be_checked()
                expect(page.get_by_text("It cannot gain more through this approval.", exact=False)).to_have_count(0)
                expect(page.get_by_text("It cannot change your setup.", exact=False)).to_have_count(0)
            if mode == "full" and width == 1280:
                paid.check()
                allowance = page.get_by_label("Lifetime call allowance", exact=True)
                for invalid in ("", "1.5", "101"):
                    allowance.fill(invalid)
                    page.get_by_role("button", name="Allow connection", exact=True).click()
                    expect(allowance).to_be_focused()
                    assert allowance.evaluate("element => element.validity.valid") is False
                    assert writes == []
                paid.uncheck()
        _capture_requested_view(page, f"assistant-oauth-{mode}-{width}.png")
        page.get_by_role("button", name="Allow connection", exact=True).click()
        page.wait_for_url(callback + "*")
        callback_values = parse_qs(urlsplit(page.url).query)
        assert callback_values["state"] == ["browser-consent-proof"]
        saved = next(grant for grant in app.api("GET", "/assistant/grants").json() if grant["client_id"] == client_id)
        if original:
            assert writes == []
            _assert_preserved(saved, original)
        else:
            assert len(writes) == 1
            assert writes[0].post_data_json["owner_managed"] is True
            assert set(writes[0].post_data_json["capabilities"]) == requested
            assert writes[0].post_data_json["constraints"] == {"max_provider_calls": 0}
            assert saved["constraints"]["owner_managed"] is True
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
                "code": callback_values["code"][0],
                "code_verifier": verifier,
            },
        )
        assert token.status_code == 200, token.text
        assert set(token.json()["scope"].split()) == expected
        credential = token.json()["access_token"]
        assert _mcp_capabilities(app, credential) == expected
        if original or mode == "limited":
            _open_assistant_access(page)
            card = page.get_by_role("article").filter(has_text=saved["name"])
            expect(card.get_by_text("Existing limited access", exact=False)).to_be_visible()
            card.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
            with page.expect_request(
                lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{saved['id']}")
            ) as upgrade:
                page.get_by_role("button", name="Upgrade to full Shortlist access", exact=True).click()
            assert upgrade.value.post_data_json == {
                "expected_revision": saved["revision"],
                "upgrade_owner_managed": True,
            }
            expect(page.get_by_role("status")).to_contain_text("Connection updated")
            updated = _saved(app, saved["id"])
            assert "requests.send" in updated["capabilities"]
            assert updated["provider_call_quota"] == saved["provider_call_quota"]
            assert updated["expires_at"] == saved["expires_at"]
            assert _mcp_capabilities(app, credential) == expected
