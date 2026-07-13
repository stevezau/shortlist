"""Users API: list with badges, enable/prefs, sync from plex.tv."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func

from rowarr.engine.clients.plex import PlexTvClient
from rowarr.server.auth import require_owner
from rowarr.server.db.models import (
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    PickRow,
    Run,
    RunUser,
    Server,
    User,
    iso_utc,
)
from rowarr.server.services.run_service import unique_slug
from rowarr.server.settings_store import SettingsStore

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_owner)])


def _pick_dict(pick: PickRow) -> dict:
    return {
        "rank": pick.rank,
        "title": pick.title,
        "reason": pick.reason,
        "media_type": pick.media_type,
        "collection_slug": pick.collection_slug or "picked",  # legacy blank rows are the default row
        "seed_title": pick.seed_title,
    }


class UserPrefs(BaseModel):
    row_name_tpl: str | None = None
    row_size: int | None = Field(default=None, ge=5, le=30)
    excluded_genres: list[str] | None = None
    max_rating: str | None = None
    paused: bool | None = None
    # Per-person curation-recipe overrides. Empty string = inherit the global default.
    prompt_tone: str | None = None
    prompt_guidance: str | None = None
    prompt_template: str | None = None


class UserPatch(BaseModel):
    enabled: bool | None = None
    prefs: UserPrefs | None = None


def _serialize(
    user: User,
    history_depth: int,
    last_run_at,
    hit_rate: float | None,
    preview_titles: list[str] | None = None,
) -> dict:
    return {
        "id": user.id,
        "plex_account_id": user.plex_account_id,
        "username": user.username,
        "slug": user.slug,
        "avatar_url": user.avatar_url,
        "user_type": user.user_type,
        "enabled": user.enabled,
        "cold_start": user.cold_start,
        "prefs": user.prefs or {},
        "history_depth": history_depth,
        "last_run_at": iso_utc(last_run_at),
        "hit_rate": hit_rate,
        # A few of their most recent pick titles, for a real preview strip on the dashboard card.
        "preview_titles": preview_titles or [],
    }


@router.get("")
async def list_users(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        out = []
        for user in session.query(User).order_by(User.username).all():
            picks_total = session.query(func.count(PickRow.id)).filter_by(user_id=user.id).scalar() or 0
            picks_watched = (
                session.query(func.count(PickRow.id))
                .filter(PickRow.user_id == user.id, PickRow.watched_at.isnot(None))
                .scalar()
                or 0
            )
            hit_rate = round(picks_watched / picks_total, 3) if picks_total else None
            last = (
                session.query(RunUser)
                .filter_by(user_id=user.id)
                .join(RunUser.run)
                .order_by(RunUser.run_id.desc())
                .first()
            )
            preview = []
            if last is not None:
                preview = [
                    p.title
                    for p in session.query(PickRow)
                    .filter_by(user_id=user.id, run_id=last.run_id)
                    .order_by(PickRow.rank)
                    .limit(3)
                    .all()
                ]
            history_depth = (user.prefs or {}).get("history_depth", 0)
            out.append(_serialize(user, history_depth, last.run.finished_at if last else None, hit_rate, preview))
        return out


@router.patch("/{user_id}")
async def patch_user(user_id: int, patch: UserPatch, request: Request) -> dict:
    with request.app.state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if patch.enabled is not None:
            user.enabled = patch.enabled
        if patch.prefs is not None:
            prefs = dict(user.prefs or {})
            prefs.update({k: v for k, v in patch.prefs.model_dump().items() if v is not None})
            user.prefs = prefs
        session.commit()
        return _serialize(user, (user.prefs or {}).get("history_depth", 0), None, None)


class RowOverridePatch(BaseModel):
    muted: bool | None = None
    row_size: int | None = Field(default=None, ge=5, le=30)
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
                picks_by_row.setdefault(pick.collection_slug or "picked", []).append(_pick_dict(pick))

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
                    "size": global_size if collection.slug == "picked" else collection.size,
                    "is_default": collection.slug == "picked",
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


@router.get("/{user_id}/runs")
async def user_runs(user_id: int, request: Request, limit: int = 15) -> list[dict]:
    """This user's recent run results — status, what changed, and the picks with their reasons."""
    with request.app.state.sessions() as session:
        if session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="user not found")
        run_users = (
            session.query(RunUser)
            .filter_by(user_id=user_id)
            .join(RunUser.run)
            .order_by(RunUser.run_id.desc())
            .limit(min(limit, 50))
            .all()
        )
        out = []
        for ru in run_users:
            run = session.get(Run, ru.run_id)
            picks = session.query(PickRow).filter_by(user_id=user_id, run_id=ru.run_id).order_by(PickRow.rank).all()
            out.append(
                {
                    "run_id": ru.run_id,
                    "started_at": iso_utc(run.started_at) if run else None,
                    "finished_at": iso_utc(run.finished_at) if run else None,
                    "status": ru.status,
                    "error": ru.error,
                    "dry_run": run.dry_run if run else False,
                    "diff": ru.diff or {},
                    "picks": [_pick_dict(p) for p in picks],
                }
            )
        return out


@router.get("/{user_id}/history")
async def user_history(user_id: int, request: Request, limit: int = 25) -> list[dict]:
    """Recent watch history for this user, from Tautulli/Plex — the same source recommendations use."""

    def fetch():
        return request.app.state.run_service.user_history(user_id, limit=min(limit, 100))

    try:
        rows = await asyncio.get_running_loop().run_in_executor(None, fetch)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e
    if rows is None:
        raise HTTPException(status_code=404, detail="user not found")
    return rows


@router.post("/sync")
async def sync_users(request: Request) -> dict:
    """Pull shared + Home users from plex.tv into the users table (idempotent upsert)."""
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        token = store.get("plex.token")
        server = session.query(Server).first()
    if not token or server is None:
        raise HTTPException(status_code=409, detail="Plex is not connected yet")
    machine_id = server.machine_id

    def fetch():
        # machine_id comes from the server table — no PMS round-trip needed to talk to plex.tv.
        return PlexTvClient(token, machine_id).list_users()

    remote = await asyncio.get_running_loop().run_in_executor(None, fetch)
    added = updated = 0
    with state.sessions() as session:
        for r in remote:
            user = session.query(User).filter_by(plex_account_id=r.id).one_or_none()
            if user is None:
                session.add(
                    User(
                        plex_account_id=r.id,
                        username=r.username,
                        slug=unique_slug(session, r.username),
                        avatar_url=r.avatar_url,
                        user_type=r.user_type.value,
                    )
                )
                added += 1
            else:
                user.username = r.username
                user.avatar_url = r.avatar_url
                user.user_type = r.user_type.value
                updated += 1
        session.commit()
    return {"added": added, "updated": updated, "total": len(remote)}
