"""Canonical owner-safe views of persisted row values.

These helpers deliberately live below both the HTTP and assistant transports so
the two surfaces do not drift on stored AI guidance or live avoid-row values.
Transport-specific authorization remains the caller's responsibility.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.web_guidance import AiInstructions
from shortlist.server.db.models import DEFAULT_SLUG, Collection
from shortlist.server.settings_store import SettingsStore


def ai_instructions_view(stored: object) -> dict[str, str]:
    """Return the stable public form of a row's stored AI instructions."""
    parsed = AiInstructions.from_stored(stored)
    return {"mode": parsed.mode, "text": parsed.text} if parsed else {"mode": "default", "text": ""}


def live_avoid_rows(session: Session, collection: Collection) -> list[str] | None:
    """Return configured per-person rows that still exist.

    Deleted and shared rows are intentionally omitted, matching the owner API:
    neither is a valid avoid-row target any longer.
    """
    if not collection.avoid_rows:
        return None
    live = {
        slug
        for (slug,) in session.execute(
            select(Collection.slug).where(Collection.slug.in_(collection.avoid_rows), Collection.build == "per_person")
        )
    }
    return [slug for slug in collection.avoid_rows if slug in live] or None


def row_display_name(session: Session, collection: Collection) -> str:
    """What the Rows page calls a row.

    The default row's real title is the global template (Settings → Defaults), which the engine renders per
    library — not its stale seeded `name` column. Surfacing the template shows the actual default
    ("✨ {library_name} Picked for You"), consistent with what delivers.
    """
    if collection.slug == DEFAULT_SLUG:
        return SettingsStore(session).get("row.name_template") or collection.name
    return collection.name
