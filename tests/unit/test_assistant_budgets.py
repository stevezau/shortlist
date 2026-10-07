"""Provider dispatch reservations are atomic and cannot be refunded by a timeout."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shortlist.server.assistant.budgets import AssistantBudget, reserve_provider_calls
from shortlist.server.assistant.policy import ChangeError
from shortlist.server.db.models import Base
from tests.db_helpers import disposing_engine


def test_lifetime_quota_is_reserved_and_rollback_is_atomic():
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            reserve_provider_calls(session, "grant", requested=1, limit=2)
            session.commit()
            reserve_provider_calls(session, "grant", requested=1, limit=2)
            session.rollback()
            assert session.get(AssistantBudget, "grant").provider_calls_reserved == 1
            reserve_provider_calls(session, "grant", requested=1, limit=2)
            session.commit()
            with pytest.raises(ChangeError, match="quota"):
                reserve_provider_calls(session, "grant", requested=1, limit=2)
            assert session.get(AssistantBudget, "grant").provider_calls_reserved == 2


def test_zero_budget_denies_paid_dispatch():
    with disposing_engine(create_engine("sqlite://")) as engine:
        Base.metadata.create_all(engine)
        with Session(engine) as session, pytest.raises(ChangeError):
            reserve_provider_calls(session, "grant", requested=1, limit=0)
