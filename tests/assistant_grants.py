"""Grant lookups and authority swaps that only tests need, built on the repository's own internals."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select, update

from shortlist.server.assistant_auth.models import AssistantGrant
from shortlist.server.assistant_auth.policy import AuthorizationDenied, basic_role_capabilities
from shortlist.server.assistant_auth.repository import AssistantAuthRepository, _grant_context, _now
from shortlist.server.assistant_auth.types import ASSISTANT_CAPABILITIES, Capability, GrantConstraints, GrantContext


def find_grant_for_client(
    repository: AssistantAuthRepository,
    client_id: str,
    *,
    owner_account_id: int | None = None,
    now: datetime | None = None,
) -> GrantContext | None:
    """Find the newest active grant for an OAuth client."""
    with repository.sessions() as session:
        statement = select(AssistantGrant).where(AssistantGrant.client_id == client_id)
        if owner_account_id is not None:
            statement = statement.where(AssistantGrant.owner_account_id == owner_account_id)
        rows = session.scalars(statement.order_by(AssistantGrant.created_at.desc())).all()
        for row in rows:
            if context := repository._active_context(session, row, _now(now)):
                return context
    return None


def replace_grant_authority(
    repository: AssistantAuthRepository,
    grant_id: str,
    *,
    capabilities: Iterable[Capability],
    constraints: GrantConstraints,
    expected_revision: int,
    now: datetime | None = None,
) -> GrantContext:
    """Replace authority using a revision compare-and-swap."""
    timestamp = _now(now)
    values = sorted({capability.value for capability in capabilities})
    if not ASSISTANT_CAPABILITIES.issuperset(Capability(value) for value in values):
        raise AuthorizationDenied("owner secrets and grant administration cannot be delegated")
    if constraints.basic_access_v1 is not None and not basic_role_capabilities(constraints.basic_access_v1).issuperset(
        Capability(value) for value in values
    ):
        raise AuthorizationDenied("capabilities exceed the selected access role")
    with repository.sessions() as session:
        current = session.get(AssistantGrant, grant_id)
        if current is not None and GrantConstraints.from_dict(current.constraints).requires_access_approval:
            raise AuthorizationDenied("owner approval is required before replacing this legacy grant")
        changed = session.execute(
            update(AssistantGrant)
            .where(
                AssistantGrant.id == grant_id,
                AssistantGrant.revision == expected_revision,
                AssistantGrant.revoked_at.is_(None),
            )
            .values(
                capabilities=values,
                constraints=constraints.as_dict(),
                revision=AssistantGrant.revision + 1,
                updated_at=timestamp,
            )
        ).rowcount
        if changed != 1:
            session.rollback()
            raise AuthorizationDenied("assistant grant changed or was revoked")
        session.commit()
        return _grant_context(session.get(AssistantGrant, grant_id))
