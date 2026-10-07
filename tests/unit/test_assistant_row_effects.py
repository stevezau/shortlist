"""Closed, ordered and restart-safe assistant consequence jobs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from shortlist.server.assistant.row_effects import (
    convergence_effect,
    privacy_sync_step,
    queue_convergence_in_session,
    validate_convergence_steps,
)
from shortlist.server.db.models import Base, Job
from shortlist.server.services import jobs
from tests.db_helpers import disposing_engine


def test_convergence_steps_are_a_closed_discriminated_union():
    with pytest.raises(ValidationError):
        validate_convergence_steps([{"kind": "arbitrary.callable", "payload": {}}])
    with pytest.raises(ValidationError):
        validate_convergence_steps([{"kind": "privacy.sync", "payload": {"reason": "x", "extra": True}}])


def test_effect_and_queue_use_one_normalized_convergence_payload():
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        step = privacy_sync_step("the row audience changed")
        effect = convergence_effect([step], domain="rows", effect_key="row-12")
        assert effect.kind == "assistant.converge"
        assert effect.payload == {"domain": "rows", "steps": [step]}

        with Session(engine) as session:
            job = queue_convergence_in_session(
                session, [step], domain="rows", operation_id="op_test", effect_key="row-12"
            )
            assert job.kind == "assistant.converge"
            assert job.payload == effect.payload
            assert session.get(Job, job.id) is job
            session.rollback()


def test_retry_skips_checkpointed_steps(monkeypatch):
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        state = SimpleNamespace(sessions=sessions)
        ran: list[str] = []

        monkeypatch.setitem(jobs._HANDLERS, "privacy.sync", lambda state, payload: ran.append("privacy") or {"ok": 1})

        def fail_once(state, payload):
            ran.append("cleanup")
            raise RuntimeError("Plex is down")

        monkeypatch.setitem(jobs._HANDLERS, "user.cleanup", fail_once)
        payload = {
            "domain": "people",
            "steps": [privacy_sync_step("audience changed"), {"kind": "user.cleanup", "payload": {"slug": "sarah"}}],
        }
        with sessions() as session:
            job = Job(kind="assistant.converge", payload=payload, status="running")
            session.add(job)
            session.commit()
            job_id = job.id

        with pytest.raises(RuntimeError, match="Plex is down"):
            asyncio.run(jobs._HANDLERS["assistant.converge"](state, payload, job_id=job_id))
        with sessions() as session:
            saved = session.get(Job, job_id).payload
            assert saved["completed_steps"] == [0]

        monkeypatch.setitem(jobs._HANDLERS, "privacy.sync", lambda state, payload: pytest.fail("checkpoint replayed"))
        monkeypatch.setitem(
            jobs._HANDLERS, "user.cleanup", lambda state, payload: ran.append("cleanup retry") or {"ok": 2}
        )
        result = asyncio.run(jobs._HANDLERS["assistant.converge"](state, saved, job_id=job_id))
        assert result["completed_steps"] == 2
        assert ran == ["privacy", "cleanup", "cleanup retry"]
