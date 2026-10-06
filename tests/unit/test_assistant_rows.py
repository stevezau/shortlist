"""Assistant row intents are strict, catalog-backed and transaction-owned."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shortlist.server.assistant.row_adapter import RowAdapter, RowIntent
from shortlist.server.db.models import Base, Collection


def test_row_intent_rejects_ambiguous_or_unknown_shapes():
    with pytest.raises(ValidationError):
        RowIntent.model_validate({"action": "delete", "row_id": 1, "values": {"enabled": False}})
    with pytest.raises(ValidationError):
        RowIntent.model_validate({"action": "create", "template_id": "picked-for-you", "values": {"mystery": 1}})


def test_catalog_create_defaults_disabled_and_is_rollback_safe():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    adapter = RowAdapter(SimpleNamespace(secrets=None))
    intent = {"action": "create", "template_id": "picked-for-you", "values": {"name": "Quiet picks"}}
    with Session(engine) as session:
        plan = adapter.prepare(session, intent)
        assert plan.normalized_intent["values"]["enabled"] is False
        assert "rows.create" in plan.requirements.capabilities
        assert session.query(Collection).count() == 0
        result = adapter.apply(session, plan.normalized_intent)
        assert session.get(Collection, result.created_row_ids[0]).enabled is False
        session.rollback()
    with Session(engine) as session:
        assert session.query(Collection).count() == 0
    engine.dispose()


def test_delete_declares_exact_row_and_ordered_cleanup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    adapter = RowAdapter(SimpleNamespace(secrets=None))
    with Session(engine) as session:
        row = Collection(slug="shared-picks", name="Shared picks", build="shared")
        session.add(row)
        session.commit()
        plan = adapter.prepare(session, {"action": "delete", "row_id": row.id})
        assert plan.requirements.row_ids == (row.id,)
        assert {"rows.delete", "runs.execute", "audiences.write"} <= set(plan.requirements.capabilities)
        assert [step["kind"] for step in plan.effects[0].payload["steps"]] == [
            "row.reconcile",
            "privacy.sync",
            "schedule.rebuild",
        ]
    engine.dispose()


@pytest.fixture
def recurring_row():
    from shortlist.server.settings_store import SettingsStore

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        row = Collection(slug="paid-picks", name="Paid picks", enabled=True, candidate_sources=["llm_web"])
        session.add(row)
        store = SettingsStore(session)
        store.set_in_transaction("curator.provider", "openai")
        session.commit()
        yield session, row, RowAdapter(SimpleNamespace(secrets=None)), store
    engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"schedule": "0 5 * * *"},
        {"size": 12},
        {"refresh_days": 2},
        {"ai_instructions": {"mode": "add", "text": "Prefer gentle comedy"}},
        {"poster": {"mode": "ai"}},
        {"req_auto_min_rating": 8.5},
    ],
)
def test_enabled_paid_row_input_changes_require_exact_review(recurring_row, values):
    session, row, adapter, _store = recurring_row
    plan = adapter.prepare(session, {"action": "update", "row_id": row.id, "values": values})
    assert plan.requirements.requires_approval
    assert {"ai.generate", "history.use", "history.providers"} <= set(plan.requirements.capabilities)
    assert "https://api.openai.com/v1" in plan.requirements.destination_ids
    assert not session.dirty


def test_recurring_request_threshold_declares_actual_destination(recurring_row):
    session, row, adapter, store = recurring_row
    row.candidate_sources = ["tmdb_similar"]
    for key, value in {
        "requests.enabled": True,
        "requests.auto_send": True,
        "requests.target": "arr",
        "requests.radarr.url": "http://radarr.test",
    }.items():
        store.set_in_transaction(key, value)
    session.commit()
    plan = adapter.prepare(session, {"action": "update", "row_id": row.id, "values": {"req_auto_min_rating": 8.5}})
    assert plan.requirements.requires_approval
    assert "requests.send" in plan.requirements.capabilities
    assert plan.requirements.destination_ids == ("http://radarr.test",)


def test_disable_paid_row_does_not_require_new_spend_approval(recurring_row):
    session, row, adapter, _store = recurring_row
    plan = adapter.prepare(session, {"action": "update", "row_id": row.id, "values": {"enabled": False}})
    assert not plan.requirements.requires_approval
    assert "ai.generate" not in plan.requirements.capabilities


def test_explicit_pause_keeps_saved_picks_and_resume_requires_review(recurring_row):
    from shortlist.server.db.models import Theme

    session, row, adapter, _store = recurring_row
    theme = Theme(slug="fixed", name="Fixed")
    session.add(theme)
    session.flush()
    row.theme_id, row.candidate_sources = theme.id, ["tmdb_similar"]
    session.commit()
    pause = adapter.prepare(session, {"action": "update", "row_id": row.id, "values": {"ai_paused": True}})
    assert not pause.requirements.requires_approval and pause.effects == ()
    assert not session.dirty
    adapter.apply(session, pause.normalized_intent)
    assert row.ai_paused and row.theme_id == theme.id
    session.commit()
    cached = adapter.prepare(session, {"action": "update", "row_id": row.id, "values": {"schedule": "0 5 * * *"}})
    assert not cached.requirements.requires_approval
    resume = adapter.prepare(session, {"action": "update", "row_id": row.id, "values": {"ai_paused": False}})
    assert resume.requirements.requires_approval
    assert "https://api.openai.com/v1" in resume.requirements.destination_ids


def test_pause_is_strict_and_only_valid_on_themed_rows(recurring_row):
    session, row, adapter, _store = recurring_row
    with pytest.raises(ValidationError):
        RowIntent(action="update", row_id=row.id, values={"ai_paused": "false"})
    with pytest.raises(ValueError, match="Only an AI row"):
        adapter.prepare(session, {"action": "update", "row_id": row.id, "values": {"ai_paused": True}})
