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
        # Spelled out because the endpoint now declares a Pydantic response model, and a model that
        # forgot a key would DROP it from the payload rather than fail — the Logs page would simply
        # stop paginating (`truncated`) or stop naming its file, with nothing red anywhere.
        assert set(body) == {"lines", "total_matched", "truncated", "file"}
        assert set(body["lines"][0]) == {"ts", "level", "source", "message"}

    def test_an_empty_log_directory_still_returns_the_whole_shape(self, client: TestClient):
        """The no-file branch builds its own dict, so it is a second place a key can go missing."""
        import shutil

        shutil.rmtree(client.app.state.config_dir / "logs", ignore_errors=True)

        body = client.get("/api/system/logs").json()

        assert set(body) == {"lines", "total_matched", "truncated", "file"}
        assert body == {"lines": [], "total_matched": 0, "truncated": False, "file": None}

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

        body = client.get("/api/notifications").json()
        items = body["notifications"]
        ids = {n["id"] for n in items}
        assert "runs-paused" in ids
        assert any(i.startswith("run-failed-") for i in ids)
        order = {"error": 0, "warning": 1, "info": 2}
        severities = [order[n["severity"]] for n in items]
        assert severities == sorted(severities)  # error before warning before info
        # Every field the bell renders, spelled out: the endpoint declares a response model now, and
        # one that missed `action_url` or `dismissable` would quietly serve un-clickable, un-hideable
        # alerts instead of erroring.
        assert set(body) == {"notifications"}
        for note in items:
            assert set(note) == {
                "id",
                "severity",
                "title",
                "body",
                "action_url",
                "action_label",
                "dismissable",
            }, note

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

    def test_a_dismissable_flag_is_served_for_every_alert(self, client: TestClient, monkeypatch):
        """The other branch of the notification shape: an alert that CAN be hidden. `dismissable` is
        the field the bell keys its hide button on, so it must survive serialization in both states."""
        import shortlist.server.notifications as notif

        monkeypatch.setattr(notif, "check_for_update", lambda _v: {"latest": "9.9.9", "url": "https://example/rel"})
        with client.app.state.sessions() as session:
            SettingsStore(session).set("paused_all", True)
            session.commit()

        by_id = {n["id"]: n for n in client.get("/api/notifications").json()["notifications"]}

        assert by_id["update-9.9.9"]["dismissable"] is True
        assert by_id["runs-paused"]["dismissable"] is False

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


class TestSystemResponseShapes:
    """The endpoints now declare Pydantic response models, and a model that misses a key DROPS it
    from the payload — silently, in production, blanking whatever read it. So each key set is
    asserted in full rather than sampled.
    """

    def test_version_reports_current_latest_and_install_type(self, client: TestClient, monkeypatch):
        from shortlist.server import version_check

        # No test may touch the network. The release check is cached in-process, so the whole cache
        # is REPLACED (not just cleared) — monkeypatch then puts the real one back untouched, rather
        # than leaving a reset cache behind for a later test to refill over the network.
        monkeypatch.setattr(version_check, "_fetch_latest", lambda: {"tag": "v9.9.9", "url": "https://example/rel"})
        monkeypatch.setattr(version_check, "_cache", {"at": None, "value": None})

        body = client.get("/api/system/version").json()

        assert set(body) == {"current_version", "latest_version", "update_available", "install_type"}
        assert body["latest_version"] == "9.9.9"  # the "v" is stripped for display
        assert body["update_available"] is True

    def test_version_reports_a_null_latest_when_the_check_is_unavailable(self, client: TestClient, monkeypatch):
        """GitHub being down must not change the SHAPE — `latest_version` goes null, nothing goes
        missing, or the About panel loses the fields it renders around it."""
        from shortlist.server import version_check

        monkeypatch.setattr(version_check, "_fetch_latest", lambda: None)
        monkeypatch.setattr(version_check, "_cache", {"at": None, "value": None})

        body = client.get("/api/system/version").json()

        assert set(body) == {"current_version", "latest_version", "update_available", "install_type"}
        assert body["latest_version"] is None and body["update_available"] is False

    def test_syncs_reports_each_schedule_with_its_nested_shape(self, client: TestClient):
        body = client.get("/api/system/syncs").json()

        assert set(body) == {"watched", "users", "backup"}
        assert set(body["watched"]) == {"last", "next", "cron"}
        assert set(body["users"]) == {"last", "next", "cron"}
        # Backups carry no "last": the backup list itself is that answer, so the nested shape differs.
        assert set(body["backup"]) == {"next", "cron", "max_keep"}
        assert isinstance(body["backup"]["max_keep"], int)

    def test_image_provider_explains_itself_when_it_cannot_generate(self, client: TestClient):
        body = client.get("/api/system/image-provider").json()

        assert set(body) == {"capable", "provider", "reason"}
        assert body["capable"] is False
        assert body["reason"], "the row editor disables the option, so it must be able to say why"

    def test_image_provider_reports_capable_with_an_image_model_configured(self, client: TestClient):
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("curator.provider", "openai")
            store.set("curator.api_key", "sk-test")
            session.commit()

        body = client.get("/api/system/image-provider").json()

        assert set(body) == {"capable", "provider", "reason"}
        assert body["capable"] is True and body["reason"] == ""

    def _connect_plex(self, client: TestClient) -> None:
        with client.app.state.sessions() as session:
            store = SettingsStore(session, client.app.state.secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "tok")
            session.commit()

    def test_libraries_lists_the_delivery_targets(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace

        self._connect_plex(client)

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                # `key` is an int on a real PMS and a string in the API — the model must not lose it.
                return [SimpleNamespace(key=1, title="Movies", type="movie")]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        body = client.get("/api/system/libraries").json()

        assert [set(x) for x in body] == [{"key", "title", "type"}]
        assert body == [{"key": "1", "title": "Movies", "type": "movie"}]

    def test_library_collections_offers_anchors_and_skips_our_own_rows(self, client: TestClient, monkeypatch):
        from types import SimpleNamespace

        self._connect_plex(client)
        ours = SimpleNamespace(title="Picked for You", labels=[SimpleNamespace(tag="shortlist_sarah")])
        theirs = SimpleNamespace(title="New Series (Unwatched)", labels=[])
        section = SimpleNamespace(
            key=1,
            title="Movies",
            type="movie",
            collections=lambda: [ours, theirs],
            managedHubs=lambda: [
                SimpleNamespace(title="Picked for You"),
                SimpleNamespace(title="New Series (Unwatched)"),
            ],
        )

        class FakePlex:
            def __init__(self, *a, **k):
                pass

            def sections(self):
                return [section]

        monkeypatch.setattr("shortlist.engine.clients.plex_pms.PlexClient", FakePlex)

        body = client.get("/api/system/libraries/1/collections").json()

        assert body == [{"title": "New Series (Unwatched)"}]  # you don't anchor a row to itself
        assert set(body[0]) == {"title"}
