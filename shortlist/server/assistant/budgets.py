"""Durable, conservative provider-call quotas for named assistant connections."""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, Session, mapped_column

from shortlist.server.db.models import Base


class AssistantBudget(Base):
    """Lifetime reservations; an uncertain external result consumes its reservation."""

    __tablename__ = "assistant_budgets"

    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_calls_reserved: Mapped[int] = mapped_column(Integer, default=0)


def reserve_provider_calls(session: Session, grant_id: str, *, requested: int, limit: int) -> None:
    """Reserve within the caller's serialized apply transaction, without committing.

    This limits dispatch count across the grant's lifetime, not currency. SDK
    retries must be disabled by consumers so one reservation is one paid request.
    """
    from .policy import ChangeError

    if requested == 0:
        return
    if type(requested) is not int or requested < 0 or type(limit) is not int or limit < 0:
        raise ChangeError("budget_exceeded", "The provider-call quota is invalid.")
    budget = session.get(AssistantBudget, grant_id)
    reserved = budget.provider_calls_reserved if budget else 0
    if reserved + requested > limit:
        raise ChangeError("budget_exceeded", "This connection's lifetime provider-call quota would be exceeded.")
    if budget is None:
        session.add(AssistantBudget(grant_id=grant_id, provider_calls_reserved=requested))
    else:
        budget.provider_calls_reserved += requested
    session.flush()
