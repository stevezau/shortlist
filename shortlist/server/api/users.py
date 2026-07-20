"""Users API: list with badges, enable/prefs, sync from plex.tv."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.delivery import remove_row_collections
from shortlist.server.auth import require_owner
from shortlist.server.db.adapters import unique_slug
from shortlist.server.db.models import DEFAULT_SLUG, Event, PickRow, Run, RunUser, Server, User, iso_utc
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_owner)])


def _pick_dict(pick: PickRow) -> dict:
    return {
        "rank": pick.rank,
        "title": pick.title,
        "reason": pick.reason,
        "media_type": pick.media_type,
        "collection_slug": pick.collection_slug or DEFAULT_SLUG,  # legacy blank rows are the default row
        "seed_title": pick.seed_title,
    }


class UserPrefs(BaseModel):
    # `row_size` and `max_rating` used to live here. Neither did anything: max_rating filtered no
    # content at all, and a row's own size always won. Per-person row size lives on the row override
    # (PUT /users/{id}/rows/{collection_id}), which the UI actually exposes.
    row_name_tpl: str | None = None
    excluded_genres: list[str] | None = None
    paused: bool | None = None
    # Per-person curation-recipe overrides. Empty string = inherit the global default.
    prompt_tone: str | None = None
    prompt_guidance: str | None = None
    prompt_template: str | None = None


class UserPatch(BaseModel):
    enabled: bool | None = None
    request_tag: str | None = Field(default=None, max_length=64)  # tag added to titles requested for this user
    prefs: UserPrefs | None = None


class BulkEnabled(BaseModel):
    enabled: bool


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
        "request_tag": user.request_tag or "",
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
            # DISTINCT title, not pick row: a title recommended over several runs is one title, and a
            # title watched after lingering a few runs is one hit — counting rows would skew both.
            # `||` via .concat(), NOT func.concat: the latter compiles to SQLite's concat() scalar,
            # which only exists in SQLite >= 3.44 — the runtime image ships 3.40, so it would 500.
            title = cast(PickRow.tmdb_id, String).concat("-").concat(PickRow.media_type)
            titles_total = (
                session.query(func.count(func.distinct(title))).filter(PickRow.user_id == user.id).scalar() or 0
            )
            titles_watched = (
                session.query(func.count(func.distinct(title)))
                .filter(PickRow.user_id == user.id, PickRow.watched_at.isnot(None))
                .scalar()
                or 0
            )
            hit_rate = round(titles_watched / titles_total, 3) if titles_total else None
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


async def _remove_users_rows(state, user_slug: str) -> None:
    """Remove ALL of a just-disabled user's Shortlist collections (their whole label). Best-effort +
    audited; removal only, so gate-exempt (it only makes the server more private). Runs in an executor
    because it does Plex I/O; a Plex outage/unconfigured server is logged, not fatal."""
    removed: list[str] = []

    def work() -> None:
        ctx = state.run_service.build_context(dry_run=False)
        removed.extend(
            remove_row_collections(
                ctx.plex, ctx.config, label=f"{ctx.config.label_prefix}_{user_slug}", displays=None, dry_run=False
            )
        )

    error: str | None = None
    try:
        await asyncio.get_running_loop().run_in_executor(None, work)
    except Exception as e:
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
        # Also narrate it: the failure IS audited to events, but this is a destructive Plex write, so
        # it should show in the live run/console log the operator is watching, not only the DB.
        logger.warning("disable cleanup for {} hit an error ({})", user_slug, type(e).__name__)
    with state.sessions() as session:
        session.add(
            Event(
                scope="user.disable.cleanup",
                level="warn",
                message={"user": user_slug, "removed": removed, "error": error, "at": datetime.now(UTC).isoformat()},
            )
        )
        session.commit()
    logger.warning(
        "disabled user '{}': removed {} collection(s){}",
        user_slug,
        len(removed),
        f" then FAILED: {error}" if error else "",
    )


@router.post("/set-enabled")
async def set_all_users_enabled(body: BulkEnabled, request: Request) -> dict:
    """Enable or disable EVERY user at once. Enabling just flips the flags (rows rebuild on the next
    run). Disabling also removes each newly-disabled user's rows from Plex now — the same cleanup the
    per-user toggle does, so 'off' means gone, not merely 'not refreshed'. Best-effort + audited."""
    state = request.app.state
    to_clean: list[str] = []
    with state.sessions() as session:
        users = session.query(User).all()
        for user in users:
            if body.enabled is False and user.enabled:
                to_clean.append(user.slug)  # was on, now off -> remove their rows from Plex
            user.enabled = body.enabled
        session.commit()
        total = len(users)
    for slug in to_clean:
        await _remove_users_rows(state, slug)
    return {"updated": total, "cleaned": len(to_clean), "enabled": body.enabled}


@router.patch("/{user_id}")
async def patch_user(user_id: int, patch: UserPatch, request: Request) -> dict:
    state = request.app.state
    disabled_slug: str | None = None
    with state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if patch.enabled is not None:
            if user.enabled and patch.enabled is False:
                # Turned off → remove their rows from Plex now, not just stop delivering to them.
                disabled_slug = user.slug
            user.enabled = patch.enabled
        if patch.request_tag is not None:
            user.request_tag = patch.request_tag.strip()
        if patch.prefs is not None:
            prefs = dict(user.prefs or {})
            prefs.update({k: v for k, v in patch.prefs.model_dump().items() if v is not None})
            user.prefs = prefs
        session.commit()
        result = _serialize(user, (user.prefs or {}).get("history_depth", 0), None, None)
    if disabled_slug is not None:
        await _remove_users_rows(state, disabled_slug)
    return result


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
        # A PMS/Tautulli error can carry a tokened URL — redact before it reaches the response (rule 9).
        logger.warning("user-history fetch failed for user {} ({})", user_id, type(e).__name__)
        raise HTTPException(status_code=502, detail=redact(f"{type(e).__name__}: {e}")) from e
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
