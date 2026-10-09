"""Catalog-backed settings reject unclassified inputs and describe durable effects."""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shortlist.server.assistant.settings_adapter import SettingsAdapter, SettingsIntent, validate_assistant_values
from shortlist.server.db.models import Setting
from shortlist.server.services.settings_mutations import apply_settings_in_session, prepare_settings_in_session
from tests.db_helpers import create_schema, disposing_engine


@pytest.mark.parametrize(
    "values",
    [
        {"api.token": "secret"},
        {"tmdb.apikey": "secret"},
        {"new.unknown": True},
        {"row.size": True},
        {"row.size": "20"},
        {"requests.target": "arbitrary"},
    ],
)
def test_unknown_secret_or_wrong_typed_fields_are_refused(values):
    with pytest.raises(ValueError):
        validate_assistant_values(SettingsIntent(values=values))


def test_no_setting_commits_before_its_owed_effects():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        with Session(engine) as session:
            mutation = prepare_settings_in_session(session, None, {"privacy.hide_shared_from_disabled": False})
            assert any(step["kind"] == "privacy.sync" for step in mutation.steps)
            apply_settings_in_session(session, None, mutation)
            session.rollback()
        with Session(engine) as session:
            assert session.get(Setting, "privacy.hide_shared_from_disabled") is None


def test_one_invalid_field_prevents_every_write():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        with Session(engine) as session:
            with pytest.raises(HTTPException):
                prepare_settings_in_session(session, None, {"row.size": 20, "unclassified": 1})
            assert session.get(Setting, "row.size") is None


def test_provider_configuration_requires_explicit_recurring_review():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        with Session(engine) as session:
            plan = SettingsAdapter(None).prepare(session, {"values": {"curator.model": "reviewed-model"}})
            assert plan.requirements.requires_approval
            assert plan.summary["recurring_external_effects"]
            assert "does not cap" in plan.summary["quota_policy"]


def test_assistant_service_url_cannot_smuggle_credentials():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        with Session(engine) as session:
            with pytest.raises((ValueError, HTTPException)):
                SettingsAdapter(None).prepare(
                    session, {"values": {"requests.radarr.url": "https://example.test/?apikey=secret"}}
                )
            assert session.get(Setting, "requests.radarr.url") is None
