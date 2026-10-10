"""Shared saved-theme selection for one person's Explore rotation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from shortlist.server.db.models import Collection, Theme, ThemeHistory, User
from shortlist.server.services.theme_rotation import audience_users, queue_next
from shortlist.server.services.theme_store import reject_person_title_clash


@dataclass(frozen=True)
class UpNextSelection:
    collection: Collection
    person: User
    theme: Theme


def prepare_up_next_in_session(
    session: Session, collection_id: int, user_id: int, theme_id: int, *, secrets=None
) -> UpNextSelection:
    """Validate the same saved selection for owner HTTP and assistant planning."""
    collection = session.get(Collection, collection_id)
    if collection is None or collection.theme_id is None:
        raise LookupError("That row isn't an AI row.")
    if collection.theme_mode != "explore" or collection.build != "per_person":
        raise ValueError("Turn on Explore for a per-person row first.")
    person = next((user for user in audience_users(session, collection) if user.id == user_id), None)
    if person is None:
        raise LookupError("That person isn't in this row's audience.")
    theme = session.get(Theme, theme_id)
    if theme is None:
        raise LookupError("theme not found")
    reject_person_title_clash(session, secrets, collection, user_id, theme)
    return UpNextSelection(collection, person, theme)


def apply_up_next_in_session(session: Session, selection: UpNextSelection) -> ThemeHistory:
    """Queue a validated saved theme; the caller owns the target lock and transaction."""
    return queue_next(
        session, selection.collection, selection.person.id, selection.theme, datetime.now(UTC), checked=True
    )
