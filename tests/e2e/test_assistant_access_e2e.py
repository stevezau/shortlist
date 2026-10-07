"""Browser coverage for owner assistant access and legacy grant transitions."""

from __future__ import annotations

import re
from pathlib import Path

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
