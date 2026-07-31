"""API contract tests: the in-app logs view, the audit log, and the notifications bell."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.server.auth import SESSION_COOKIE
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration


class TestLogsApi:
    """The in-app Logs view. Owner-only, and redacted — it exists to be copied into bug reports."""

    def _write_log(self, client: TestClient, *lines: str) -> None:
        logs = client.app.state.config_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "shortlist.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    LINE = "2026-07-21 07:27:18.100 | {level:<8} | shortlist.server.main:lifespan:168 - {message}"

    def test_returns_parsed_lines_filtered_by_level(self, client: TestClient):
        self._write_log(
            client,
            self.LINE.format(level="DEBUG", message="quiet"),
            self.LINE.format(level="ERROR", message="loud"),
        )

        body = client.get("/api/system/logs?level=ERROR").json()

        assert [x["message"] for x in body["lines"]] == ["loud"]
        assert body["file"] == "shortlist.log"

    def test_never_serves_a_credential(self, client: TestClient):
        """The whole point of the view is that it gets shared, so this is the load-bearing test."""
        self._write_log(client, self.LINE.format(level="INFO", message="GET /x?X-Plex-Token=LEAKME -> 200"))

        assert "LEAKME" not in client.get("/api/system/logs").text

    def test_the_zip_download_is_attached_and_redacted(self, client: TestClient):
        import io
        import zipfile

        self._write_log(client, self.LINE.format(level="INFO", message="token: X-Plex-Token: LEAKME"))

        r = client.get("/api/system/logs/download")

        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        assert "attachment; filename=" in r.headers["content-disposition"]
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        assert "LEAKME" not in archive.read("logs/shortlist.log").decode()

    def test_logs_are_owner_only(self, client: TestClient):
        """Logs describe the whole server and name every user on it — they are not public."""
        client.cookies.delete(SESSION_COOKIE)
        assert client.get("/api/system/logs").status_code == 401
        assert client.get("/api/system/logs/download").status_code == 401


class TestEventsApi:
    def test_audit_log_empty(self, client: TestClient):
        assert client.get("/api/events/log").json() == []


class TestNotifications:
    def test_surface_paused_and_failed_run_most_severe_first(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif
        from shortlist.server.db.models import Run

        monkeypatch.setattr(notif, "check_for_update", lambda _v: None)  # never touch GitHub in a test
        with client.app.state.sessions() as session:
            SettingsStore(session).set("paused_all", True)
            session.add(Run(trigger="manual", status="error"))
            session.commit()

        items = client.get("/api/notifications").json()["notifications"]
        ids = {n["id"] for n in items}
        assert "runs-paused" in ids
        assert any(i.startswith("run-failed-") for i in ids)
        order = {"error": 0, "warning": 1, "info": 2}
        severities = [order[n["severity"]] for n in items]
        assert severities == sorted(severities)  # error before warning before info

    def test_a_partial_run_and_recent_errors_surface_as_warnings(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif
        from shortlist.server.db.models import Event, Run

        monkeypatch.setattr(notif, "check_for_update", lambda _v: None)
        with client.app.state.sessions() as session:
            session.add(Run(trigger="manual", status="ok", stats={"users_ok": 1, "users_error": 2}))
            session.add(Event(scope="requests.send", level="error", message={"detail": "arr down"}))
            session.commit()

        items = client.get("/api/notifications").json()["notifications"]
        by_id = {n["id"]: n for n in items}
        partial = next(n for k, n in by_id.items() if k.startswith("run-partial-"))
        assert "2 people failed" in partial["title"]  # pluralized
        assert "recent-errors" in by_id and by_id["recent-errors"]["severity"] == "warning"

    def test_update_notification_can_be_dismissed_per_version(self, client: TestClient, monkeypatch):
        import shortlist.server.notifications as notif

        monkeypatch.setattr(notif, "check_for_update", lambda _v: {"latest": "9.9.9", "url": "https://example/rel"})
        first = client.get("/api/notifications").json()["notifications"]
        assert any(n["id"] == "update-9.9.9" and n["dismissable"] for n in first)

        assert client.post("/api/notifications/dismiss", json={"id": "update-9.9.9"}).json() == {"ok": True}
        after = client.get("/api/notifications").json()["notifications"]
        assert not any(n["id"] == "update-9.9.9" for n in after)  # dismissed by id

    def test_debug_bundle_reports_facts_but_never_a_secret(self, client: TestClient):

        with client.app.state.sessions() as session:
            SettingsStore(session, client.app.state.secrets).set("plex.token", "SUPERSECRETTOKEN")
            SettingsStore(session).set("plex.url", "http://pms")
            session.commit()

        r = client.get("/api/system/debug")
        assert r.status_code == 200
        text = r.text
        assert "Shortlist debug bundle" in text and "db migration head:" in text
        assert "plex=True" in text  # connection reported as configured...
        assert "SUPERSECRETTOKEN" not in text  # ...but the token itself is never in the bundle
