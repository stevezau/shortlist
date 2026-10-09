"""Request actions stay bounded; acquisition attempts cross a durable one-shot boundary."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shortlist.engine.models import ArrTarget, RequestConfig
from shortlist.server.assistant.request_adapter import RequestAdapter, RequestIntent
from shortlist.server.db.models import Collection, RequestCandidate, User
from shortlist.server.services.request_actions import apply_request_action_in_session
from tests.db_helpers import create_schema, disposing_engine
from tests.unit.assistant_fixtures import candidate


def test_request_intent_rejects_duplicates_and_unbounded_batches():
    with pytest.raises(ValueError):
        RequestIntent(action="send", candidate_ids=[1, 1])
    with pytest.raises(ValueError):
        RequestIntent(action="send", candidate_ids=list(range(1, 27)))


def test_local_action_is_transaction_owned():
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        with Session(engine) as session:
            session.add(candidate())
            session.commit()
            assert apply_request_action_in_session(session, "reject", [1])["changed"] == 1
            assert session.get(RequestCandidate, 1).status == "rejected"
            session.rollback()
        with Session(engine) as session:
            assert session.get(RequestCandidate, 1).status == "pending"


def test_send_plan_freezes_destination_body_and_resource_scope(monkeypatch):
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        cfg = RequestConfig(
            enabled=True,
            target="arr",
            radarr=ArrTarget("http://radarr", "secret", 7, "/movies", "shortlist"),
        )
        state = SimpleNamespace(secrets=None)
        monkeypatch.setattr("shortlist.server.assistant.request_adapter._request_config", lambda state, session: cfg)
        adapter = RequestAdapter(state)
        with Session(engine) as session:
            person = User(slug="sarah", username="sarah", nickname="Sarah", plex_account_id=10)
            row = Collection(slug="movies", name="Movies", build="per_person")
            request = candidate()
            request.row_slug = "movies"
            session.add_all([person, row, request])
            session.commit()
            plan = adapter.prepare(session, {"action": "send", "candidate_ids": [request.id]})
            assert plan.requirements.capabilities == ("requests.send",)
            assert plan.requirements.row_ids == (row.id,)
            assert plan.requirements.person_ids == (person.id,)
            assert plan.requirements.destination_ids == ("http://radarr",)
            assert plan.effects[0].max_attempts == 1
            entry = plan.effects[0].payload["entries"][0]
            assert entry["target"] == {
                "destination": "http://radarr",
                "service": "radarr",
                "configured": True,
                "quality_profile_id": 7,
                "root_folder": "/movies",
                "tag": "shortlist",
                "monitor": None,
            }
            assert "secret" not in repr(entry)
