"""A saved theme and its disabled row form one reviewed configuration change."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from shortlist.server.assistant.setup_adapter import SetupAdapter, ThemeRowIntent
from shortlist.server.db.models import Collection, Theme
from tests.db_helpers import create_schema, disposing_engine


def intent():
    return {
        "theme": {"name": "Quiet comedy", "media": ["movie"], "genres": ["Comedy"]},
        "row": {"action": "create", "template_id": "picked-for-you", "values": {"name": "Quiet night"}},
    }


def test_setup_requires_new_inactive_unscheduled_row():
    for fields in [{"enabled": True}, {"schedule": "0 1 * * *"}, {"theme_id": 1}]:
        value = intent()
        value["row"]["values"].update(fields)
        with pytest.raises(ValidationError):
            ThemeRowIntent.model_validate(value)


def test_setup_prepare_is_pure_and_apply_rolls_back_both_objects():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        adapter = SetupAdapter(SimpleNamespace(secrets=None))
        with Session(engine) as session:
            plan = adapter.prepare(session, intent())
            assert not session.new and not session.dirty
            assert session.scalars(select(Theme)).all() == []
            assert session.scalars(select(Collection)).all() == []
            assert len(plan.effects) == 1
            assert [step["kind"] for step in plan.effects[0].payload["steps"]] == ["schedule.rebuild"]
            result = adapter.apply(session, plan.normalized_intent)
            row = session.get(Collection, result.result["row_id"])
            assert row.theme_id == result.result["theme_id"]
            assert row.enabled is False and row.schedule == ""
            assert result.created_row_ids == (row.id,)
            session.rollback()
        with Session(engine) as session:
            assert session.scalars(select(Theme)).all() == []
            assert session.scalars(select(Collection)).all() == []


def test_setup_can_pause_future_topups_atomically_with_saved_theme():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        adapter = SetupAdapter(SimpleNamespace(secrets=None))
        definition = intent()
        definition["row"]["values"]["ai_paused"] = True
        with Session(engine) as session:
            plan = adapter.prepare(session, definition)
            assert not session.new and not session.dirty
            result = adapter.apply(session, plan.normalized_intent)
            row = session.get(Collection, result.result["row_id"])
            assert row.ai_paused and row.theme_id == result.result["theme_id"]
            session.rollback()
