"""Retired assistant theme dispatch never starts another provider call."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shortlist.server.assistant.budgets import AssistantBudget
from shortlist.server.assistant.generation import dispatch_generation, provider_destination
from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.db.models import Base, Job
from tests.db_helpers import disposing_engine


def test_local_provider_destination_is_explicit_and_credential_free():
    store = SimpleNamespace(
        get=lambda key: {
            "curator.provider": "openai_compatible",
            "curator.openai_base_url": "http://localhost:1234/v1",
        }.get(key)
    )
    assert provider_destination(store) == "http://localhost:1234/v1"
    bad = SimpleNamespace(
        get=lambda key: {
            "curator.provider": "openai_compatible",
            "curator.openai_base_url": "https://user:secret@example.com/v1",
        }.get(key)
    )
    with pytest.raises(ValueError):
        provider_destination(bad)


@pytest.mark.parametrize(
    ("prior_stage", "expected_status"),
    [(None, "cancelled"), ("external_started", "outcome_unknown"), ("provider_returned", "outcome_unknown")],
)
def test_historical_queued_generation_never_replays_or_forgets_dispatch_evidence(
    tmp_path, monkeypatch, prior_stage, expected_status
):
    from shortlist.server.assistant import generation

    # A saved operation may have crossed the paid-call checkpoint before an upgrade.
    # The retired worker must preserve that uncertainty and the historical reservation.
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'retired-generation.db'}")) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        state = SimpleNamespace(sessions=sessions)
        now = datetime.now(UTC)
        with sessions() as session:
            session.add(
                AssistantChange(
                    id="change",
                    grant_id="grant",
                    owner_account_id=1,
                    client_id="client",
                    grant_revision=1,
                    kind="generation",
                    intent={},
                    dependencies={},
                    requirements={},
                    effects=[],
                    summary={},
                    content_hash="hash",
                    expires_at=now + timedelta(days=1),
                )
            )
            session.add(
                AssistantOperation(
                    id="operation",
                    grant_id="grant",
                    owner_account_id=1,
                    client_id="client",
                    change_id="change",
                    idempotency_key="key",
                    request_hash="hash",
                    status="queued",
                    authorization_basis="standing",
                    result={"generation_stage": prior_stage} if prior_stage else {},
                )
            )
            session.add(AssistantBudget(grant_id="grant", provider_calls_reserved=1))
            job = Job(kind="assistant.generate_theme", operation_id="operation", payload={}, status="queued")
            session.add(job)
            session.commit()
            job_id = job.id

        # These old dispatch dependencies must remain unreachable.
        monkeypatch.setattr(generation, "provider_destination", lambda _store: pytest.fail("provider called"))
        first = dispatch_generation(state, {}, job_id=job_id)
        second = dispatch_generation(state, {}, job_id=job_id)
        assert first["status"] == second["status"] == expected_status
        with sessions() as session:
            operation = session.get(AssistantOperation, "operation")
            assert operation.status == expected_status
            assert operation.finished_at is not None
            assert session.get(AssistantBudget, "grant").provider_calls_reserved == 1
            if prior_stage:
                assert operation.result["generation_stage"] == prior_stage
                assert operation.result["generation_retired"] is True
            else:
                assert operation.result["generation_stage"] == "cancelled_before_dispatch"


def test_retired_dispatch_rejects_an_uncorrelated_job(tmp_path):
    with disposing_engine(create_engine(f"sqlite:///{tmp_path / 'no-operation.db'}")) as engine:
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine)
        with sessions() as session:
            job = Job(kind="assistant.generate_theme", payload={})
            session.add(job)
            session.commit()
            job_id = job.id
        with pytest.raises(RuntimeError, match="correlated approved operation"):
            dispatch_generation(SimpleNamespace(sessions=sessions), {}, job_id=job_id)
