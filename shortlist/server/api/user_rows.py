"""Per-user row overrides: which rows a user gets, and their mute/resize tweaks. Owner-only.

Split out of ``users.py`` so that module is about the user roster (list/patch/sync) and this one is
about the per-person row settings that hang off ``/users/{id}/rows``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from shortlist.server.api.users import _pick_dict
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    PickRow,
    RunUser,
    User,
)
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_owner)])


class RowOverridePatch(BaseModel):
    muted: bool | None = None
    row_size: int | None = Field(default=None, ge=5, le=40)
    recent_count: int | None = Field(default=None, ge=1, le=25)


def _applicable_rows(session, user: User) -> list[Collection]:
    """Enabled per-person collections this user is in the audience of (everyone, or a subset they're in)."""
    subset_ids = {row.collection_id for row in session.query(CollectionAudience).filter_by(user_id=user.id).all()}
    rows = (
        session.query(Collection)
        .filter_by(enabled=True, build="per_person")
        .order_by(Collection.sort_order, Collection.id)
        .all()
    )
    return [c for c in rows if c.audience == "everyone" or c.id in subset_ids]


@router.get("/{user_id}/rows")
async def user_rows(user_id: int, request: Request) -> list[dict]:
    """The rows this user gets, each with its effective settings, their override, and latest picks."""
    with request.app.state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")

        # Latest run's picks for this user, grouped by row (legacy blank slug -> the default row).
        latest = session.query(RunUser.run_id).filter_by(user_id=user.id).order_by(RunUser.run_id.desc()).first()
        picks_by_row: dict[str, list[dict]] = {}
        if latest is not None:
            for pick in (
                session.query(PickRow).filter_by(user_id=user.id, run_id=latest.run_id).order_by(PickRow.rank).all()
            ):
                picks_by_row.setdefault(pick.collection_slug or DEFAULT_SLUG, []).append(_pick_dict(pick))

        overrides = {o.collection_id: o for o in session.query(CollectionUserOverride).filter_by(user_id=user.id).all()}
        # The default 'picked' row's size follows the global setting, not its own stored column
        # (that's what the engine uses), so report that as its base size.
        store = SettingsStore(session, request.app.state.secrets)
        global_size = int(store.get("row.size"))
        # recent_count has no row-vs-global special case for the default row (unlike size): the row's
        # own column falls through to the global default the same way for every row, so report that
        # resolved base — it's what "Use the row's default" means for this person's override.
        global_recent_count = int(store.get("recommendations.recent_count"))

        out = []
        for collection in _applicable_rows(session, user):
            override = overrides.get(collection.id)
            row_recent_count = collection.recent_count if collection.recent_count is not None else global_recent_count
            out.append(
                {
                    "collection_id": collection.id,
                    "slug": collection.slug,
                    "name": collection.name,
                    "media": collection.media,
                    "size": global_size if collection.slug == DEFAULT_SLUG else collection.size,
                    "recent_count": row_recent_count,
                    "is_default": collection.slug == DEFAULT_SLUG,
                    "muted": bool(override and override.muted),
                    "override": {
                        "row_size": override.row_size if override else None,
                        "recent_count": override.recent_count if override else None,
                    },
                    "picks": picks_by_row.get(collection.slug, []),
                }
            )
        return out


@router.put("/{user_id}/rows/{collection_id}")
async def set_user_row_override(user_id: int, collection_id: int, patch: RowOverridePatch, request: Request) -> dict:
    """Mute or resize one row for one person — upserts their override."""
    with request.app.state.sessions() as session:
        if session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="user not found")
        if session.get(Collection, collection_id) is None:
            raise HTTPException(status_code=404, detail="row not found")
        override = session.get(CollectionUserOverride, (collection_id, user_id))
        if override is None:
            override = CollectionUserOverride(collection_id=collection_id, user_id=user_id)
            session.add(override)
        # Only touch fields actually present in the request, so a mute toggle that sends just
        # {muted} never clobbers a saved size, and an explicit row_size=null (the UI's "Default"
        # choice) really clears the override rather than being ignored.
        sent = patch.model_fields_set
        if "muted" in sent:
            override.muted = bool(patch.muted)
        if "row_size" in sent:
            override.row_size = patch.row_size  # None -> clear, inherit the row's own size
        if "recent_count" in sent:
            override.recent_count = patch.recent_count  # None -> clear, inherit the row's own recent_count
        session.commit()
        return {
            "collection_id": collection_id,
            "muted": override.muted,
            "row_size": override.row_size,
            "recent_count": override.recent_count,
        }
