"""Assistant changes commit configuration, audit and owed jobs as one durable operation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.changes import (
    AccessRequirements,
    ChangeError,
    ChangeService,
    DomainPlan,
    DomainResult,
    EffectIntent,
    fingerprint,
)
from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.db.models import Base, Event, Job, Setting
from tests.db_helpers import disposing_engine

NOW = datetime(2026, 10, 5, tzinfo=UTC)


class Policy:
    def current_grant(self, session, principal, now):
        if session.get(Setting, "test.revoked"):
            raise ChangeError("missing_permission", "Connection revoked")
        revision = session.get(Setting, "test.revision")
        return SimpleNamespace(
            grant_id=principal.grant_id,
            owner_account_id=principal.owner_account_id,
            client_id=principal.client_id,
            revision=revision.value["v"] if revision else 1,
        )

    def authorize(self, grant, requirements):
        if "rows.delete" in requirements.capabilities:
            raise ChangeError("missing_permission", "Needs owner approval")


class SettingAdapter:
    kind = "test.setting"
    fail_after_write = False
    require_approval = False

    def prepare(self, session, intent):
        row = session.get(Setting, "test.value")
        return DomainPlan(
            normalized_intent={"value": int(intent["value"])},
            dependencies={"setting:test.value": fingerprint(row.value if row else None)},
            requirements=AccessRequirements(capabilities=("rows.delete" if self.require_approval else "config.write",)),
            effects=(EffectIntent("privacy.sync", {"reason": "test"}, "privacy"),),
            summary={"value": int(intent["value"])},
        )

    def apply(self, session, intent):
        row = session.get(Setting, "test.value")
        if row is None:
            session.add(Setting(key="test.value", value={"v": intent["value"]}))
        else:
            row.value = {"v": intent["value"]}
        if self.fail_after_write:
            raise RuntimeError("injected failure")
        return DomainResult(result={"saved": True}, audit_diff={"value": intent["value"]})


@pytest.fixture
def env(tmp_path):
    with disposing_engine(
        create_engine(f"sqlite:///{tmp_path / 'changes.db'}", connect_args={"timeout": 10})
    ) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        adapter = SettingAdapter()
        principal = SimpleNamespace(grant_id="grant-a", client_id="client-a", owner_account_id=42)
        service = ChangeService(sessions, {adapter.kind: adapter}, Policy(), clock=lambda: NOW)
        yield SimpleNamespace(sessions=sessions, adapter=adapter, principal=principal, service=service)


def test_apply_commits_config_audit_and_one_job(env):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    receipt = env.service.apply(env.principal, change["change_id"], "request-a")
    with env.sessions() as session:
        assert session.get(Setting, "test.value").value == {"v": 9}
        job = session.scalars(select(Job)).one()
        assert (job.operation_id, job.effect_key) == (receipt["operation_id"], "privacy")
        assert job.payload == {"reason": "test"}
        assert session.scalars(select(Event).where(Event.scope == "assistant.applied")).one()
        assert receipt["status"] == "delivery_pending"


def test_failure_rolls_back_config_receipt_consumption_and_jobs(env):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    env.adapter.fail_after_write = True
    with pytest.raises(RuntimeError, match="injected"):
        env.service.apply(env.principal, change["change_id"], "request-a")
    with env.sessions() as session:
        assert session.get(Setting, "test.value") is None
        assert session.scalars(select(AssistantOperation)).all() == []
        assert session.scalars(select(Job)).all() == []
        assert session.get(AssistantChange, change["change_id"]).operation_id is None
    env.adapter.fail_after_write = False
    assert env.service.apply(env.principal, change["change_id"], "request-a")["operation_id"]


def test_replay_same_plan_with_new_key_returns_original_operation(env):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    first = env.service.apply(env.principal, change["change_id"], "request-a")
    assert env.service.apply(env.principal, change["change_id"], "request-b") == first
    with env.sessions() as session:
        assert len(session.scalars(select(Job)).all()) == 1


def test_idempotency_key_cannot_be_reused_for_another_plan(env):
    first = env.service.prepare(env.principal, "test.setting", {"value": 9})
    env.service.apply(env.principal, first["change_id"], "request-a")
    second = env.service.prepare(env.principal, "test.setting", {"value": 10})
    with pytest.raises(ChangeError) as error:
        env.service.apply(env.principal, second["change_id"], "request-a")
    assert error.value.code == "operation_conflict"


def test_a_retry_alias_key_is_bound_to_its_original_change(env):
    first = env.service.prepare(env.principal, "test.setting", {"value": 9})
    env.service.apply(env.principal, first["change_id"], "request-a")
    env.service.apply(env.principal, first["change_id"], "request-b")
    second = env.service.prepare(env.principal, "test.setting", {"value": 10})
    with pytest.raises(ChangeError) as error:
        env.service.apply(env.principal, second["change_id"], "request-b")
    assert error.value.code == "operation_conflict"


def test_adapter_cannot_commit_configuration_outside_operation_boundary(env):
    original = env.adapter.apply

    def committing_adapter(session, intent):
        result = original(session, intent)
        session.commit()
        return result

    env.adapter.apply = committing_adapter
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    with pytest.raises(ChangeError, match="commit"):
        env.service.apply(env.principal, change["change_id"], "request-a")
    with env.sessions() as session:
        assert session.get(Setting, "test.value") is None


def test_planner_cannot_flush_a_mutation(env):
    original = env.adapter.prepare

    def writing_planner(session, intent):
        plan = original(session, intent)
        session.add(Setting(key="test.value", value={"v": 9}))
        session.flush()
        return plan

    env.adapter.prepare = writing_planner
    with pytest.raises(ChangeError, match="write"):
        env.service.prepare(env.principal, "test.setting", {"value": 9})
    with env.sessions() as session:
        assert session.get(Setting, "test.value") is None


def test_concurrent_applies_consume_a_plan_once(env):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(lambda key: env.service.apply(env.principal, change["change_id"], key), ["a", "b"]))
    assert receipts[0] == receipts[1]
    with env.sessions() as session:
        assert len(session.scalars(select(Job)).all()) == 1


@pytest.mark.parametrize("mutation", ["dependency", "revision", "revoked", "hash"])
def test_stale_or_unauthorized_plan_cannot_write(env, mutation):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    with env.sessions() as session:
        if mutation == "hash":
            stored = session.get(AssistantChange, change["change_id"])
            stored.intent = {"value": 100}
        else:
            key = {"dependency": "test.value", "revision": "test.revision", "revoked": "test.revoked"}[mutation]
            session.add(Setting(key=key, value={"v": 7}))
        session.commit()
    with pytest.raises(ChangeError):
        env.service.apply(env.principal, change["change_id"], "request-a")
    with env.sessions() as session:
        assert session.scalars(select(Job)).all() == []


def test_expired_plan_cannot_write(env):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    env.service.clock = lambda: NOW + timedelta(hours=1)
    with pytest.raises(ChangeError) as error:
        env.service.apply(env.principal, change["change_id"], "request-a")
    assert error.value.code == "stale_plan"


def test_exact_owner_approval_allows_only_the_prepared_plan(env):
    env.adapter.require_approval = True
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    assert change["authorization"]["can_apply"] is False
    with pytest.raises(ChangeError):
        env.service.apply(env.principal, change["change_id"], "request-a")
    env.service.approve(change["change_id"], owner_account_id=42)
    receipt = env.service.apply(env.principal, change["change_id"], "request-a")
    assert receipt["authorization_basis"] == "operation_approval"


def test_foreign_owner_cannot_approve_or_read_a_plan(env):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    with pytest.raises(ChangeError):
        env.service.approve(change["change_id"], owner_account_id=7)
    stranger = SimpleNamespace(grant_id="grant-b", client_id="client-b", owner_account_id=42)
    with pytest.raises(ChangeError):
        env.service.get_change(stranger, change["change_id"])


def test_new_resource_references_resolve_only_within_prepared_effects(env):
    original_prepare = env.adapter.prepare
    env.adapter.prepare = lambda session, intent: replace(
        original_prepare(session, intent),
        effects=(EffectIntent("privacy.sync", {"target": {"$ref": "row"}}, "privacy"),),
    )
    env.adapter.apply = lambda session, intent: DomainResult({}, {}, references={"row": 123})
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    env.service.apply(env.principal, change["change_id"], "request-a")
    with env.sessions() as session:
        assert session.scalars(select(Job)).one().payload == {"target": 123}


def test_unknown_reference_rolls_back_everything(env):
    original_prepare = env.adapter.prepare
    env.adapter.prepare = lambda session, intent: replace(
        original_prepare(session, intent),
        effects=(EffectIntent("privacy.sync", {"target": {"$ref": "missing"}}, "privacy"),),
    )
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    with pytest.raises(ChangeError):
        env.service.apply(env.principal, change["change_id"], "request-a")
    with env.sessions() as session:
        assert session.get(Setting, "test.value") is None
        assert session.scalars(select(Job)).all() == []


def test_progress_survives_revision_change_and_rechecks_detailed_receipt(env):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    receipt = env.service.apply(env.principal, change["change_id"], "first")
    with env.sessions() as session:
        session.add(Setting(key="test.revision", value={"v": 2}))
        session.commit()
    assert env.service.get_operation(env.principal, receipt["operation_id"])["result"]["saved"] is True
    assert env.service.apply(env.principal, change["change_id"], "retry-after-expansion") == receipt


def test_scope_reduction_preserves_only_progress_on_status_and_replay(env, monkeypatch):
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    receipt = env.service.apply(env.principal, change["change_id"], "first")
    with env.sessions() as session:
        session.add(Setting(key="test.revision", value={"v": 2}))
        session.commit()

    def deny_details(grant, requirements):
        raise ChangeError("missing_permission", "Scope reduced")

    monkeypatch.setattr(env.service.policy, "authorize", deny_details)
    progress = env.service.get_operation(env.principal, receipt["operation_id"])
    assert progress["operation_id"] == receipt["operation_id"]
    assert progress["status"] == "delivery_pending"
    assert progress["details_available"] is False
    assert set(progress) == {"operation_id", "change_id", "status", "committed_at", "finished_at", "details_available"}
    replay = env.service.apply(env.principal, change["change_id"], "retry-after-reduction")
    assert replay == progress
    with env.sessions() as session:
        assert len(session.scalars(select(Job)).all()) == 1


def test_exact_approval_does_not_carry_detailed_disclosure_across_scope_revision(env):
    env.adapter.require_approval = True
    change = env.service.prepare(env.principal, "test.setting", {"value": 9})
    env.service.approve(change["change_id"], owner_account_id=42)
    receipt = env.service.apply(env.principal, change["change_id"], "first")
    assert env.service.get_operation(env.principal, receipt["operation_id"])["result"]["saved"] is True
    with env.sessions() as session:
        session.add(Setting(key="test.revision", value={"v": 2}))
        session.commit()
    assert env.service.get_operation(env.principal, receipt["operation_id"])["details_available"] is False


@pytest.mark.parametrize("failure", ["raise", "commit"])
def test_operation_reservation_failure_rolls_back_configuration_receipt_and_outbox(env, failure):
    def reserve(session, operation, intent):
        assert operation.id and session.get(AssistantOperation, operation.id) is operation
        session.add(Setting(key="test.reservation", value={"operation_id": operation.id}))
        session.flush()
        if failure == "commit":
            session.commit()
        raise RuntimeError("injected reservation failure")

    env.adapter.stage_operation = reserve
    plan = env.service.prepare(env.principal, "test.setting", {"value": 9})
    with pytest.raises((ChangeError, RuntimeError)):
        env.service.apply(env.principal, plan["change_id"], "reservation-failure")
    with env.sessions() as session:
        assert session.get(Setting, "test.value") is None
        assert session.get(Setting, "test.reservation") is None
        assert list(session.scalars(select(AssistantOperation))) == []
        assert list(session.scalars(select(Job))) == []
        assert session.get(AssistantChange, plan["change_id"]).operation_id is None
