"""Users API: list with badges, enable/prefs, sync from plex.tv."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func

from rowarr.engine.clients.plextv import PlexTvClient
from rowarr.server.auth import require_owner
from rowarr.server.db.adapters import unique_slug
from rowarr.server.db.models import PickRow, Run, RunUser, Server, User, iso_utc
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
