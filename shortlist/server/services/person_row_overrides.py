"""Shared transaction projections for one person's settings on one per-person row."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from shortlist.engine.models import MAX_ROW_SIZE, MIN_ROW_SIZE
from shortlist.server.db.models import DEFAULT_SLUG, Collection, CollectionAudience, CollectionUserOverride, User
from shortlist.server.settings_store import SettingsStore

_NUMERIC_FIELDS = {"row_size", "recent_count", "favourite_count", "older_count"}


class RowOverridePatch(BaseModel):
    """PATCH-shaped stored preference values shared by REST and assistant planning."""

    model_config = ConfigDict(extra="forbid")

    muted: bool | None = None
    row_size: int | None = Field(default=None, ge=MIN_ROW_SIZE, le=MAX_ROW_SIZE)
    recent_count: int | None = Field(default=None, ge=1, le=25)
    favourite_count: int | None = Field(default=None, ge=0, le=10)
    older_count: int | None = Field(default=None, ge=0, le=10)


@dataclass(frozen=True)
class PersonRowOverrideMutation:
    user_id: int
    collection_id: int
    legacy_shared: bool
    values: dict[str, object]
    changed: dict[str, dict[str, object]]
    steps: tuple[dict, ...]


def _person_and_row(session: Session, user_id: int, collection_id: int) -> tuple[User, Collection]:
    user = session.get(User, user_id)
    if user is None:
        raise ValueError("row override requires an existing person")
    row = session.get(Collection, collection_id)
    if row is None:
        raise ValueError("row override requires an existing row")
    return user, row


def _applicable_person_row(
    session: Session, user_id: int, collection_id: int, *, allow_legacy_shared: bool = False
) -> tuple[User, Collection, bool]:
    user, row = _person_and_row(session, user_id, collection_id)
    legacy_shared = row.build == "shared" and session.get(CollectionUserOverride, (collection_id, user_id)) is not None
    if row.build != "per_person" and not (allow_legacy_shared and legacy_shared):
        raise ValueError("row override requires a per-person row")
    if (
        row.build == "per_person"
        and row.audience == "subset"
        and session.get(CollectionAudience, (collection_id, user_id)) is None
    ):
        raise ValueError("row override requires a row in this person's audience")
    return user, row, legacy_shared


def prepare_person_row_override_in_session(
    session: Session,
    user_id: int,
    collection_id: int,
    patch: RowOverridePatch,
    *,
    allow_legacy_shared: bool = False,
) -> PersonRowOverrideMutation:
    """Validate and project a sparse override without writing it.

    A mute is a privacy/visibility change: it owes a narrow durable reconcile so
    existing Plex collections disappear rather than waiting for the next run.
    """
    _user, row, legacy_shared = _applicable_person_row(
        session, user_id, collection_id, allow_legacy_shared=allow_legacy_shared
    )
    existing = session.get(CollectionUserOverride, (collection_id, user_id))
    before = {
        "muted": bool(existing and existing.muted),
        "row_size": existing.row_size if existing else None,
        "recent_count": existing.recent_count if existing else None,
        "favourite_count": existing.favourite_count if existing else None,
        "older_count": existing.older_count if existing else None,
    }
    values: dict[str, object] = {}
    for field in patch.model_fields_set:
        value = getattr(patch, field)
        # `muted` has no inheritance state. REST has always treated an explicit
        # null as an unmute; numeric nulls intentionally restore inheritance.
        values[field] = bool(value) if field == "muted" else value
    if legacy_shared and any(
        (field == "muted" and value is True) or (field in _NUMERIC_FIELDS and value is not None)
        for field, value in values.items()
    ):
        raise ValueError("legacy shared row overrides can only be cleared in the owner API")
    changed = {
        field: {"before": before[field], "after": value} for field, value in values.items() if before[field] != value
    }
    steps: tuple[dict, ...] = ()
    if not legacy_shared and changed.get("muted", {}).get("after") is True:
        from shortlist.server.assistant.row_effects import reconcile_step

        steps = (
            reconcile_step(
                row.slug,
                build="per_person",
                only_user_ids=[user_id],
                scope="user.row_override.mute",
            ),
        )
    return PersonRowOverrideMutation(
        user_id=user_id,
        collection_id=collection_id,
        legacy_shared=legacy_shared,
        values=values,
        changed=changed,
        steps=steps,
    )


def apply_person_row_override_in_session(
    session: Session, mutation: PersonRowOverrideMutation
) -> CollectionUserOverride | None:
    """Apply a prepared override under the caller's transaction boundary."""
    if not mutation.changed:
        return session.get(CollectionUserOverride, (mutation.collection_id, mutation.user_id))
    override = session.get(CollectionUserOverride, (mutation.collection_id, mutation.user_id))
    if override is None:
        override = CollectionUserOverride(collection_id=mutation.collection_id, user_id=mutation.user_id)
        session.add(override)
    for field, value in mutation.values.items():
        setattr(override, field, value)
    return override


def read_person_row_override_in_session(
    session: Session, user_id: int, collection_id: int, *, secrets
) -> dict[str, object]:
    """Return stored and engine-effective values for one personal row.

    A legacy shared-row record remains visible so the owner can clear it through
    the original API, but it is deliberately not described as an MCP-supported
    effective preference and new plans cannot create or strengthen it.
    """
    _user, row = _person_and_row(session, user_id, collection_id)
    override = session.get(CollectionUserOverride, (collection_id, user_id))
    stored: dict[str, bool | int | None] = {
        "muted": bool(override and override.muted),
        "row_size": override.row_size if override else None,
        "recent_count": override.recent_count if override else None,
        "favourite_count": override.favourite_count if override else None,
        "older_count": override.older_count if override else None,
    }
    if row.build != "per_person":
        return {
            "supported": False,
            "stored": stored,
            "effective": None,
            "warning": "A legacy shared-row override is read-only here; new assistant plans cannot modify it.",
        }
    if row.audience == "subset" and session.get(CollectionAudience, (collection_id, user_id)) is None:
        raise ValueError("row override requires a row in this person's audience")
    store = SettingsStore(session, secrets)
    base_row_size = int(store.get("row.size")) if row.slug == DEFAULT_SLUG else row.size
    base_recent_count = (
        row.recent_count if row.recent_count is not None else int(store.get("recommendations.recent_count"))
    )
    base_favourite_count = (
        row.favourite_count if row.favourite_count is not None else int(store.get("recommendations.favourite_count"))
    )
    base_older_count = row.older_count if row.older_count is not None else int(store.get("recommendations.older_count"))
    effective: dict[str, bool | int] = {
        "muted": bool(stored["muted"]),
        "row_size": int(stored["row_size"] if stored["row_size"] is not None else base_row_size),
        "recent_count": int(stored["recent_count"] if stored["recent_count"] is not None else base_recent_count),
        "favourite_count": int(
            stored["favourite_count"] if stored["favourite_count"] is not None else base_favourite_count
        ),
        "older_count": int(stored["older_count"] if stored["older_count"] is not None else base_older_count),
        "base_row_size": int(base_row_size),
        "base_recent_count": int(base_recent_count),
        "base_favourite_count": int(base_favourite_count),
        "base_older_count": int(base_older_count),
    }
    return {"supported": True, "stored": stored, "effective": effective}
