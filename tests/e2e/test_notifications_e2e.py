"""E2E: the webhook, end to end — the real app, a real run against the fake Plex, and a receiver on loopback.

Unit tests prove each rule; this proves the wiring that only a running app has: the settings PUT and its
validation, the encrypted auth header reaching the wire, the run and the job queue queuing their events,
the queue draining them, and every message that leaves naming nobody.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

AUTH_NAME, AUTH_VALUE = "X-Test-Key", "k3y-for-the-receiver"


@pytest.fixture
def receiver() -> Iterator[tuple[str, list[dict]]]:
    """A webhook receiver on loopback that records each POST's headers and JSON body."""
    received: list[dict] = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/hook", received
    finally:
        server.shutdown()


def _events(received: list[dict]) -> list[str]:
    return [r["body"]["event"] for r in received]


def _wait_for(received: list[dict], wanted: set[str], timeout_s: float = 90) -> None:
    """The queue drains at the end of each run and job, and on the worker's 60-second tick."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if wanted <= set(_events(received)):
            return
        time.sleep(0.2)
    raise AssertionError(f"never received {sorted(wanted - set(_events(received)))}; got {_events(received)}")


def _configure(app: ShortlistApp, url: str, events: list[str]) -> None:
    resp = app.api(
        "PUT",
        "/api/settings",
        json={
            "values": {
                "notify.webhook.enabled": True,
                "notify.webhook.url": url,
                "notify.webhook.events": events,
                "notify.webhook.auth_header_name": AUTH_NAME,
                "notify.webhook.auth_header_value": AUTH_VALUE,
            }
        },
    )
    assert resp.status_code == 200, resp.text


class TestTheWebhookEndToEnd:
    def test_a_run_and_a_job_reach_the_receiver_with_the_auth_header_and_no_names(self, app: ShortlistApp, receiver):
        url, received = receiver
        _configure(app, url, ["run.started", "run.finished", "run.partial", "job.started", "job.finished"])
        assert app.api("GET", "/api/settings").json()["notify.webhook.auth_header_value"] == "•••••"
        refused = app.api("PUT", "/api/settings", json={"values": {"notify.webhook.auth_header_value": "key\t"}})
        assert refused.status_code == 422

        test = app.api("POST", "/api/settings/test/notify").json()
        assert test["ok"], test
        _wait_for(received, {"test"})

        job = app.api("POST", "/api/system/jobs", json={"kind": "backup.take", "payload": {}}).json()
        assert job["status"] == "done", job
        created = app.api("POST", "/api/runs", json={"dry_run": False}).json()
        run = app.wait_for_run(created["run_id"])
        assert run["status"] == "ok", run

        _wait_for(received, {"job.started", "job.finished", "run.started"})
        _wait_for(received, {"run.finished"} if not run["stats"].get("users_error") else {"run.partial"})

        usernames = {u["username"].lower() for u in app.api("GET", "/api/users").json()}
        assert usernames, "the fake server has people, so the no-names check has teeth"
        for message in received:
            assert message["headers"][AUTH_NAME.lower()] == AUTH_VALUE
            body = message["body"]
            assert body["content"] == body["text"] == f"{body['title']}\n{body['message']}"
            said = f"{body['title']} {body['message']}".lower()
            assert not [name for name in usernames if name in said], body

    def test_a_dry_run_sends_nothing(self, app: ShortlistApp, receiver):
        url, received = receiver
        _configure(app, url, ["run.started", "run.finished", "run.partial", "run.failed"])
        created = app.api("POST", "/api/runs", json={"dry_run": True}).json()
        app.wait_for_run(created["run_id"])
        # Nothing is even queued: a dry run never reaches `notify.send`, so the queue is the evidence
        # rather than a pause to see whether something arrives. `run.started` is queued before the run
        # begins and `run.finished` after it persists; neither exists for a dry run.
        queued = app.api("GET", "/api/system/jobs", params={"kind": "notify.send"}).json()
        assert queued == []
        assert received == []


def test_the_settings_card_lists_the_events_and_ticks_the_chosen_ones(page: Page, app: ShortlistApp):
    resp = app.api(
        "PUT",
        "/api/settings",
        json={"values": {"notify.webhook.enabled": True, "notify.webhook.events": ["run.failed", "job.failed"]}},
    )
    assert resp.status_code == 200, resp.text
    page.goto("/settings#notifications")
    expect(page.get_by_text("What to send")).to_be_visible(timeout=20_000)
    expect(page.get_by_role("checkbox", name="A run failed")).to_be_checked()
    expect(page.get_by_role("checkbox", name="A job failed")).to_be_checked()
    expect(page.get_by_role("checkbox", name="A run started")).not_to_be_checked()

    page.get_by_role("checkbox", name="A run started").click()
    expect(page.get_by_role("checkbox", name="A run started")).to_be_checked()
    app.wait_for_setting("notify.webhook.events", ["run.started", "run.failed", "job.failed"])


def test_the_webhook_is_set_up_and_removed_from_its_connection_card(page: Page, app: ShortlistApp):
    """The address and the auth header live on one card in Connections, with Test and Remove like every
    other service — removing it is the way to take the header off along with the address."""
    page.goto("/settings")
    card = page.get_by_test_id("connection-notify")
    expect(card).to_be_visible(timeout=20_000)
    card.get_by_role("button", name="Set up").click()
    card.get_by_label("Address").fill("https://hooks.example.com/shortlist/abc")
    card.get_by_label("Header name").fill("X-Api-Key")
    card.get_by_label("Header value").fill("k3y-value")
    card.get_by_role("button", name="Save", exact=True).click()
    expect(card.get_by_text("Address and auth header saved").filter(visible=True)).to_be_visible()
    # Saving sends nothing yet: the card says so and links to where it is switched on.
    expect(card.get_by_text("Not sending yet", exact=False)).to_be_visible()
    card.get_by_role("link", name="Notifications").click()
    expect(page).to_have_url(re.compile(r"#notifications$"))
    expect(page.get_by_role("switch", name="Send alerts to a webhook")).to_be_in_viewport()
    saved = app.api("GET", "/api/settings").json()
    assert saved["notify.webhook.url"] == "•••••"
    assert saved["notify.webhook.auth_header_name"] == "X-Api-Key"
    assert saved["notify.webhook.auth_header_value"] == "•••••"

    page.goto("/settings#connections")
    card.get_by_role("button", name="Remove Webhook connection").click()
    card.get_by_role("button", name="Remove", exact=True).click()
    expect(card.get_by_role("button", name="Set up")).to_be_visible()
    cleared = app.api("GET", "/api/settings").json()
    assert (cleared["notify.webhook.url"], cleared["notify.webhook.auth_header_value"]) == ("", "")
    assert cleared["notify.webhook.auth_header_name"] == ""
