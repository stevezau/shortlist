"""Browser coverage for owner assistant access and legacy grant transitions."""

from __future__ import annotations

import os
import re
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import Browser, Page, expect
from sqlalchemy import select

from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.auth import SESSION_COOKIE, session_serializer
from shortlist.server.db.session import make_engine, make_session_factory
from tests.db_helpers import disposing_engine
from tests.e2e.conftest import OWNER_ACCOUNT_ID, ShortlistApp

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _enable_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHORTLIST_MCP_URL", "http://127.0.0.1/mcp")


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


def _create_grant(app: ShortlistApp, name: str) -> dict:
    response = app.api("POST", "/assistant/grants", json=_grant_payload(name))
    assert response.status_code == 201, response.text
    return response.json()


def _make_legacy(app: ShortlistApp, grant_id: str) -> None:
    """Seed a historical grant shape; all reads and mutations still use the real API."""
    with disposing_engine(make_engine(Path(app.config_dir))) as engine:
        sessions = make_session_factory(engine)
        with sessions() as session:
            row = session.scalar(select(AssistantGrant).where(AssistantGrant.id == grant_id))
            assert row is not None
            constraints = dict(row.constraints)
            constraints["include_future_people"] = False
            row.constraints = constraints
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


def test_owner_assistant_access_approval_and_revoked_removal(browser: Browser, app: ShortlistApp, page: Page) -> None:
    fresh = _create_grant(app, "E2E Fresh Owner")
    legacy = _create_grant(app, "E2E Legacy Owner")
    _make_legacy(app, legacy["id"])
    revoked = _create_grant(app, "E2E Revoked Owner")
    revoke_response = app.api("POST", f"/assistant/grants/{revoked['id']}/revoke")
    assert revoke_response.status_code == 200, revoke_response.text

    listed = app.api("GET", "/assistant/grants").json()
    by_name = {grant["name"]: grant for grant in listed}
    assert by_name[legacy["name"]]["requires_access_approval"] is True
    assert by_name[fresh["name"]]["requires_access_approval"] is False
    assert "person_ids" not in by_name[legacy["name"]]["constraints"]

    _open_assistant_access(page)
    legacy_card = page.get_by_role("article").filter(has_text=legacy["name"])
    expect(legacy_card.get_by_role("button", name="Approve updated access")).to_be_visible()
    legacy_card.get_by_role("button", name="Approve updated access").click()
    expect(page.get_by_role("heading", name=f"Approve updated access for {legacy['name']}")).to_be_visible()
    with page.expect_request(
        lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{legacy['id']}")
    ) as approval_request:
        page.get_by_role("button", name="Confirm updated access").click()
    body = approval_request.value.post_data_json
    assert body == {"expected_revision": legacy["revision"], "approve_updated_access": True}
    expect(page.get_by_role("status")).to_contain_text("Updated access approved")

    approved = app.api("GET", "/assistant/grants").json()
    approved_legacy = next(grant for grant in approved if grant["id"] == legacy["id"])
    assert approved_legacy["requires_access_approval"] is False

    revoked_card = page.get_by_role("article").filter(has_text=revoked["name"])
    revoked_card.get_by_role("button", name="Remove").click()
    dialog = page.get_by_role("dialog")
    expect(dialog).to_contain_text("Remove")
    with page.expect_response(
        lambda response: (
            response.request.method == "DELETE"
            and response.url.endswith(f"/assistant/grants/{revoked['id']}")
            and response.status == 204
        )
    ):
        dialog.get_by_role("button", name=re.compile("Remove connection", re.I)).click()
    expect(page.get_by_role("article").filter(has_text=revoked["name"])).to_have_count(0)

    audit_listing = app.api("GET", "/assistant/grants").json()
    assert all(grant["id"] != revoked["id"] for grant in audit_listing)

    mobile_context = _owner_context(browser, app, width=390)
    try:
        mobile = mobile_context.new_page()
        mobile.set_default_timeout(60_000)
        _open_assistant_access(mobile)
        expect(mobile.locator("body")).to_have_js_property("scrollWidth", 390)
        expect(mobile.get_by_role("heading", name="AI assistants", level=1)).to_be_visible()
        expect(mobile.get_by_role("article").filter(has_text=fresh["name"])).to_be_visible()
        mobile.screenshot(path="/tmp/shortlist-simple-access-proof/browser/assistant-access-mobile.png", full_page=True)
    finally:
        mobile_context.close()


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_owner_creates_simple_access_and_preserves_restricted_edits(
    browser: Browser, app: ShortlistApp, tmp_path: Path, width: int
) -> None:
    restricted_payload = _grant_payload("Restricted connection")
    restricted_payload["constraints"].update(
        row_ids=[1],
        library_keys=["1"],
        include_future_rows=False,
        include_future_libraries=False,
        max_batch_size=7,
        max_work_per_operation=13,
        destination_ids=["https://approved.example"],
    )
    response = app.api("POST", "/assistant/grants", json=restricted_payload)
    assert response.status_code == 201, response.text
    restricted = response.json()
    context = _owner_context(browser, app, width=width)
    try:
        page = context.new_page()
        screenshots = Path(os.environ.get("ASSISTANT_ACCESS_SCREENSHOTS_DIR", str(tmp_path)))
        screenshots.mkdir(parents=True, exist_ok=True)
        _open_assistant_access(page)
        page.get_by_role("button", name="New connection", exact=True).click()
        expect(page.get_by_role("button", name=re.compile("^Manage rows"))).to_be_visible()
        expect(page.get_by_role("checkbox", name="Manage row defaults settings", exact=True)).to_be_visible()
        expect(page.get_by_text("Rows: All current and future rows", exact=False)).to_be_visible()
        expect(page.get_by_label("Paid provider calls", exact=True)).to_have_value("0")
        expect(page.get_by_label(re.compile("^Max batch size"))).to_have_count(0)
        page.get_by_role("button", name="Select all settings groups", exact=True).click()
        settings = page.get_by_role("checkbox", name=re.compile(r"^Manage .* settings$"))
        assert settings.count() > 0
        for checkbox in settings.all():
            expect(checkbox).to_be_checked()
        page.get_by_role("button", name="Clear settings groups selection", exact=True).click()
        for checkbox in settings.all():
            expect(checkbox).not_to_be_checked()
        page.get_by_role("button", name="Advanced access and limits", exact=True).click()
        expect(page.get_by_label(re.compile("^Max batch size"))).to_have_value("25")
        expect(page.get_by_label(re.compile("^Max work per operation"))).to_have_value("")
        expect(page.get_by_label(re.compile("^Connection expires after days"))).to_have_value("90")
        page.get_by_role("button", name="Hide advanced access and limits", exact=True).click()
        expect(page.locator("body")).to_have_js_property("scrollWidth", width)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(screenshots / f"assistant-create-{width}.png"), full_page=True)
        page.get_by_placeholder("Living room Codex").fill("Simple connection")
        with page.expect_request(
            lambda request: request.method == "POST" and request.url.endswith("/assistant/grants")
        ) as creation:
            page.get_by_role("button", name="Create connection", exact=True).click()
        body = creation.value.post_data_json
        assert body["constraints"] == _grant_payload("unused")["constraints"]
        assert body["expires_in_days"] == 90
        expect(page.get_by_role("article").filter(has_text="Simple connection")).to_be_visible()
        saved = next(g for g in app.api("GET", "/assistant/grants").json() if g["name"] == "Simple connection")
        assert saved["constraints"] == body["constraints"]
        assert saved["expires_at"] is not None

        page.get_by_role("article").filter(has_text=restricted["name"]).get_by_role(
            "button", name="Edit resources", exact=True
        ).click()
        expect(page.get_by_role("radio", name="Selected rows", exact=True)).to_be_checked()
        expect(page.get_by_role("radio", name="Selected libraries", exact=True)).to_be_checked()
        expect(page.get_by_label(re.compile("^Max batch size"))).to_have_value("7")
        expect(page.get_by_label(re.compile("^Max work per operation"))).to_have_value("13")
        page.get_by_role("button", name="Select all current libraries", exact=True).click()
        expect(page.get_by_role("checkbox", name="Movies", exact=True)).to_be_checked()
        expect(page.get_by_role("checkbox", name="TV Shows", exact=True)).to_be_checked()
        page.get_by_role("button", name="Clear current libraries selection", exact=True).click()
        expect(page.get_by_role("checkbox", name="Movies", exact=True)).not_to_be_checked()
        expect(page.get_by_role("checkbox", name="TV Shows", exact=True)).not_to_be_checked()
        page.get_by_role("button", name="Select all current libraries", exact=True).click()
        expect(page.locator("body")).to_have_js_property("scrollWidth", width)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(screenshots / f"assistant-edit-{width}.png"), full_page=True)
        with page.expect_request(
            lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{restricted['id']}")
        ) as update:
            page.get_by_role("button", name="Save resources", exact=True).click()
        assert update.value.post_data_json == {
            "expected_revision": restricted["revision"],
            "constraints": {"library_keys": ["1", "2"]},
        }
        expect(page.get_by_role("status")).to_contain_text("Resources saved")
        edited = next(g for g in app.api("GET", "/assistant/grants").json() if g["id"] == restricted["id"])
        assert edited["constraints"] == {**restricted["constraints"], "library_keys": ["1", "2"]}
        for key in ("capabilities", "preset", "client_id", "owner_account_id", "expires_at"):
            assert edited[key] == restricted[key]
    finally:
        context.close()


def test_advanced_validation_cannot_be_hidden_and_next_connection_resets_expiry(page: Page, app: ShortlistApp) -> None:
    _open_assistant_access(page)
    page.get_by_role("button", name="New connection", exact=True).click()
    page.get_by_placeholder("Living room Codex").fill("Short lived connection")
    page.get_by_role("button", name="Advanced access and limits", exact=True).click()
    batch = page.get_by_label("Max batch size", exact=True)
    work = page.get_by_label("Max work per operation", exact=True)
    expiry = page.get_by_label("Connection expires after days", exact=True)
    collapse = page.get_by_role("button", name="Hide advanced access and limits", exact=True)
    for field, invalid, valid in ((batch, "-1", "25"), (work, "1.5", ""), (expiry, "0", "14")):
        field.fill(invalid)
        collapse.click()
        expect(collapse).to_have_attribute("aria-expanded", "true")
        expect(field).to_be_focused()
        assert field.evaluate("element => element.validity.valid") is False
        field.fill(valid)
    collapse.focus()
    collapse.press("Enter")
    expect(page.get_by_role("button", name="Advanced access and limits", exact=True)).to_have_attribute(
        "aria-expanded", "false"
    )
    with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith("/assistant/grants")
    ) as creation:
        page.get_by_role("button", name="Create connection", exact=True).click()
    assert creation.value.post_data_json["expires_in_days"] == 14
    expect(page.get_by_role("article").filter(has_text="Short lived connection")).to_be_visible()
    page.get_by_role("button", name="New connection", exact=True).click()
    page.get_by_role("button", name="Advanced access and limits", exact=True).click()
    expect(page.get_by_label("Connection expires after days", exact=True)).to_have_value("90")
    page.get_by_label("Connection expires after days", exact=True).fill("3")
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="New connection", exact=True).click()
    expect(page.get_by_label("Connection expires after days", exact=True)).to_have_value("3")
    page.get_by_label("Max batch size", exact=True).fill("-1")
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="New connection", exact=True).click()
    expect(page.get_by_label("Max batch size", exact=True)).to_have_value("-1")
    page.get_by_placeholder("Living room Codex").fill("Retained invalid draft")
    page.get_by_role("button", name="Create connection", exact=True).click()
    expect(page.get_by_label("Max batch size", exact=True)).to_be_focused()
    assert all(grant["name"] != "Retained invalid draft" for grant in app.api("GET", "/assistant/grants").json())


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
def test_oauth_consent_keeps_explicit_all_scope_and_zero_paid_default(
    browser: Browser, app: ShortlistApp, width: int
) -> None:
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
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": callback,
            "resource": "http://127.0.0.1/mcp",
            "scope": "instance.read config.read",
            "state": "browser-consent-proof",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    context = _owner_context(browser, app, width=width)
    try:
        page = context.new_page()
        page.route(callback + "*", lambda route: route.fulfill(status=200, body="Authorization received"))
        page.goto(f"/assistant/oauth/authorize?{query}")
        expect(page.get_by_role("button", name="Allow connection", exact=True)).to_be_enabled()
        expect(page.get_by_text("Provider calls stay at zero.", exact=False)).to_be_visible()
        expect(page.locator("body")).to_have_js_property("scrollWidth", width)
        with page.expect_request(
            lambda request: request.method == "POST" and request.url.endswith("/assistant/grants")
        ) as creation:
            page.get_by_role("button", name="Allow connection", exact=True).click()
        assert creation.value.post_data_json["constraints"] == _grant_payload("unused")["constraints"]
        page.wait_for_url(callback + "*")
        callback_values = parse_qs(urlsplit(page.url).query)
        assert callback_values["state"] == ["browser-consent-proof"]
        assert callback_values["code"]
        saved = next(g for g in app.api("GET", "/assistant/grants").json() if g["client_id"] == client_id)
        assert saved["constraints"] == _grant_payload("unused")["constraints"]
        assert set(saved["capabilities"]) == {"instance.read", "config.read"}
    finally:
        context.close()
