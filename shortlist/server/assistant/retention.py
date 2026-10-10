"""Retention for assistant state: OAuth working rows and plans nobody applied."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, exists
from sqlalchemy.orm import Session

from shortlist.server.assistant.operation_models import AssistantChange, AssistantOperation
from shortlist.server.assistant_auth.retention import prune_oauth_state

#: How long an expired, never-applied plan stays inspectable before it is dropped.
UNAPPLIED_CHANGE_RETENTION = timedelta(days=7)


def prune_assistant_state(session: Session, *, now: datetime) -> dict[str, int]:
    """Prune every assistant table that only grows. Idempotent; the caller commits.

    An applied change is kept: its operation row reads the change back for history and receipts, so
    only plans that never produced an operation are removed.
    """
    applied = exists().where(AssistantOperation.change_id == AssistantChange.id)
    changes = session.execute(
        delete(AssistantChange).where(
            AssistantChange.expires_at < now - UNAPPLIED_CHANGE_RETENTION,
            AssistantChange.operation_id.is_(None),
            ~applied,
        )
    ).rowcount
    return {**prune_oauth_state(session, now=now), "assistant_changes": changes}
