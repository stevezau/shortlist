"""Shared owner/MCP semantics for one person's per-row preferences."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.people_seasons import PeopleAdapter, PeopleIntent
from shortlist.server.db.models import Base, Collection, CollectionAudience, CollectionUserOverride, Setting, User
from shortlist.server.services.person_row_overrides import (
    RowOverridePatch,
    apply_person_row_override_in_session,
    prepare_person_row_override_in_session,
    read_person_row_override_in_session,
)
from tests.db_helpers import disposing_engine


@pytest.fixture
def sessions():
    with _seed_sessions() as (_engine, factory):
        yield factory


@contextmanager
def _seed_sessions() -> Iterator[tuple[Engine, sessionmaker]]:
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        with factory() as session:
            session.add_all(
                [
                    Setting(key="row.size", value={"v": 15}),
                    Setting(key="recommendations.recent_count", value={"v": 10}),
                    # Disabled people and rows remain configurable before either is activated.
                    User(id=1, plex_account_id=11, username="Allowed", slug="allowed", enabled=False),
                    User(id=2, plex_account_id=22, username="Other", slug="other"),
                    # The default row's stored size deliberately differs from its global effective size.
                    Collection(id=1, slug="picked", name="Picked", build="per_person", size=30, recent_count=None),
                    Collection(
                        id=2,
                        slug="disabled",  # Disabled rows can be configured before activation.
                        name="Disabled",
                        build="per_person",
                        enabled=False,
                        audience="subset",
                        library_keys=["2"],
                        size=22,
                        recent_count=7,
                    ),
                    Collection(id=3, slug="shared", name="Shared", build="shared", library_keys=["1"]),
                ]
            )
            session.flush()
            session.add(CollectionAudience(collection_id=2, user_id=1))
            session.add(CollectionUserOverride(collection_id=1, user_id=1, muted=False, row_size=20, recent_count=4))
            session.commit()
        yield engine, factory


def test_explicit_null_clears_only_that_numeric_override_and_omitted_values_survive(sessions):
    with sessions() as session:
        patch = RowOverridePatch(row_size=None)
        mutation = prepare_person_row_override_in_session(session, 1, 1, patch)

        assert mutation.changed == {"row_size": {"before": 20, "after": None}}
        assert mutation.steps == ()
        apply_person_row_override_in_session(session, mutation)
        override = session.get(CollectionUserOverride, (1, 1))
        assert override.row_size is None
        assert override.recent_count == 4
        assert override.muted is False


def test_reader_uses_engine_default_row_and_global_inheritance_precedence(sessions):
    with sessions() as session:
        result = read_person_row_override_in_session(session, 1, 1, secrets=None)
        assert result["supported"] is True
        assert result["stored"] == {"muted": False, "row_size": 20, "recent_count": 4}
        assert result["effective"] == {
            "muted": False,
            "row_size": 20,
            "recent_count": 4,
            "base_row_size": 15,
            "base_recent_count": 10,
        }

        mutation = prepare_person_row_override_in_session(
            session, 1, 1, RowOverridePatch(row_size=None, recent_count=None)
        )
        apply_person_row_override_in_session(session, mutation)
        result = read_person_row_override_in_session(session, 1, 1, secrets=None)
        assert result["stored"] == {"muted": False, "row_size": None, "recent_count": None}
        assert result["effective"]["row_size"] == result["effective"]["base_row_size"] == 15
        assert result["effective"]["recent_count"] == result["effective"]["base_recent_count"] == 10


def test_disabled_but_configured_member_row_is_valid_and_muting_declares_narrow_cleanup(sessions):
    with sessions() as session:
        mutation = prepare_person_row_override_in_session(session, 1, 2, RowOverridePatch(muted=True))

        assert mutation.changed == {"muted": {"before": False, "after": True}}
        assert mutation.steps == (
            {
                "kind": "row.reconcile",
                "payload": {
                    "slug": "disabled",
                    "build": "per_person",
                    "only_user_ids": [1],
                    "dry_run": False,
                    "template": None,
                    "in_sections": None,
                    "scope": "user.row_override.mute",
                },
            },
        )


@pytest.mark.parametrize("person_id,row_id", [(2, 2), (1, 3)])
def test_rejects_rows_outside_person_audience_or_non_per_person(sessions, person_id, row_id):
    with sessions() as session, pytest.raises(ValueError, match="row override"):
        prepare_person_row_override_in_session(session, person_id, row_id, RowOverridePatch(muted=True))


def test_legacy_shared_override_is_read_only_to_mcp_but_owner_api_can_clear_it(sessions):
    with sessions() as session:
        session.add(CollectionUserOverride(collection_id=3, user_id=1, muted=True, row_size=20, recent_count=4))
        session.flush()

        result = read_person_row_override_in_session(session, 1, 3, secrets=None)
        assert result == {
            "supported": False,
            "stored": {"muted": True, "row_size": 20, "recent_count": 4},
            "effective": None,
            "warning": "A legacy shared-row override is read-only here; new assistant plans cannot modify it.",
        }
        with pytest.raises(ValueError, match="per-person"):
            prepare_person_row_override_in_session(session, 1, 3, RowOverridePatch(muted=False))

        mutation = prepare_person_row_override_in_session(
            session,
            1,
            3,
            RowOverridePatch(muted=False, row_size=None, recent_count=None),
            allow_legacy_shared=True,
        )
        apply_person_row_override_in_session(session, mutation)
        override = session.get(CollectionUserOverride, (3, 1))
        assert (override.muted, override.row_size, override.recent_count) == (False, None, None)


def test_people_plan_scopes_only_overridden_rows_and_fingerprints_override_state(sessions):
    adapter = PeopleAdapter()
    with sessions() as session:
        intent = PeopleIntent.model_validate(
            {"person_id": 1, "patch": {}, "row_overrides": [{"row_id": 2, "recent_count": 5}]}
        )
        first = adapter.prepare(session, intent.model_dump(mode="json", exclude_unset=True))

        assert first.requirements.row_ids == (2,)
        assert first.requirements.library_keys == ("2",)
        assert first.requirements.person_ids == (1,)
        assert first.requirements.capabilities == ("people.write", "rows.update")
        assert first.effects == ()

        session.add(CollectionUserOverride(collection_id=2, user_id=1, recent_count=9))
        session.flush()
        second = adapter.prepare(session, intent.model_dump(mode="json", exclude_unset=True))
        assert first.dependencies["collection_user_overrides"] != second.dependencies["collection_user_overrides"]


def test_people_plan_requires_runs_execute_only_for_immediate_mute_cleanup(sessions):
    adapter = PeopleAdapter()
    with sessions() as session:
        intent = PeopleIntent.model_validate(
            {"person_id": 1, "patch": {}, "row_overrides": [{"row_id": 2, "muted": True}]}
        )
        plan = adapter.prepare(session, intent.model_dump(mode="json", exclude_unset=True))

        assert plan.requirements.capabilities == ("people.write", "rows.update", "runs.execute")
        assert plan.effects[0].kind == "assistant.converge"
        assert plan.effects[0].payload["steps"][0]["payload"]["only_user_ids"] == [1]


@settings(max_examples=40, deadline=None)
@given(
    supplied=st.sets(st.sampled_from(("muted", "row_size", "recent_count")), min_size=1),
    muted=st.one_of(st.none(), st.booleans()),
    row_size=st.one_of(st.none(), st.integers(min_value=5, max_value=40)),
    recent_count=st.one_of(st.none(), st.integers(min_value=1, max_value=25)),
)
def test_sparse_override_patch_preserves_omitted_values_and_declares_only_narrow_mute_cleanup(
    supplied, muted, row_size, recent_count
):
    """PATCH field presence, rather than defaults, decides stored inheritance and Plex work."""
    with _seed_sessions() as (_engine, sessions):
        payload = {
            name: {"muted": muted, "row_size": row_size, "recent_count": recent_count}[name] for name in supplied
        }
        with sessions() as session:
            mutation = prepare_person_row_override_in_session(session, 1, 1, RowOverridePatch.model_validate(payload))
            apply_person_row_override_in_session(session, mutation)
            override = session.get(CollectionUserOverride, (1, 1))

            expected = {"muted": False, "row_size": 20, "recent_count": 4}
            for name, value in payload.items():
                expected[name] = bool(value) if name == "muted" else value
            assert (override.muted, override.row_size, override.recent_count) == (
                expected["muted"],
                expected["row_size"],
                expected["recent_count"],
            )
            if payload.get("muted") is True:
                assert mutation.steps[0]["payload"]["only_user_ids"] == [1]
                assert len(mutation.steps) == 1
            else:
                assert mutation.steps == ()
