"""Saved Explore selections use MCP review, rotation locks, and the owner's queue mutation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Lock

import pytest
from sqlalchemy import select, text

from shortlist.server.assistant_auth import Capability
from shortlist.server.db.models import Collection, CollectionAudience, Theme, ThemeHistory, User
from shortlist.server.services.theme_rotation import queue_next
from tests.integration.test_assistant_mcp_tool_matrix import _apply, _approve_owner_change, _wire_app

pytestmark = pytest.mark.integration


def _call(wire, name, body):
    return wire.exercise([(name, body)])[name]["data"]


def _explore(app):
    with app.state.sessions() as session:
        row = session.get(Collection, 10)
        row.theme_mode = "explore"
        row.ai_paused = True
        person_id = session.scalar(select(CollectionAudience.user_id).where(CollectionAudience.collection_id == 10))
        session.add(Theme(id=21, slug="saved-next", name="Saved next", media=["movie"], genres=["Drama"]))
        session.commit()
        return person_id


def _request(person_id):
    return {"person_id": person_id, "patch": {}, "row_overrides": [{"row_id": 10, "up_next_theme_id": 21}]}


def test_sdk_saved_up_next_replaces_queue_without_generation_promotion_or_plex_writes(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, state):
        person_id = _explore(app)
        with app.state.sessions() as session:
            row = session.get(Collection, 10)
            queue_next(session, row, person_id, session.get(Theme, 20), datetime.now(UTC), secrets=app.state.secrets)
            session.commit()
        plex_before = deepcopy(state.collections)
        plan = _call(wire, "shortlist_plan_people", _request(person_id))
        assert plan["authorization"]["can_apply"] is True
        assert {"people.write", "rows.update", "themes.write"} <= set(plan["required_capabilities"])
        assert not {"ai.generate", "runs.execute"} & set(plan["required_capabilities"])
        applied = _apply(wire, plan["change_id"], "saved-up-next")
        assert applied["status"] == "completed" and applied["job_ids"] == []
        viewed = _call(wire, "shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})
        assert viewed["up_next"]["theme_id"] == 21
        assert viewed["up_next"]["name"] == "Saved next"
        with app.state.sessions() as session:
            history = session.scalars(
                select(ThemeHistory).where(
                    ThemeHistory.collection_id == 10,
                    ThemeHistory.user_id == person_id,
                )
            ).all()
            assert [(item.state, item.theme_id) for item in history] == [("next", 21)]
            assert session.get(Collection, 10).theme_id == 20
        assert state.collections == plex_before
        assert app.state.matrix_provider_calls == []


@pytest.mark.parametrize("changed", ["theme", "queue", "audience"])
def test_sdk_up_next_stale_source_or_target_cannot_overwrite_newer_state(tmp_path, monkeypatch, changed):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        person_id = _explore(app)
        plan = _call(wire, "shortlist_plan_people", _request(person_id))
        with app.state.sessions() as session:
            if changed == "theme":
                session.get(Theme, 21).genres = ["Comedy"]
            elif changed == "queue":
                queue_next(
                    session,
                    session.get(Collection, 10),
                    person_id,
                    session.get(Theme, 20),
                    datetime.now(UTC),
                    secrets=app.state.secrets,
                )
            else:
                session.delete(session.get(CollectionAudience, (10, person_id)))
            session.commit()
        wire.expect_error(
            "shortlist_apply_change",
            {
                "change_id": plan["change_id"],
                "idempotency_key": "saved-up-next-stale",
            },
        )
        with app.state.sessions() as session:
            assert session.scalar(select(ThemeHistory.id).where(ThemeHistory.theme_id == 21)) is None


@pytest.mark.parametrize("denial", ["capability", "row", "library"])
def test_sdk_up_next_requires_standing_authority_and_exact_approval_does_not_grant_history_reads(
    tmp_path,
    monkeypatch,
    denial,
):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        person_id = _explore(app)
        if denial == "library":
            with app.state.sessions() as session:
                session.get(Theme, 21).collections = [
                    {
                        "section_key": "2",
                        "section_title": "PRIVATE_LIBRARY",
                        "title": "PRIVATE_COLLECTION",
                    }
                ]
                session.commit()
        repository = app.state.assistant_auth.repository
        grant = repository.find_grant_for_client("mcp-wire-matrix")
        caps = grant.capabilities - {Capability.HISTORY_EXPORT}
        if denial == "capability":
            caps -= {Capability.THEMES_WRITE}
        constraints = replace(
            grant.constraints,
            include_future_rows=denial != "row",
            row_ids=frozenset(),
            include_future_libraries=denial != "library",
            library_keys=frozenset({"1"}),
        )
        repository.replace_grant_authority(
            grant.grant_id, capabilities=caps, constraints=constraints, expected_revision=grant.revision
        )
        plan = _call(wire, "shortlist_plan_people", _request(person_id))
        assert plan["authorization"]["can_apply"] is False
        assert "PRIVATE_" not in str(plan)
        wire.expect_error(
            "shortlist_apply_change",
            {
                "change_id": plan["change_id"],
                "idempotency_key": "saved-up-next-denied",
            },
            code="missing_permission",
        )
        _approve_owner_change(wire, app, plan["change_id"])
        _apply(wire, plan["change_id"], "saved-up-next-approved")
        if denial == "row":
            wire.expect_error("shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})
        else:
            viewed = _call(wire, "shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})
            assert "up_next" not in viewed
            assert "up_next" in viewed["redacted_fields"]
            assert "Saved next" not in str(viewed) and "PRIVATE_" not in str(viewed)
        assert repository.get_grant_context(grant.grant_id).capabilities == caps


def test_sdk_up_next_ignores_unrelated_theme_and_other_person_rotation(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        person_id = _explore(app)
        with app.state.sessions() as session:
            other_id = session.scalar(select(User.id).where(User.id != person_id))
            session.add(
                Theme(id=22, slug="unrelated-source", name="Unrelated source", media=["movie"], genres=["Drama"])
            )
            session.flush()
            session.add(
                Collection(
                    id=11,
                    slug="other-explore",
                    name="Other Explore",
                    theme_id=22,
                    theme_mode="explore",
                    audience="subset",
                    library_keys=["1"],
                    enabled=False,
                )
            )
            session.flush()
            session.add(CollectionAudience(collection_id=11, user_id=other_id))
            session.commit()
        plan = _call(wire, "shortlist_plan_people", _request(person_id))
        with app.state.sessions() as session:
            theme = session.get(Theme, 22)
            theme.genres = ["Comedy"]
            queue_next(
                session, session.get(Collection, 11), other_id, theme, datetime.now(UTC), secrets=app.state.secrets
            )
            session.commit()
        applied = _apply(wire, plan["change_id"], "saved-next-unrelated")
        assert applied["status"] == "completed"
        viewed = _call(wire, "shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})
        assert viewed["up_next"]["theme_id"] == 21


@pytest.mark.parametrize("shared_theme_id", [20, 21])
def test_sdk_up_next_ignores_other_person_history_for_same_source(tmp_path, monkeypatch, shared_theme_id):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        person_id = _explore(app)
        with app.state.sessions() as session:
            other_id = session.scalar(select(User.id).where(User.id != person_id))
            session.add(CollectionAudience(collection_id=10, user_id=other_id))
            session.commit()
        plan = _call(wire, "shortlist_plan_people", _request(person_id))
        with app.state.sessions() as session:
            queue_next(
                session,
                session.get(Collection, 10),
                other_id,
                session.get(Theme, shared_theme_id),
                datetime.now(UTC),
                secrets=app.state.secrets,
            )
            session.commit()
        applied = _apply(wire, plan["change_id"], "saved-next-other-person-same-source")
        assert applied["status"] == "completed"
        with app.state.sessions() as session:
            queued = dict(
                session.execute(
                    select(ThemeHistory.user_id, ThemeHistory.theme_id).where(
                        ThemeHistory.collection_id == 10, ThemeHistory.state == "next"
                    )
                ).all()
            )
            assert queued == {person_id: 21, other_id: shared_theme_id}


def test_sdk_replacing_private_queue_needs_only_target_write_authority(tmp_path, monkeypatch):
    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        person_id = _explore(app)
        with app.state.sessions() as session:
            session.add(
                Collection(
                    id=11,
                    slug="private-consumer",
                    name="PRIVATE_OTHER_ROW",
                    theme_id=20,
                    library_keys=["2"],
                    enabled=False,
                )
            )
            queue_next(
                session,
                session.get(Collection, 10),
                person_id,
                session.get(Theme, 20),
                datetime.now(UTC),
                secrets=app.state.secrets,
            )
            session.commit()
        repository = app.state.assistant_auth.repository
        grant = repository.find_grant_for_client("mcp-wire-matrix")
        caps = grant.capabilities - {Capability.HISTORY_EXPORT}
        constraints = replace(
            grant.constraints,
            include_future_rows=False,
            row_ids=frozenset({10}),
            include_future_libraries=False,
            library_keys=frozenset({"1"}),
        )
        repository.replace_grant_authority(
            grant.grant_id, capabilities=caps, constraints=constraints, expected_revision=grant.revision
        )
        plan = _call(wire, "shortlist_plan_people", _request(person_id))
        assert plan["authorization"]["can_apply"] is True
        assert "history.export" not in plan["required_capabilities"]
        assert "PRIVATE_" not in str(plan)
        applied = _apply(wire, plan["change_id"], "replace-private-next")
        assert applied["status"] == "completed" and applied["job_ids"] == []
        viewed = _call(wire, "shortlist_get_person_row_settings", {"person_id": person_id, "row_id": 10})
        assert "up_next" not in viewed and "up_next" in viewed["redacted_fields"]
        with app.state.sessions() as session:
            assert session.get(Collection, 11).theme_id == 20
            assert (
                session.scalar(
                    select(ThemeHistory.theme_id).where(
                        ThemeHistory.collection_id == 10,
                        ThemeHistory.user_id == person_id,
                        ThemeHistory.state == "next",
                    )
                )
                == 21
            )


@pytest.mark.parametrize("conflicting_write", [False, True])
def test_sdk_up_next_waits_without_sqlite_writer_and_rechecks_after_rotation_lock(
    tmp_path,
    monkeypatch,
    conflicting_write,
):
    from shortlist.server.services import theme_rotation

    with _wire_app(tmp_path, monkeypatch, configured_provider=False) as (wire, app, _state):
        person_id = _explore(app)
        plan = _call(wire, "shortlist_plan_people", _request(person_id))
        attempting = Event()
        mutex = Lock()

        class ObservedLock:
            def __enter__(self):
                attempting.set()
                mutex.acquire()
                return self

            def __exit__(self, *args):
                mutex.release()

        # Observe the actual mutex boundary, retaining real exclusion and real database transactions.
        monkeypatch.setitem(theme_rotation._TARGET_LOCKS, (10, person_id), ObservedLock())
        with ThreadPoolExecutor(max_workers=1) as pool:
            mutex.acquire()
            try:
                if conflicting_write:
                    future = pool.submit(
                        wire.expect_error,
                        "shortlist_apply_change",
                        {
                            "change_id": plan["change_id"],
                            "idempotency_key": "saved-next-lock-stale",
                        },
                        code="stale_plan",
                    )
                else:
                    future = pool.submit(_apply, wire, plan["change_id"], "saved-next-lock")
                assert attempting.wait(5), "apply never reached the rotation lock"
                with app.state.sessions() as session:
                    session.execute(text("BEGIN IMMEDIATE"))
                    if conflicting_write:
                        queue_next(
                            session,
                            session.get(Collection, 10),
                            person_id,
                            session.get(Theme, 20),
                            datetime.now(UTC),
                            secrets=app.state.secrets,
                        )
                    session.commit()
            finally:
                mutex.release()
            future.result(timeout=15)
        assert mutex.acquire(blocking=False), "apply leaked the target lock"
        mutex.release()
        with app.state.sessions() as session:
            next_theme = session.scalar(
                select(ThemeHistory.theme_id).where(
                    ThemeHistory.collection_id == 10,
                    ThemeHistory.user_id == person_id,
                    ThemeHistory.state == "next",
                )
            )
            assert next_theme == (20 if conflicting_write else 21)
