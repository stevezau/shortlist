"""Per-user row overrides: which rows a user gets, and their mute/resize tweaks. Owner-only.

Split out of ``users.py`` so that module is about the user roster (list/patch/sync) and this one is
about the per-person row settings that hang off ``/users/{id}/rows``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from shortlist.server.api.serializers import UserPickOut, pick_dict
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    PickRow,
    User,
)
from shortlist.server.schema_base import PassthroughModel
from shortlist.server.services.person_row_overrides import (
    RowOverridePatch,
    apply_person_row_override_in_session,
    prepare_person_row_override_in_session,
)
from shortlist.server.services.run_persistence import live_pick_ids
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_owner)])


class RowOverrideOut(PassthroughModel):
    """This person's stored tweaks for one row. `None` on either field means "use the row's own"."""

    row_size: int | None
    recent_count: int | None


class UserRowOut(PassthroughModel):
    """One row this person gets, in one library: its effective settings, their override, its picks."""

    collection_id: int
    slug: str
    name: str
    media: str
    library: str
    section_key: str
    size: int
    recent_count: int  # the row's EFFECTIVE depth — what "use the row's default" resolves to
    is_default: bool
    muted: bool
    override: RowOverrideOut
    picks: list[UserPickOut]


class RowOverrideSavedOut(PassthroughModel):
    """The override as stored, echoed back by the PUT."""

    collection_id: int
    muted: bool
    row_size: int | None
    recent_count: int | None


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


@router.get("/{user_id}/rows", response_model=list[UserRowOut])
async def user_rows(user_id: int, request: Request) -> list[dict]:
    """The rows this user gets, each with its effective settings, their override, and latest picks."""
    with request.app.state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")

        # What is on Plex right now, grouped by (row, library): each row's picks come from the newest run
        # that delivered IT (`live_pick_ids`), not the newest run overall. Rows have their own crons, so
        # a run that built one row must not blank the others, and a dry run or cancelled run writes no
        # picks and changes nothing here. A row spanning multiple libraries is one Plex collection per
        # library — show them as separate cards.
        live_ids = live_pick_ids(session, user_id=user.id).get(user.id, set())
        picks_by_row_lib: dict[tuple[str, str], list[dict]] = {}
        if live_ids:
            for pick in session.query(PickRow).filter(PickRow.id.in_(live_ids)).order_by(PickRow.rank).all():
                key = (pick.collection_slug or DEFAULT_SLUG, pick.section_key or "")
                picks_by_row_lib.setdefault(key, []).append(pick_dict(pick))

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
            slug = collection.slug
            # Collect all library keys this row delivered to, preserving order from the picks.
            lib_keys = list(dict.fromkeys(k for (s, k) in picks_by_row_lib if s == slug))
            if not lib_keys:
                lib_keys = [""]
            for section_key in lib_keys:
                picks = picks_by_row_lib.get((slug, section_key), [])
                library_name = picks[0]["library"] if picks else ""
                out.append(
                    {
                        "collection_id": collection.id,
                        "slug": slug,
                        "name": collection.name,
                        "media": collection.media,
                        "library": library_name,
                        "section_key": section_key,
                        "size": global_size if slug == DEFAULT_SLUG else collection.size,
                        "recent_count": row_recent_count,
                        "is_default": slug == DEFAULT_SLUG,
                        "muted": bool(override and override.muted),
                        "override": {
                            "row_size": override.row_size if override else None,
                            "recent_count": override.recent_count if override else None,
                        },
                        "picks": picks,
                    }
                )
        return out


@router.put("/{user_id}/rows/{collection_id}", response_model=RowOverrideSavedOut)
async def set_user_row_override(user_id: int, collection_id: int, patch: RowOverridePatch, request: Request) -> dict:
    """Mute or resize one row for one person — upserts their override."""
    from shortlist.server.assistant.row_effects import queue_convergence_in_session
    from shortlist.server.services import jobs

    state = request.app.state
    with request.app.state.sessions() as session:
        try:
            mutation = prepare_person_row_override_in_session(
                session,
                user_id,
                collection_id,
                patch,
                allow_legacy_shared=True,
            )
        except ValueError as exc:
            status_code = 404 if "existing person" in str(exc) or "existing row" in str(exc) else 422
            raise HTTPException(status_code=status_code, detail=str(exc)) from None
        override = apply_person_row_override_in_session(session, mutation)
        if mutation.steps:
            queue_convergence_in_session(session, mutation.steps, domain="people")
        session.commit()
        result = {
            "collection_id": collection_id,
            "muted": bool(override and override.muted),
            "row_size": override.row_size if override else None,
            "recent_count": override.recent_count if override else None,
        }
    if mutation.steps:
        await jobs.drain_now(state, "person row override changed")
    return result
