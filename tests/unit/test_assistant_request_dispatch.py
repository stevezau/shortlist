"""Acquisition claims belong to the apply transaction and uncertain sends are never repeated."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.changes import ChangeError, ChangeService
from shortlist.server.assistant.operation_models import AssistantOperation, AssistantRequestDispatch
from shortlist.server.assistant.request_adapter import RequestAdapter, dispatch_assistant_requests
from shortlist.server.assistant_auth import Capability, GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.db.models import Base, Collection, Job, RequestCandidate, Server, User
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import SettingsStore
from tests.db_helpers import disposing_engine
from tests.unit.test_assistant_requests import candidate


@pytest.fixture
def request_env(tmp_path):
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'requests.db'}")) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path))
        state.run_service = SimpleNamespace(
            build_requests_context=lambda: pytest.fail("separate context during planning")
        )
        with sessions() as session:
            session.add(
                Server(machine_id="requests", url="http://unused.invalid", token_enc="unused", owner_account_id=42)
            )
            session.add(User(id=1, slug="sarah", username="sarah", plex_account_id=10))
            session.add(Collection(id=1, slug="movies", name="Movies", library_keys=["1"]))
            item = candidate()
            item.row_slug = "movies"
            session.add(item)
            store = SettingsStore(session, state.secrets)
            for key, value in {
                "requests.enabled": True,
                "requests.target": "arr",
                "requests.radarr.url": "http://radarr.test",
                "requests.radarr.apikey": "private-request-key",
                "requests.radarr.quality_profile_id": 7,
                "requests.radarr.root_folder": "/movies",
                "requests.tag": "shortlist",
                "tmdb.apikey": "private-metadata-key",
            }.items():
                store.set_in_transaction(key, value)
            session.commit()
        repository = AssistantAuthRepository(sessions, CredentialHasher(b"request-test-key-0000000000000000"))
        principal = repository.create_grant(
            owner_account_id=42,
            client_id="requests-test",
            name="Requests test",
            preset=GrantPreset.OWNER_AUTOMATION,
            capabilities={
                Capability.CHANGES_PREPARE,
                Capability.REQUESTS_READ,
                Capability.REQUESTS_MANAGE,
                Capability.REQUESTS_SEND,
            },
            constraints=GrantConstraints(
                row_ids=frozenset({1}),
                person_ids=frozenset({1}),
                library_keys=frozenset({"1"}),
                destination_ids=frozenset({"radarr", "http://radarr.test"}),
                max_batch_size=25,
            ),
        )
        state.assistant_auth = SimpleNamespace(repository=repository)
        adapter = RequestAdapter(state)
        service = ChangeService(sessions, {"requests": adapter})
        yield SimpleNamespace(state=state, adapter=adapter, service=service, principal=principal, repository=repository)


def prepare_send(env):
    return env.service.prepare(env.principal, "requests", {"action": "send", "candidate_ids": [1]})


def apply_send(env):
    plan = prepare_send(env)
    receipt = env.service.apply(env.principal, plan["change_id"], "send-once")
    with env.state.sessions() as session:
        job = session.scalars(select(Job).where(Job.operation_id == receipt["operation_id"])).one()
        return plan, receipt, job.id, job.payload


def dispatch(env, job_id, payload):
    import asyncio

    return asyncio.run(dispatch_assistant_requests(env.state, payload, job_id=job_id))


@pytest.mark.parametrize("action", ["reject", "restore", "archive"])
def test_local_request_decisions_resolve_the_candidate_row_and_people(request_env, action):
    env = request_env
    with env.state.sessions() as session:
        plan = env.adapter.prepare(session, {"action": action, "candidate_ids": [1]})
        assert plan.requirements.row_ids == (1,)
        assert plan.requirements.person_ids == (1,)
        assert plan.requirements.library_keys == ("1",)
        assert not session.new and not session.dirty
    from dataclasses import replace

    outside = replace(env.principal, constraints=replace(env.principal.constraints, library_keys=frozenset()))
    # Exercise current DB authority, not a manually widened context.
    from shortlist.server.assistant_auth.models import AssistantGrant

    with env.state.sessions() as session:
        session.get(AssistantGrant, env.principal.grant_id).constraints = outside.constraints.as_dict()
        session.commit()
    saved = env.service.prepare(outside, "requests", {"action": action, "candidate_ids": [1]})
    assert saved["authorization"]["can_apply"] is False


def test_apply_globally_claims_candidate_with_operation_and_job(request_env):
    env = request_env
    plan, receipt, _job_id, payload = apply_send(env)
    with env.state.sessions() as session:
        claim = session.scalars(select(AssistantRequestDispatch)).one()
        assert claim.operation_id == receipt["operation_id"]
        assert claim.candidate_id == 1 and claim.status == "reserved"
        assert "private-request-key" not in str(claim.request_body)
    assert env.service.apply(env.principal, plan["change_id"], "same-plan-another-key") == receipt
    assert "private-request-key" not in str(payload)
    with pytest.raises((ChangeError, ValueError)):
        other = prepare_send(env)
        env.service.apply(env.principal, other["change_id"], "new-plan-must-not-resend")


@pytest.mark.parametrize("stage", ["reserved", "external_started", "outcome_unknown", "succeeded"])
def test_a_new_plan_cannot_send_a_candidate_with_an_existing_claim(request_env, stage):
    env = request_env
    _plan, _receipt, _job_id, _payload = apply_send(env)
    with env.state.sessions() as session:
        session.scalars(select(AssistantRequestDispatch)).one().status = stage
        session.commit()
    with pytest.raises((ChangeError, ValueError)):
        plan = prepare_send(env)
        env.service.apply(env.principal, plan["change_id"], "independent-retry")


def test_claim_and_configuration_roll_back_if_enqueue_fails(request_env, monkeypatch):
    env = request_env
    plan = prepare_send(env)

    def fail_enqueue(*args, **kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr("shortlist.server.services.jobs.enqueue_in_session", fail_enqueue)
    with pytest.raises(RuntimeError, match="outbox"):
        env.service.apply(env.principal, plan["change_id"], "outbox-failure")
    with env.state.sessions() as session:
        assert list(session.scalars(select(AssistantRequestDispatch))) == []
        assert list(session.scalars(select(AssistantOperation))) == []
        assert list(session.scalars(select(Job))) == []
        assert session.get(RequestCandidate, 1).status == "pending"


@pytest.mark.parametrize("change", ["revoked", "owner", "token_scope", "target_url", "target_key"])
def test_dispatch_rechecks_identity_scope_and_frozen_credentials_before_io(request_env, monkeypatch, change):
    from datetime import UTC, datetime

    from shortlist.server.assistant_auth.models import AssistantGrant

    env = request_env
    _plan, receipt, job_id, payload = apply_send(env)
    with env.state.sessions() as session:
        if change == "revoked":
            session.get(AssistantGrant, env.principal.grant_id).revoked_at = datetime.now(UTC)
        elif change == "owner":
            session.scalars(select(Server)).one().owner_account_id = 99
        elif change == "token_scope":
            operation = session.get(AssistantOperation, receipt["operation_id"])
            operation.result = {**operation.result, "_effective_capabilities": ["changes.prepare", "requests.read"]}
        else:
            key = "requests.radarr.url" if change == "target_url" else "requests.radarr.apikey"
            SettingsStore(session, env.state.secrets).set_in_transaction(
                key, "http://changed.test" if change == "target_url" else "changed-key"
            )
        session.commit()
    calls = []
    monkeypatch.setattr("shortlist.server.assistant.request_adapter._send_once", lambda *args: calls.append(args))
    dispatch(env, job_id, payload)
    assert calls == []
    with env.state.sessions() as session:
        claim = session.scalars(select(AssistantRequestDispatch)).one()
        assert claim.status not in {"external_started", "succeeded"}


def test_unknown_external_outcome_is_not_replayed_or_replanned(request_env, monkeypatch):
    env = request_env
    _plan, receipt, job_id, payload = apply_send(env)
    calls = []

    def uncertain(cfg, tmdb, entry, batch=None):
        calls.append(entry)
        assert entry["candidate_id"] == 1
        assert entry["title"]["tmdb_id"] == 101
        assert cfg.radarr.url == "http://radarr.test"
        assert cfg.radarr.api_key == "private-request-key"
        raise TimeoutError("vendor may have accepted the request")

    monkeypatch.setattr("shortlist.server.assistant.request_adapter._send_once", uncertain)
    dispatch(env, job_id, payload)
    dispatch(env, job_id, payload)
    assert len(calls) == 1
    with env.state.sessions() as session:
        assert session.scalars(select(AssistantRequestDispatch)).one().status == "outcome_unknown"
    assert env.service.get_operation(env.principal, receipt["operation_id"])["status"] == "outcome_unknown"
    with pytest.raises((ChangeError, ValueError)):
        prepare_send(env)


def test_archiving_and_recreating_candidate_cannot_bypass_unknown_claim(request_env, monkeypatch):
    env = request_env
    _plan, _receipt, job_id, payload = apply_send(env)

    def uncertain(*args):
        raise TimeoutError("unknown acceptance")

    monkeypatch.setattr("shortlist.server.assistant.request_adapter._send_once", uncertain)
    dispatch(env, job_id, payload)
    with env.state.sessions() as session:
        old = session.get(RequestCandidate, 1)
        tmdb_id, media_type = old.tmdb_id, old.media_type
        session.delete(old)
        session.flush()
        replacement = candidate(2)
        replacement.tmdb_id, replacement.media_type, replacement.row_slug = tmdb_id, media_type, "movies"
        session.add(replacement)
        session.commit()
    with pytest.raises((ChangeError, ValueError)):
        env.service.prepare(env.principal, "requests", {"action": "send", "candidate_ids": [2]})


def test_disabling_assistant_stops_reserved_outbound_work(request_env, monkeypatch):
    env = request_env
    _plan, _receipt, job_id, payload = apply_send(env)
    env.state.assistant_auth = None
    monkeypatch.setattr(
        "shortlist.server.assistant.request_adapter._send_once", lambda *args: pytest.fail("disabled send")
    )
    dispatch(env, job_id, payload)
    with env.state.sessions() as session:
        assert session.scalars(select(AssistantRequestDispatch)).one().status == "refused"
