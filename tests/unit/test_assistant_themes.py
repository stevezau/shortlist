"""Externally authored themes cannot forge metadata or provider accounting."""

import time

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shortlist.server.assistant.theme_adapter import ThemeAdapter, ThemeIntent
from shortlist.server.db.models import CacheRow, Theme
from tests.db_helpers import create_schema, disposing_engine


def draft():
    return {
        "action": "create",
        "draft": {
            "name": "A short evening",
            "media": ["movie"],
            "picks": [{"tmdb_id": 12, "media": "movie", "title": "Unverified caller title"}],
        },
    }


def test_theme_tool_does_not_accept_claimed_costs():
    with pytest.raises(ValidationError):
        ThemeIntent.model_validate({**draft(), "tokens": 9999})


def test_theme_nested_fields_do_not_coerce_malformed_values():
    value = draft()
    value["draft"]["picks"][0]["tmdb_id"] = "12"
    with pytest.raises(ValidationError):
        ThemeIntent.model_validate(value)


def test_title_must_be_resolved_and_authoritative_metadata_wins():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        adapter = ThemeAdapter(None)
        with Session(engine) as session:
            with pytest.raises(ValueError, match="Resolve"):
                adapter.prepare(session, draft())
            session.add(
                CacheRow(
                    kind="assistant_titles",
                    key="movie:12",
                    expires_at=time.time() + 3600,
                    value={"tmdb_id": 12, "media": "movie", "title": "Verified title", "year": 2003},
                )
            )
            session.flush()
            plan = adapter.prepare(session, draft())
            assert "ai.generate" not in plan.requirements.capabilities
            result = adapter.apply(session, plan.normalized_intent)
            row = session.get(Theme, result.result["theme_id"])
            assert row.ai_tokens == 0
            assert row.origin == "assistant"
            assert row.picks[0]["title"] == "Verified title"
            session.rollback()
