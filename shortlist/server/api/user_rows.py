"""Per-user row overrides: which rows a user gets, and their mute/resize/restyle tweaks. Owner-only.

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
    # Per-row curation override; empty strings inherit the row's own recipe.
    prompt_tone: str | None = None
    prompt_guidance: str | None = None
    prompt_template: str | None = None


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
        global_size = int(SettingsStore(session, request.app.state.secrets).get("row.size"))

        out = []
        for collection in _applicable_rows(session, user):
            override = overrides.get(collection.id)
            recipe = (override.prompt if override else None) or {}
            out.append(
                {
                    "collection_id": collection.id,
                    "slug": collection.slug,
                    "name": collection.name,
                    "media": collection.media,
                    "size": global_size if collection.slug == DEFAULT_SLUG else collection.size,
                    "is_default": collection.slug == DEFAULT_SLUG,
                    "muted": bool(override and override.muted),
                    "override": {
                        "row_size": override.row_size if override else None,
                        "prompt_tone": recipe.get("tone", ""),
                        "prompt_guidance": recipe.get("guidance", ""),
                        "prompt_template": recipe.get("template", ""),
                    },
                    "picks": picks_by_row.get(collection.slug, []),
                }
            )
        return out


@router.put("/{user_id}/rows/{collection_id}")
async def set_user_row_override(user_id: int, collection_id: int, patch: RowOverridePatch, request: Request) -> dict:
    """Mute, resize, or restyle one row for one person — upserts their override."""
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
        # {muted} never clobbers a saved size/recipe, and an explicit row_size=null (the UI's
        # "Default" choice) really clears the override rather than being ignored.
        sent = patch.model_fields_set
        if "muted" in sent:
            override.muted = bool(patch.muted)
        if "row_size" in sent:
            override.row_size = patch.row_size  # None -> clear, inherit the row's own size
        if sent & {"prompt_tone", "prompt_guidance", "prompt_template"}:
            recipe = {
                "tone": (patch.prompt_tone or "").strip(),
                "guidance": (patch.prompt_guidance or "").strip(),
                "template": (patch.prompt_template or "").strip(),
            }
            override.prompt = recipe if any(recipe.values()) else {}  # all-blank clears it
        session.commit()
        return {
            "collection_id": collection_id,
            "muted": override.muted,
            "row_size": override.row_size,
            "prompt": override.prompt,
        }
