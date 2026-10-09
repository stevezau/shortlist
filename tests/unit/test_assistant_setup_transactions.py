"""One setup proposal atomically owns its theme, row, receipt and owed scheduler work."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition

import pytest
from sqlalchemy import select

from shortlist.server.assistant.changes import ChangeError
from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.db.models import Collection, Job, Theme, User
from tests.unit.assistant_fixtures import setup_env  # noqa: F401


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
