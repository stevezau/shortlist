"""One setup proposal atomically owns its theme, row, receipt and owed scheduler work."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.changes import ChangeError, ChangeService
from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.assistant.setup_adapter import SetupAdapter
from shortlist.server.assistant_auth import GrantConstraints, GrantPreset
from shortlist.server.assistant_auth.credentials import CredentialHasher
from shortlist.server.assistant_auth.repository import AssistantAuthRepository
from shortlist.server.db.models import Base, Collection, Job, Server, Theme, User
from shortlist.server.services.secrets import SecretBox


@pytest.fixture
def setup_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'setup.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        session.add(Server(machine_id="setup", url="http://unused.invalid", token_enc="unused", owner_account_id=42))
        session.add(User(id=1, username="recipient", slug="recipient", plex_account_id=1))
        session.commit()
    repository = AssistantAuthRepository(sessions, CredentialHasher(b"setup-transaction-key-00000000000"))
    principal = repository.create_grant(
        owner_account_id=42,
        client_id="setup-test",
        name="Setup test",
        preset=GrantPreset.OWNER_AUTOMATION,
        constraints=GrantConstraints(person_ids=frozenset({1}), library_keys=frozenset({"1"})),
    )
    state = SimpleNamespace(sessions=sessions, secrets=SecretBox(tmp_path))
    adapter = SetupAdapter(state)
    service = ChangeService(sessions, {"setup": adapter})
    intent = {
        "theme": {"name": "Quiet comedy", "media": ["movie"], "genres": ["Comedy"]},
        "row": {
            "action": "create",
            "template_id": "picked-for-you",
            "values": {"name": "Quiet night", "audience": "subset", "audience_user_ids": [1], "library_keys": ["1"]},
        },
    }
    yield SimpleNamespace(
        sessions=sessions, repository=repository, principal=principal, adapter=adapter, service=service, intent=intent
    )
    engine.dispose()


def test_setup_prepare_then_apply_and_replay_owns_exactly_one_theme_and_row(setup_env):
    env = setup_env
    plan = env.service.prepare(env.principal, "setup", env.intent)
    assert plan["authorization"]["can_apply"] is True
    with env.sessions() as session:
        assert list(session.scalars(select(Theme))) == []
        assert list(session.scalars(select(Collection))) == []
        assert list(session.scalars(select(Job))) == []
    receipt = env.service.apply(env.principal, plan["change_id"], "setup-once")
    current = env.repository.get_grant_context(env.principal.grant_id)
    assert env.service.apply(current, plan["change_id"], "setup-replay") == receipt
    with env.sessions() as session:
        theme = session.scalars(select(Theme)).one()
        row = session.scalars(select(Collection)).one()
        assert row.theme_id == theme.id == receipt["result"]["theme_id"]
        assert row.id == receipt["result"]["row_id"]
        assert not row.enabled and row.schedule == ""
        job = session.scalars(select(Job)).one()
        assert job.operation_id == receipt["operation_id"]
        assert [step["kind"] for step in job.payload["steps"]] == ["schedule.rebuild"]
        assert row.id in current.constraints.row_ids
        assert len(list(session.scalars(select(AssistantOperation)))) == 1


def test_setup_rolls_back_first_write_when_second_write_fails(setup_env, monkeypatch):
    env = setup_env
    plan = env.service.prepare(env.principal, "setup", env.intent)
    create_row = env.adapter.rows.apply

    def fail_after_row_insert(session, intent):
        create_row(session, intent)
        assert session.scalar(select(Theme.id)) is not None
        assert session.scalar(select(Collection.id)) is not None
        raise RuntimeError("injected second write failure")

    monkeypatch.setattr(env.adapter.rows, "apply", fail_after_row_insert)
    with pytest.raises(RuntimeError, match="second write"):
        env.service.apply(env.principal, plan["change_id"], "setup-failure")
    with env.sessions() as session:
        assert list(session.scalars(select(Theme))) == []
        assert list(session.scalars(select(Collection))) == []
        assert list(session.scalars(select(Job))) == []
        assert list(session.scalars(select(AssistantOperation))) == []
        assert session.get(AssistantChange, plan["change_id"]).operation_id is None
    assert env.repository.get_grant_context(env.principal.grant_id).revision == 1


def test_setup_stale_dependencies_refuse_both_writes(setup_env):
    env = setup_env
    plan = env.service.prepare(env.principal, "setup", env.intent)
    with env.sessions() as session:
        session.get(User, 1).nickname = "Changed display name"
        session.commit()
    with pytest.raises(ChangeError, match="changed"):
        env.service.apply(env.principal, plan["change_id"], "stale-setup")
    with env.sessions() as session:
        assert list(session.scalars(select(Theme))) == []
        assert list(session.scalars(select(Collection))) == []
        assert list(session.scalars(select(Job))) == []
