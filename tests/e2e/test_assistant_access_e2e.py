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

from shortlist.server.assistant.budgets import AssistantBudget
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


def _reserve_historical_call(app: ShortlistApp, grant_id: str) -> None:
    """Model an earlier consumed/uncertain call without contacting a provider."""
    with disposing_engine(make_engine(Path(app.config_dir))) as engine:
        sessions = make_session_factory(engine)
        with sessions() as session:
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
        expect(page.get_by_role("button", name=re.compile("^Manage Shortlist"))).to_be_visible()
        expect(page.get_by_role("button", name=re.compile("^Suggest changes"))).to_be_visible()
        expect(page.get_by_role("checkbox", name="Manage row defaults settings", exact=True)).to_have_count(0)
        expect(
            page.get_by_text(
                re.compile(
                    r"All current and future people\. All current and future rows; all current and future libraries"
                )
            )
        ).to_be_visible()
        expect(
            page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
        ).not_to_be_checked()
        expect(page.locator("textarea")).to_have_count(0)
        expect(page.get_by_label(re.compile("^Max batch size"))).to_have_count(0)
        page.get_by_role("button", name="Advanced access and limits", exact=True).click()
        status = app.api("GET", "/api/assistant/status").json()
        settings = page.get_by_role("checkbox", name=re.compile(r"^Manage .* settings$"))
        assert settings.count() == len(status["setting_groups"])
        for checkbox in settings.all():
            expect(checkbox).to_be_checked()
        page.get_by_role("button", name="Clear settings groups selection", exact=True).click()
        for checkbox in settings.all():
            expect(checkbox).not_to_be_checked()
        page.get_by_role("button", name="Select all settings groups", exact=True).click()
        for checkbox in settings.all():
            expect(checkbox).to_be_checked()
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
        assert body["preset"] == "owner_automation"
        assert set(body["capabilities"]) == set(status["presets"]["owner_automation"])
        assert body["constraints"] == {
            **_grant_payload("unused")["constraints"],
            "setting_groups": status["setting_groups"],
        }
        assert body["expires_in_days"] == 90
        expect(page.get_by_role("article").filter(has_text="Simple connection")).to_be_visible()
        saved = next(g for g in app.api("GET", "/assistant/grants").json() if g["name"] == "Simple connection")
        assert saved["constraints"] == body["constraints"]
        assert saved["expires_at"] is not None

        page.get_by_role("article").filter(has_text=restricted["name"]).get_by_role(
            "button", name="Edit connection", exact=True
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
        page.get_by_role("button", name="Hide advanced access and limits", exact=True).click()
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(screenshots / f"assistant-edit-{width}.png"), full_page=True)
        with page.expect_request(
            lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{restricted['id']}")
        ) as update:
            page.get_by_role("button", name="Save connection", exact=True).click()
        assert update.value.post_data_json == {
            "expected_revision": restricted["revision"],
            "constraints": {"library_keys": ["1", "2"]},
            "selected_destinations": [],
        }
        expect(page.get_by_role("status")).to_contain_text("Connection saved")
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


def test_editing_custom_access_preserves_spent_calls_until_paid_group_is_changed(page: Page, app: ShortlistApp) -> None:
    status = app.api("GET", "/api/assistant/status").json()
    payload = _grant_payload("Historical paid connection")
    payload["capabilities"] = [*status["presets"]["inspect"], "rows.update", "ai.generate"]
    payload["constraints"].update(
        row_ids=[1, 777],
        library_keys=["1", "retired-library"],
        setting_groups=["system"],
        destination_ids=["https://previously-approved.example"],
        include_future_rows=False,
        include_future_libraries=False,
        max_batch_size=7,
    )
    response = app.api("POST", "/assistant/grants", json=payload)
    assert response.status_code == 201, response.text
    original = response.json()
    _reserve_historical_call(app, original["id"])

    def saved() -> dict:
        return next(grant for grant in app.api("GET", "/assistant/grants").json() if grant["id"] == original["id"])

    _open_assistant_access(page)
    page.get_by_role("article").filter(has_text=original["name"]).get_by_role(
        "button", name="Edit connection", exact=True
    ).click()
    paid = page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
    expect(paid).not_to_be_checked()
    page.get_by_label("Max batch size", exact=True).fill("8")
    with page.expect_request(
        lambda request: request.method == "PATCH" and request.url.endswith(f"/assistant/grants/{original['id']}")
    ) as unrelated:
        page.get_by_role("button", name="Save connection", exact=True).click()
    assert unrelated.value.post_data_json == {
        "expected_revision": original["revision"],
        "constraints": {"max_batch_size": 8},
        "selected_destinations": [],
    }
    expect(page.get_by_role("status")).to_contain_text("Connection saved")
    preserved = saved()
    assert preserved["capabilities"] == original["capabilities"]
    assert preserved["constraints"] == {**original["constraints"], "max_batch_size": 8}
    assert preserved["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 1, "remaining": 0}
    for key in ("preset", "client_id", "owner_account_id", "expires_at"):
        assert preserved[key] == original[key]

    page.get_by_role("article").filter(has_text=original["name"]).get_by_role(
        "button", name="Edit connection", exact=True
    ).click()
    paid.check()
    expect(page.get_by_label("Lifetime call allowance", exact=True)).to_have_value("2")
    _capture_requested_view(page, "assistant-edit-historical-paid.png")
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith(f"/assistant/grants/{original['id']}")
            and response.status == 200
        )
    ):
        page.get_by_role("button", name="Save connection", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Connection saved")
    enabled = saved()
    assert enabled["provider_call_quota"] == {"lifetime_limit": 2, "reserved": 1, "remaining": 1}
    assert enabled["capabilities"] == preserved["capabilities"]
    assert enabled["constraints"] == {**preserved["constraints"], "max_provider_calls": 2}

    page.get_by_role("article").filter(has_text=original["name"]).get_by_role(
        "button", name="Edit connection", exact=True
    ).click()
    paid.uncheck()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and response.url.endswith(f"/assistant/grants/{original['id']}")
            and response.status == 200
        )
    ):
        page.get_by_role("button", name="Save connection", exact=True).click()
    expect(page.get_by_role("status")).to_contain_text("Connection saved")
    disabled = saved()
    assert disabled["provider_call_quota"] == {"lifetime_limit": 0, "reserved": 1, "remaining": 0}
    assert set(disabled["capabilities"]) == set(original["capabilities"]) - {"ai.generate"}
    assert disabled["constraints"] == preserved["constraints"]


def test_named_services_require_review_again_when_the_configured_endpoint_changes(
    page: Page, app: ShortlistApp
) -> None:
    original_url = "http://127.0.0.1:65531"
    changed_url = "http://127.0.0.1:65532"
    configured = app.api("PUT", "/api/settings", json={"values": {"searxng.url": original_url}})
    assert configured.status_code == 200, configured.text
    catalog = app.api("GET", "/assistant/destinations").json()
    original_choice = next(choice for choice in catalog if choice["service_id"] == "searxng")
    assert original_choice["destination_id"] == original_url
    historical_url = "https://previously-approved.example"
    payload = _grant_payload("Historical service approval")
    payload["constraints"]["destination_ids"] = [historical_url]
    historical_response = app.api("POST", "/assistant/grants", json=payload)
    assert historical_response.status_code == 201, historical_response.text
    historical = historical_response.json()

    page.clock.install()
    _open_assistant_access(page)
    page.get_by_role("article").filter(has_text=historical["name"]).get_by_role(
        "button", name="Edit connection", exact=True
    ).click()
    page.get_by_role("button", name="Select all current services", exact=True).click()
    page.get_by_role("button", name="Clear current services", exact=True).click()
    expect(page.get_by_role("button", name="Save connection", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Select all current services", exact=True).click()
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH" and response.url.endswith(f"/assistant/grants/{historical['id']}")
        )
    ) as updated:
        page.get_by_role("button", name="Save connection", exact=True).click()
    assert updated.value.status == 200
    approved_urls = {historical_url, *(choice["destination_id"] for choice in catalog)}
    update_payload = updated.value.request.post_data_json
    assert set(update_payload["constraints"]["destination_ids"]) == approved_urls
    assert {(item["service_id"], item["destination_id"]) for item in update_payload["selected_destinations"]} == {
        (choice["service_id"], choice["destination_id"]) for choice in catalog
    }
    expect(page.get_by_role("status")).to_contain_text("Connection saved")
    page.get_by_role("button", name="New connection", exact=True).click()
    page.get_by_placeholder("Living room Codex").fill("Named service connection")
    service = page.get_by_role("checkbox", name=re.compile(r"^SearXNG search"))
    expect(service).not_to_be_checked()
    expect(page.locator("textarea")).to_have_count(0)
    page.get_by_role("button", name="Select all current services", exact=True).click()
    for choice in catalog:
        expect(page.get_by_role("checkbox", name=re.compile(r"^" + re.escape(choice["label"])))).to_be_checked()
    page.get_by_role("button", name="Clear current services", exact=True).click()
    for choice in catalog:
        expect(page.get_by_role("checkbox", name=re.compile(r"^" + re.escape(choice["label"])))).not_to_be_checked()
    service.check()

    changed = app.api("PUT", "/api/settings", json={"values": {"searxng.url": changed_url}})
    assert changed.status_code == 200, changed.text
    preserved = next(grant for grant in app.api("GET", "/assistant/grants").json() if grant["id"] == historical["id"])
    assert set(preserved["constraints"]["destination_ids"]) == approved_urls
    assert changed_url not in preserved["constraints"]["destination_ids"]
    page.clock.fast_forward(16_000)
    with page.expect_response(lambda response: response.url.endswith("/assistant/destinations")) as refreshed:
        page.evaluate(
            """() => {
                Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});
                document.dispatchEvent(new Event('visibilitychange', {bubbles: true}));
                Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});
                document.dispatchEvent(new Event('visibilitychange', {bubbles: true}));
            }"""
        )
        page.clock.run_for(50)
    assert (
        next(choice for choice in refreshed.value.json() if choice["service_id"] == "searxng")["destination_id"]
        == changed_url
    )
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/assistant/grants")
    ) as rejected:
        page.get_by_role("button", name="Create connection", exact=True).click()
    assert rejected.value.status == 409
    assert all(grant["name"] != "Named service connection" for grant in app.api("GET", "/assistant/grants").json())
    expect(page.get_by_role("alert")).to_contain_text(re.compile(r"changed|reload|review", re.I))

    page.reload()
    page.get_by_role("button", name="New connection", exact=True).click()
    page.get_by_placeholder("Living room Codex").fill("Named service connection")
    page.get_by_role("checkbox", name=re.compile(r"^SearXNG search")).check()
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/assistant/grants")
    ) as created:
        page.get_by_role("button", name="Create connection", exact=True).click()
    assert created.value.status == 201
    body = created.value.request.post_data_json
    assert body["constraints"]["destination_ids"] == [changed_url]
    assert body["selected_destinations"] == [{"service_id": "searxng", "destination_id": changed_url}]
    saved = next(
        grant for grant in app.api("GET", "/assistant/grants").json() if grant["name"] == "Named service connection"
    )
    assert saved["constraints"]["destination_ids"] == [changed_url]
    assert saved["constraints"]["max_provider_calls"] == 0


@pytest.mark.parametrize("width", [1280, 390], ids=["desktop", "mobile"])
@pytest.mark.parametrize(
    ("management", "existing"),
    [(False, False), (True, False), (True, True)],
    ids=["suggest", "manage", "existing-custom"],
)
def test_oauth_consent_applies_only_chosen_requested_access_and_preserves_existing_grants(
    browser: Browser, app: ShortlistApp, width: int, management: bool, existing: bool
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
    requested = {"instance.read", "config.read"}
    expected = set(requested)
    if management:
        requested.update({"rows.update", "ai.generate", "history.export", "requests.send"})
        expected.add("rows.update")
    original = None
    if existing:
        payload = _grant_payload("Existing custom client")
        payload.update(client_id=client_id, capabilities=[*sorted(expected), "history.providers"])
        payload["constraints"].update(
            row_ids=[1],
            library_keys=["1"],
            setting_groups=["system"],
            destination_ids=["https://previously-approved.example"],
            include_future_rows=False,
            include_future_libraries=False,
            max_batch_size=7,
        )
        created = app.api("POST", "/assistant/grants", json=payload)
        assert created.status_code == 201, created.text
        original = created.json()
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
    context = _owner_context(browser, app, width=width)
    try:
        page = context.new_page()
        grant_writes = []
        page.on(
            "request",
            lambda request: (
                grant_writes.append(request)
                if request.method in {"POST", "PATCH"} and "/assistant/grants" in request.url
                else None
            ),
        )
        page.route(callback + "*", lambda route: route.fulfill(status=200, body="Authorization received"))
        page.goto(f"/assistant/oauth/authorize?{query}")
        expect(page.get_by_role("button", name="Allow connection", exact=True)).to_be_enabled()
        if existing:
            expect(page.get_by_role("radio", name=re.compile("^Existing custom client"))).to_be_checked()
            expect(page.get_by_text(re.compile("1 selected rows; 1 selected libraries"))).to_be_visible()
        else:
            expect(page.get_by_role("button", name=re.compile("^Manage Shortlist"))).to_be_visible()
            expect(page.get_by_role("button", name=re.compile("^Suggest changes"))).to_be_visible()
            expect(
                page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
            ).not_to_be_checked()
            if management and width == 1280:
                paid = page.get_by_role("checkbox", name="Allow this assistant to use paid services", exact=True)
                paid.check()
                allowance = page.get_by_label("Lifetime call allowance", exact=True)
                for invalid in ("", "1.5", "101"):
                    allowance.fill(invalid)
                    expect(allowance).to_be_visible()
                    page.get_by_role("button", name="Allow connection", exact=True).click()
                    expect(allowance).to_be_focused()
                    assert allowance.evaluate("element => element.validity.valid") is False
                    assert grant_writes == []
                allowance.fill("2")
                assert allowance.evaluate("element => element.validity.valid") is True
                paid.uncheck()
        expect(page.locator("body")).to_have_js_property("scrollWidth", width)
        mode_name = "existing-custom" if existing else "manage" if management else "suggest"
        _capture_requested_view(page, f"assistant-oauth-{mode_name}-{width}.png")
        page.get_by_role("button", name="Allow connection", exact=True).click()
        page.wait_for_url(callback + "*")
        callback_values = parse_qs(urlsplit(page.url).query)
        assert callback_values["state"] == ["browser-consent-proof"]
        assert callback_values["code"]
        saved = next(g for g in app.api("GET", "/assistant/grants").json() if g["client_id"] == client_id)
        if original:
            assert grant_writes == []
            for key in ("id", "capabilities", "preset", "constraints", "revision", "expires_at"):
                assert saved[key] == original[key]
        else:
            status = app.api("GET", "/api/assistant/status").json()
            expected_constraints = {
                **_grant_payload("unused")["constraints"],
                "setting_groups": status["setting_groups"] if management else [],
            }
            assert len(grant_writes) == 1
            assert grant_writes[0].post_data_json["constraints"] == expected_constraints
            assert set(grant_writes[0].post_data_json["capabilities"]) == expected
            assert saved["constraints"] == expected_constraints
            assert set(saved["capabilities"]) == expected
        token = app.api(
            "POST",
            "/assistant/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "redirect_uri": callback,
                "code": callback_values["code"][0],
                "code_verifier": verifier,
                "resource": "http://127.0.0.1/mcp",
            },
        )
        assert token.status_code == 200, token.text
        assert set(token.json()["scope"].split()) == expected
    finally:
        context.close()
