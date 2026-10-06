"""Run plans reject unbudgeted effects and pin explicit execution selectors."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.changes import ChangeError
from shortlist.server.assistant.run_adapter import RunAdapter, RunIntent
from shortlist.server.db.models import Base, Collection, User
from shortlist.server.services.run_service import RunService


@pytest.fixture
def run_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runs.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        session.add(User(id=1, plex_account_id=11, slug="alice", username="alice", enabled=True))
        session.add(User(id=2, plex_account_id=22, slug="bob", username="bob", enabled=True))
        session.add(Collection(id=1, slug="row-one", name="Picked", library_keys=["1"]))
        session.commit()
    from shortlist.server.services.secrets import SecretBox

    state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path), assistant_auth=object())
    state.run_service = RunService(sessions, SimpleNamespace(publish=lambda *a: None), tmp_path, state.secrets)
    state.run_service.state = state
    yield state
    engine.dispose()


def test_run_requires_explicit_nonempty_selectors():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RunIntent.model_validate({"row_ids": [], "person_ids": [1], "dry_run": True})
    with pytest.raises(ValidationError):
        RunIntent.model_validate({"row_ids": [1], "person_ids": [1]})


def test_run_projection_is_pure_and_external_effect_is_once(run_env):
    with run_env.sessions() as session:
        plan = RunAdapter(run_env).prepare(session, {"row_ids": [1], "person_ids": [1], "dry_run": True})
        assert not session.new and not session.dirty
        assert plan.effects[0].max_attempts == 1
        assert plan.effects[0].payload == {"run_id": {"$ref": "run_id"}}
        assert "history.use" in plan.requirements.capabilities
        assert plan.requirements.person_ids == (1,)


def test_ai_source_requires_budgeted_dispatch(run_env):
    with run_env.sessions() as session:
        session.get(Collection, 1).candidate_sources = ["llm_web"]
        session.commit()
        with pytest.raises(ChangeError, match="budget"):
            RunAdapter(run_env).prepare(session, {"row_ids": [1], "person_ids": [1], "dry_run": True})


def test_shared_run_rejects_partial_roster(run_env):
    with run_env.sessions() as session:
        session.get(Collection, 1).build = "shared"
        session.commit()
        with pytest.raises(ChangeError, match="roster"):
            RunAdapter(run_env).prepare(
                session,
                {
                    "row_ids": [1],
                    "person_ids": [1],
                    "dry_run": False,
                    "include_shared": True,
                },
            )


def test_run_insert_rolls_back_with_caller(run_env):
    from sqlalchemy import select

    from shortlist.server.db.models import Run

    with run_env.sessions() as session:
        result = RunAdapter(run_env).apply(session, {"row_ids": [1], "person_ids": [1], "dry_run": True})
        assert result.created_run_ids == (result.result["run_id"],)
        session.rollback()
    with run_env.sessions() as session:
        assert list(session.scalars(select(Run))) == []


def _authorized_run(run_env, *, approved=False):
    from shortlist.server.assistant.changes import ChangeService
    from shortlist.server.assistant_auth import GrantConstraints, GrantPreset, StoredGrantIdentity
    from shortlist.server.assistant_auth.models import AssistantGrant
    from shortlist.server.assistant_auth.repository import require_current_grant_in_session
    from shortlist.server.db.models import Server

    capabilities = [
        "changes.prepare",
        "runs.preview",
        "history.use",
        "history.providers",
        "activity.read",
        "jobs.cancel",
    ]
    if approved:
        capabilities.remove("history.providers")
    with run_env.sessions() as session:
        session.add(Server(id=1, machine_id="test", url="http://test.invalid", token_enc="test", owner_account_id=42))
        session.add(
            AssistantGrant(
                id="grant-run",
                owner_account_id=42,
                client_id="test",
                name="Test run",
                preset=GrantPreset.OWNER_AUTOMATION.value,
                capabilities=capabilities,
                constraints=GrantConstraints(
                    row_ids=frozenset({1}),
                    person_ids=frozenset({1}),
                    library_keys=frozenset({"1"}),
                    include_future_libraries=True,
                ).as_dict(),
            )
        )
        session.commit()
        principal = require_current_grant_in_session(
            session, StoredGrantIdentity(grant_id="grant-run", client_id="test", owner_account_id=42, revision=1)
        )
    service = ChangeService(run_env.sessions, {"run": RunAdapter(run_env)})
    change = service.prepare(principal, "run", {"row_ids": [1], "person_ids": [1], "dry_run": True})
    if approved:
        service.approve(change["change_id"], owner_account_id=42)
    receipt = service.apply(principal, change["change_id"], "run-key")
    return principal, service, receipt


def test_queue_stamps_actor_and_handoff_receipt_is_not_completion(run_env):
    from sqlalchemy import select

    from shortlist.server.db.models import Job, Run

    _principal, service, receipt = _authorized_run(run_env)
    with run_env.sessions() as session:
        run = session.get(Run, receipt["result"]["run_id"])
        actor = run.stats["assistant_actor"]
        assert actor["grant_id"] == "grant-run"
        assert actor["operation_id"] == receipt["operation_id"]
        assert actor["requirements_hash"]
        job = session.scalars(select(Job).where(Job.operation_id == receipt["operation_id"])).one()
        assert job.payload == {"run_id": run.id}
        assert job.max_attempts == 1
        job.status = "done"
        session.commit()
    assert service.get_operation(_principal, receipt["operation_id"])["status"] == "queued"


def test_revocation_rejects_before_context_construction(run_env, monkeypatch):
    from datetime import UTC, datetime

    from shortlist.server.assistant_auth.models import AssistantGrant

    _principal, _service, receipt = _authorized_run(run_env)
    with run_env.sessions() as session:
        session.get(AssistantGrant, "grant-run").revoked_at = datetime.now(UTC)
        session.commit()
    calls = []
    monkeypatch.setattr(run_env.run_service, "build_context", lambda **kwargs: calls.append(kwargs))
    with pytest.raises((ChangeError, PermissionError)):
        run_env.run_service._build_assistant_context(
            receipt["result"]["run_id"], dry_run=True, loop=None, log_sink=None
        )
    assert calls == []


def test_changed_configuration_rejects_before_context_construction(run_env, monkeypatch):
    _principal, _service, receipt = _authorized_run(run_env)
    with run_env.sessions() as session:
        session.get(Collection, 1).size = 99
        session.commit()
    calls = []
    monkeypatch.setattr(run_env.run_service, "build_context", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ChangeError, match="changed"):
        run_env.run_service._build_assistant_context(
            receipt["result"]["run_id"], dry_run=True, loop=None, log_sink=None
        )
    assert calls == []


def test_interrupted_handoff_is_not_replayed(run_env):
    import asyncio

    from shortlist.server.db.models import Run

    _principal, _service, receipt = _authorized_run(run_env)
    run_id = receipt["result"]["run_id"]
    with run_env.sessions() as session:
        run = session.get(Run, run_id)
        run.stats = {**run.stats, "assistant_handoff_at": "2026-10-05T00:00:00+00:00"}
        session.commit()
    with pytest.raises(ChangeError, match="not replayed"):
        asyncio.run(run_env.run_service.dispatch_queued_assistant_run(run_id))
    assert not run_env.run_service._tasks
    with run_env.sessions() as session:
        assert session.get(Run, run_id).stats["assistant_outcome_unknown"] is True


@pytest.mark.parametrize("reduced", [False, True])
def test_queued_standing_run_rechecks_exact_authority_after_revision_change(run_env, reduced):
    from shortlist.server.assistant.run_adapter import validate_execution_in_session
    from shortlist.server.assistant_auth.models import AssistantGrant
    from shortlist.server.db.models import Run

    _principal, _service, receipt = _authorized_run(run_env)
    with run_env.sessions() as session:
        grant = session.get(AssistantGrant, "grant-run")
        grant.revision += 1
        if reduced:
            grant.capabilities = [cap for cap in grant.capabilities if cap != "history.providers"]
        else:
            grant.constraints = {**grant.constraints, "row_ids": [1, 99]}
        session.commit()
        run = session.get(Run, receipt["result"]["run_id"])
        if reduced:
            with pytest.raises(PermissionError):
                validate_execution_in_session(session, run_env, run)
        else:
            contract, profiles = validate_execution_in_session(session, run_env, run)
            assert contract["intent"]["row_ids"] == [1]
            assert [profile.slug for profile in profiles] == ["alice"]


def test_exact_run_approval_cannot_survive_a_later_grant_revision(run_env):
    from shortlist.server.assistant.run_adapter import validate_execution_in_session
    from shortlist.server.assistant_auth.models import AssistantGrant
    from shortlist.server.db.models import Run

    _principal, _service, receipt = _authorized_run(run_env, approved=True)
    assert receipt["authorization_basis"] == "operation_approval"
    with run_env.sessions() as session:
        run = session.get(Run, receipt["result"]["run_id"])
        validate_execution_in_session(session, run_env, run)
        session.get(AssistantGrant, "grant-run").revision += 1
        session.commit()
        with pytest.raises(PermissionError):
            validate_execution_in_session(session, run_env, run)


@pytest.mark.parametrize("stage", ["dispatch", "context"])
def test_disabling_assistant_access_stops_queued_runs_before_execution(run_env, monkeypatch, stage):
    import asyncio

    from shortlist.server.db.models import Run

    _principal, _service, receipt = _authorized_run(run_env)
    run_id = receipt["result"]["run_id"]
    run_env.assistant_auth = None
    calls = []

    async def execution(*args):
        calls.append(args)

    monkeypatch.setattr(run_env.run_service, "_execute", execution)
    monkeypatch.setattr(run_env.run_service, "build_context", lambda **kwargs: calls.append(kwargs))
    with pytest.raises(ChangeError, match="disabled"):
        if stage == "dispatch":
            asyncio.run(run_env.run_service.dispatch_queued_assistant_run(run_id))
        else:
            run_env.run_service._build_assistant_context(run_id, dry_run=True, loop=None, log_sink=None)
    assert calls == []
    with run_env.sessions() as session:
        run = session.get(Run, run_id)
        assert "assistant_handoff_at" not in run.stats
        if stage == "dispatch":
            assert run.status == "aborted"
