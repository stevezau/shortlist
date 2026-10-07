"""Shared mutations preserve owner behavior and leave effects in the caller's transaction."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from shortlist.server.api.users import UserPatch
from shortlist.server.db.models import Base, Job, User
from shortlist.server.services.person_changes import apply_person_in_session, prepare_person_in_session
from tests.db_helpers import disposing_engine


@pytest.fixture
def people_db(tmp_path):
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'people.db'}")) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        with sessions() as session:
            session.add(User(id=1, plex_account_id=11, username="alice", slug="alice", enabled=True))
            session.add(User(id=2, plex_account_id=22, username="owner", slug="owner", user_type="owner"))
            session.commit()
        yield SimpleNamespace(sessions=sessions)


def test_person_projection_is_pure_and_orders_cleanup_before_privacy(people_db):
    with people_db.sessions() as session:
        mutation = prepare_person_in_session(session, 1, UserPatch(enabled=False, prefs={"paused": True}))
        assert session.get(User, 1).enabled is True
        assert not session.dirty
        assert [step["kind"] for step in mutation.steps] == ["user.cleanup", "privacy.sync", "user.hide"]


def test_owner_share_management_flag_is_ignored(people_db):
    with people_db.sessions() as session:
        mutation = prepare_person_in_session(session, 2, UserPatch(manage_sharing=False))
        apply_person_in_session(session, mutation)
        assert session.get(User, 2).manage_sharing is True
        assert mutation.steps == ()


def test_nickname_conflict_does_not_write(people_db):
    with people_db.sessions() as session:
        with pytest.raises(Exception) as raised:
            prepare_person_in_session(session, 1, UserPatch(nickname="OWNER", enabled=False))
        assert raised.value.status_code == 409
        assert session.get(User, 1).enabled is True
        assert not session.dirty


def test_person_config_and_convergence_rollback_together(people_db):
    from shortlist.server.assistant.row_effects import queue_convergence_in_session

    with people_db.sessions() as session:
        mutation = prepare_person_in_session(session, 1, UserPatch(enabled=False))
        apply_person_in_session(session, mutation)
        queue_convergence_in_session(session, mutation.steps, domain="people")
        session.rollback()
    with people_db.sessions() as session:
        assert session.get(User, 1).enabled is True
        assert session.scalars(select(Job)).all() == []


def test_person_adapter_does_not_disclose_saved_seed_titles(people_db):
    from shortlist.server.assistant.people_seasons import PeopleAdapter

    with people_db.sessions() as session:
        session.get(User, 1).prefs = {"blocked_seeds": [{"tmdb_id": 42, "title": "Private watched title"}]}
        session.commit()
        plan = PeopleAdapter().prepare(session, {"person_id": 1, "patch": {"prefs": {"paused": True}}})
        assert "Private watched title" not in str(plan.summary)
        assert plan.summary["configuration_diff"]["prefs"] == {"changed_fields": ["paused"]}
        assert plan.requirements.person_ids == (1,)
        assert not session.dirty


def test_assistant_nested_unknown_fields_are_rejected():
    from pydantic import ValidationError

    from shortlist.server.assistant.people_seasons import PeopleIntent, SeasonsIntent

    with pytest.raises(ValidationError):
        PeopleIntent.model_validate({"person_id": 1, "patch": {"prefs": {"parental_controls": "off"}}})
    with pytest.raises(ValidationError):
        SeasonsIntent.model_validate(
            {
                "action": "create",
                "definition": {
                    "name": "Festival",
                    "emoji": "🎬",
                    "rule": {"kind": "month", "month": 10, "unexpected": True},
                    "genre": 28,
                },
            }
        )


def test_season_create_is_pure_then_rolls_back(people_db):
    from shortlist.server.api.seasons import SeasonIn
    from shortlist.server.db.models import SeasonDef
    from shortlist.server.services.season_changes import apply_season_in_session, prepare_season_in_session

    with people_db.sessions() as session:
        body = SeasonIn(name="Festival", emoji="🎬", rule={"kind": "month", "month": 10}, genre=28)
        mutation = prepare_season_in_session(session, "create", body=body)
        assert session.scalars(select(SeasonDef)).all() == []
        assert not session.new
        row = apply_season_in_session(session, SimpleNamespace(secrets=None), mutation)
        assert row.slug == "festival"
        assert (row.lead_days, row.after_days) == (0, 0)
        session.rollback()
    with people_db.sessions() as session:
        assert session.scalars(select(SeasonDef)).all() == []


def test_season_delete_preserves_only_season_guard(people_db):
    from fastapi import HTTPException

    from shortlist.server.api.seasons import SeasonIn
    from shortlist.server.db.models import Collection, SeasonDef
    from shortlist.server.services.season_changes import apply_season_in_session, prepare_season_in_session

    with people_db.sessions() as session:
        mutation = prepare_season_in_session(
            session,
            "create",
            body=SeasonIn(
                name="Festival",
                emoji="🎬",
                rule={"kind": "month", "month": 10},
                genre=28,
            ),
        )
        apply_season_in_session(session, SimpleNamespace(secrets=None), mutation)
        session.add(Collection(slug="festival-row", name="Festival row", seasons=["festival"]))
        session.commit()
        with pytest.raises(HTTPException) as raised:
            prepare_season_in_session(session, "delete", slug="festival")
        assert raised.value.status_code == 409
        assert session.scalar(select(SeasonDef.slug)) == "festival"
        assert not session.dirty
