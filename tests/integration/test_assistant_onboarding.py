"""Fresh installation onboarding through real owner HTTP, MCP SDK, and queued workers."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select

from shortlist.server.assistant.operation_models import AssistantChange
from shortlist.server.assistant_auth.types import ASSISTANT_CAPABILITIES
from shortlist.server.auth import CSRF_HEADER
from shortlist.server.catalogs.settings import get_settings_catalog
from shortlist.server.db.models import Job, User
from shortlist.server.main import create_app
from tests.e2e.conftest import _make_fake_tmdb
from tests.fakes.fake_plex import make_fake_plex, make_fake_plextv, seed_state
from tests.integration.test_assistant_mcp_tool_matrix import McpWire, _port, _wait_for_operation
from tests.uvicorn_thread import UvicornThread

pytestmark = pytest.mark.integration


@dataclass
class FreshInstallation:
    app: object
    owner: httpx.Client
    wire: McpWire
    plex: object

    def call(self, name: str, request: dict | None = None) -> dict:
        return self.wire.exercise([(name, request)])[name]

    def metadata(self) -> None:
        saved = self.owner.put("/api/settings", json={"values": {"tmdb.apikey": "synthetic-tmdb-key"}})
        assert saved.status_code == 200
        tested = self.owner.post("/api/settings/test/tmdb")
        assert tested.status_code == 200 and tested.json()["ok"] is True

    def approved_apply(self, change_id: str, key: str) -> dict:
        approved = self.owner.post(f"/api/assistant/changes/{change_id}/approve")
        assert approved.status_code == 200, approved.text
        applied = self.call("shortlist_apply_change", {"change_id": change_id, "idempotency_key": key})["data"]
        operation = _wait_for_operation(self.wire, applied["operation_id"])
        assert operation["status"] == "completed", operation
        return operation


@contextmanager
def fresh_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The product creates its defaults; even owner identity and grants are established over HTTP."""
    plex = seed_state()
    pms = UvicornThread(make_fake_plex(plex), _port())
    tv = UvicornThread(make_fake_plextv(plex), _port())
    tmdb = UvicornThread(_make_fake_tmdb(plex), _port())
    servers = [pms, tv, tmdb]
    for server, health in ((pms, "/identity"), (tv, "/api/users"), (tmdb, "/configuration")):
        server.start()
        server.wait_until_up(health)
    plex.pms_url = f"http://127.0.0.1:{pms.port}"
    tv_url = f"http://127.0.0.1:{tv.port}"
    for target in (
        "shortlist.engine.clients.plextv.PLEXTV",
        "shortlist.server.auth.PLEXTV",
        "shortlist.server.api.setup.PLEXTV",
        "shortlist.server.services.setup_probe.PLEXTV",
    ):
        monkeypatch.setattr(target, tv_url)
    monkeypatch.setattr("shortlist.engine.clients.tmdb.API", f"http://127.0.0.1:{tmdb.port}")
    number = _port()
    base_url = f"http://127.0.0.1:{number}"
    monkeypatch.setenv("SHORTLIST_MCP_URL", base_url + "/mcp")
    monkeypatch.delenv("APP_BASE_PATH", raising=False)
    app = create_app(config_dir=tmp_path)
    runtime = UvicornThread(app, number)
    servers.append(runtime)
    runtime.start()
    runtime.wait_until_up("/api/system/health")
    try:
        with httpx.Client(base_url=base_url, headers={CSRF_HEADER: "1"}, timeout=60) as owner:
            assert owner.get("/api/setup/state").json() == {"step": 0, "state": {}, "completed": False}
            pin = owner.post("/api/auth/pin")
            assert pin.status_code == 200
            login = owner.get(f"/api/auth/pin/{pin.json()['id']}")
            assert login.status_code == 200 and login.json()["linked"] is True
            probe = owner.post("/api/setup/probe", json={"plex_url": plex.pms_url})
            assert probe.status_code == 200, probe.text
            linked = owner.post(
                "/api/setup/link",
                json={
                    "plex_url": plex.pms_url,
                    "machine_id": probe.json()["machine_id"],
                    "server_name": probe.json()["server_name"],
                    "owner_account_id": login.json()["account_id"],
                    "plex_pass": probe.json()["checks"]["plex_pass"]["ok"],
                },
            )
            assert linked.status_code == 200
            assert owner.get("/api/users").json() == []
            granted = owner.post(
                "/assistant/grants",
                json={
                    "client_id": "fresh-sdk",
                    "name": "Fresh owner-approved SDK",
                    "preset": "owner_automation",
                    "capabilities": sorted(cap.value for cap in ASSISTANT_CAPABILITIES),
                    "constraints": {
                        "include_future_rows": True,
                        "include_future_libraries": True,
                        "setting_groups": sorted({item.group.value for item in get_settings_catalog()}),
                        "destination_ids": ["https://api.themoviedb.org/3"],
                    },
                },
            )
            assert granted.status_code == 201, granted.text
            issued = owner.post(f"/assistant/grants/{granted.json()['id']}/credentials", json={"expires_in_days": 1})
            assert issued.status_code == 201
            yield FreshInstallation(app, owner, McpWire(base_url + "/mcp", issued.json()["credential"]), plex)
    finally:
        for server in reversed(servers):
            server.stop()


def test_fresh_status_distinguishes_credentials_roster_and_wizard_completion(tmp_path, monkeypatch):
    with fresh_installation(tmp_path, monkeypatch) as install:
        install.metadata()
        result = install.call("shortlist_get_setup_status")
        data = result["data"]
        assert data["credentials_ready"] is True
        assert data["discovery_ready"] is False
        assert data["setup_completed"] is False and data["ready"] is False
        checks = {check["id"]: check for check in data["checks"]}
        assert checks["libraries"]["ready"] is True
        assert checks["people"]["ready"] is False
        assert checks["wizard_completion"]["ready"] is False
        assert "people.sync" in str(result)
        assert result["next_action"] == "shortlist_plan_maintenance"
        assert install.owner.get("/api/users").json() == []


@pytest.mark.parametrize("saved_step", [0, 4])
def test_unfinished_tmdb_handoff_resumes_metadata_step_and_preserves_other_state(tmp_path, monkeypatch, saved_step):
    with fresh_installation(tmp_path, monkeypatch) as install:
        state = {"existing_resume_hint": "preserve this owner state"}
        saved = install.owner.put("/api/setup/state", json={"step": saved_step, "state": state, "completed": False})
        assert saved.status_code == 200
        settings_before = install.owner.get("/api/settings").json()
        identity_before = install.owner.get("/api/auth/session").json()
        result = install.call("shortlist_start_connection", {"service": "tmdb"})
        assert install.owner.get("/api/setup/state").json() == {"step": 2, "state": state, "completed": False}
        destination = urlsplit(result["data"]["browser_url"])
        assert destination.path == "/setup" and not destination.query and not destination.fragment
        assert "Recommendations" in str(result) and "history" in str(result)
        settings_after = install.owner.get("/api/settings").json()
        for values in (settings_before, settings_after):
            values.get("values", values).pop("setup.step", None)
        assert settings_after == settings_before
        assert install.owner.get("/api/auth/session").json() == identity_before


def test_approved_people_sync_imports_actual_roster_then_completes_setup(tmp_path, monkeypatch):
    with fresh_installation(tmp_path, monkeypatch) as install:
        install.metadata()
        planned = install.call("shortlist_plan_maintenance", {"task": "people.sync"})["data"]
        assert set(planned["required_capabilities"]) >= {
            "maintenance.execute",
            "people.write",
            "rows.update",
            "runs.execute",
        }
        assert planned["authorization"]["can_apply"] is False
        with install.app.state.sessions() as session:
            change = session.get(AssistantChange, planned["change_id"])
            assert change.requirements["requires_approval"] is True
            assert change.requirements["dynamic_rows"] is True
            assert change.requirements["dynamic_libraries"] is True
        install.wire.expect_error(
            "shortlist_apply_change", {"change_id": planned["change_id"], "idempotency_key": "fresh-unapproved-sync"}
        )
        assert install.owner.get("/api/users").json() == []
        install.approved_apply(planned["change_id"], "fresh-approved-sync")
        expected_accounts = set(install.plex.users) | {install.plex.owner_account_id}
        with install.app.state.sessions() as session:
            people = session.scalars(select(User)).all()
            assert {person.plex_account_id for person in people} == expected_accounts
            sync = session.scalars(select(Job).where(Job.kind == "sync.users")).all()
            assert len(sync) == 1 and sync[0].status == "done"
            assert sync[0].result["added"] == len(expected_accounts)
        status = install.call("shortlist_get_setup_status")
        assert status["data"]["credentials_ready"] is True and status["data"]["discovery_ready"] is True
        assert status["data"]["ready"] is False and "setup.complete" in str(status)
        completion = install.call("shortlist_plan_maintenance", {"task": "setup.complete"})["data"]
        assert completion["authorization"]["can_apply"] is False
        install.approved_apply(completion["change_id"], "fresh-complete-setup")
        assert install.owner.get("/api/setup/state").json()["completed"] is True
        final = install.call("shortlist_get_setup_status")["data"]
        assert final["ready"] is True and final["setup_completed"] is True
        with install.app.state.sessions() as session:
            assert len(session.scalars(select(Job).where(Job.kind == "sync.users")).all()) == 1


def test_setup_completion_cannot_mark_fresh_unready_installation_complete(tmp_path, monkeypatch):
    with fresh_installation(tmp_path, monkeypatch) as install:
        install.wire.expect_error("shortlist_plan_maintenance", {"task": "setup.complete"}, code="invalid_selection")
        refused = install.owner.put("/api/setup/state", json={"step": 5, "state": {}, "completed": True})
        assert refused.status_code == 409
        assert "TMDB" in refused.json()["detail"]
        assert install.owner.get("/api/setup/state").json()["completed"] is False
        assert install.owner.get("/api/users").json() == []


@pytest.mark.parametrize("missing", ["people", "libraries"])
def test_setup_completion_requires_current_library_and_roster_evidence(tmp_path, monkeypatch, missing):
    with fresh_installation(tmp_path, monkeypatch) as install:
        install.metadata()
        if missing == "libraries":
            sync = install.call("shortlist_plan_maintenance", {"task": "people.sync"})["data"]
            install.approved_apply(sync["change_id"], "readiness-sync-people")
            # Only the external PMS changes. A previously successful probe is insufficient.
            install.plex.sections.clear()
        install.wire.expect_error("shortlist_plan_maintenance", {"task": "setup.complete"}, code="invalid_selection")
        refused = install.owner.put("/api/setup/state", json={"step": 5, "state": {}, "completed": True})
        assert refused.status_code == 409
        assert ("Sync people" if missing == "people" else "library") in refused.json()["detail"]
        assert install.owner.get("/api/setup/state").json()["completed"] is False


def test_setup_completion_revalidates_readiness_after_exact_owner_approval(tmp_path, monkeypatch):
    with fresh_installation(tmp_path, monkeypatch) as install:
        install.metadata()
        sync = install.call("shortlist_plan_maintenance", {"task": "people.sync"})["data"]
        install.approved_apply(sync["change_id"], "stale-completion-sync")
        completion = install.call("shortlist_plan_maintenance", {"task": "setup.complete"})["data"]
        assert install.owner.post(f"/api/assistant/changes/{completion['change_id']}/approve").status_code == 200
        assert install.owner.put("/api/settings", json={"values": {"tmdb.apikey": ""}}).status_code == 200
        install.wire.expect_error(
            "shortlist_apply_change",
            {
                "change_id": completion["change_id"],
                "idempotency_key": "stale-completion-apply",
            },
        )
        assert install.owner.get("/api/setup/state").json()["completed"] is False


def test_people_sync_does_not_dispatch_after_the_reviewed_configuration_changes(tmp_path, monkeypatch):
    with fresh_installation(tmp_path, monkeypatch) as install:
        sync = install.call("shortlist_plan_maintenance", {"task": "people.sync"})["data"]
        assert install.owner.post(f"/api/assistant/changes/{sync['change_id']}/approve").status_code == 200
        assert install.owner.put("/api/settings", json={"values": {"row.size": 9}}).status_code == 200
        install.wire.expect_error(
            "shortlist_apply_change",
            {
                "change_id": sync["change_id"],
                "idempotency_key": "stale-people-sync-apply",
            },
            code="stale_plan",
        )
        assert install.owner.get("/api/users").json() == []
        with install.app.state.sessions() as session:
            assert session.scalars(select(Job).where(Job.kind == "sync.users")).all() == []
