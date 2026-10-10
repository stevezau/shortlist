"""Maintenance is a finite named action with exact owner approval and durable effects."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from shortlist.server.assistant.changes import ChangeError, ChangeService
from shortlist.server.assistant.maintenance_adapter import MaintenanceAdapter, MaintenanceIntent
from shortlist.server.db.models import Collection, Job
from tests.unit.assistant_fixtures import setup_env  # noqa: F401


@pytest.mark.parametrize(
    "intent",
    [
        {"task": "shell", "command": "echo test"},
        {"task": "row.cleanup"},
        {"task": "row.cleanup", "row_id": True},
        {"task": "cache.refresh", "row_id": 1},
        {"task": "delivery.reconcile", "row_id": 1, "payload": {"all": True}},
    ],
)
def test_maintenance_rejects_arbitrary_dispatch_and_ambiguous_targets(intent):
    with pytest.raises(ValueError):
        MaintenanceIntent.model_validate(intent)


def test_maintenance_requires_exact_approval_and_leaves_configuration_intact(setup_env):
    env = setup_env
    state = SimpleNamespace(sessions=env.sessions, secrets=None)
    service = ChangeService(env.sessions, {"maintenance": MaintenanceAdapter(state)})
    with env.sessions() as session:
        session.add(Collection(id=10, slug="owned-row", name="Owned row", library_keys=["1"]))
        session.commit()
    plan = service.prepare(env.principal, "maintenance", {"task": "row.cleanup", "row_id": 10})
    assert plan["authorization"]["can_apply"] is False
    with pytest.raises(ChangeError, match="approval"):
        service.apply(env.principal, plan["change_id"], "cleanup-approved-once")
    service.approve(plan["change_id"], owner_account_id=42)
    receipt = service.apply(env.principal, plan["change_id"], "cleanup-approved-once")
    assert receipt["authorization_basis"] == "operation_approval"
    assert service.apply(env.principal, plan["change_id"], "cleanup-replay") == receipt
    with env.sessions() as session:
        row = session.get(Collection, 10)
        assert row.name == "Owned row" and row.enabled
        job = session.scalars(select(Job)).one()
        assert job.operation_id == receipt["operation_id"]
        assert job.payload["steps"][0]["kind"] == "row.reconcile"
        assert job.payload["steps"][0]["payload"]["slug"] == "owned-row"


def test_uninstall_has_no_hidden_assistant_execution_path(setup_env):
    env = setup_env
    adapter = MaintenanceAdapter(SimpleNamespace(sessions=env.sessions, secrets=None))
    service = ChangeService(env.sessions, {"maintenance": adapter})
    with pytest.raises(ChangeError, match="browser"):
        service.prepare(env.principal, "maintenance", {"task": "uninstall"})
    with env.sessions() as session:
        with pytest.raises(ChangeError, match="browser"):
            adapter.apply(session, {"task": "uninstall"})
        assert list(session.scalars(select(Job))) == []


def test_cache_refresh_is_bounded_and_cannot_smuggle_a_job_payload(setup_env):
    env = setup_env
    adapter = MaintenanceAdapter(SimpleNamespace(sessions=env.sessions, secrets=None))
    with env.sessions() as session:
        plan = adapter.prepare(session, {"task": "cache.refresh"})
        assert not session.new and not session.dirty
        assert plan.requirements.requires_approval is True
        assert plan.requirements.row_ids == ()
        assert plan.effects[0].payload["steps"] == [{"kind": "cache.invalidate", "payload": {}}]
    with pytest.raises(ValueError):
        MaintenanceIntent.model_validate(
            {"task": "cache.refresh", "kind": "user.cleanup", "payload": {"slug": "other"}}
        )
