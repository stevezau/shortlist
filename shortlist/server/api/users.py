"""Users API: list with badges, enable/prefs, sync from plex.tv."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.clients.tautulli import TautulliClient
from shortlist.engine.delivery import remove_row_collections
from shortlist.engine.models import UserType
from shortlist.server.auth import require_owner
from shortlist.server.db.adapters import unique_slug
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Event,
    PickRow,
    Run,
    RunUser,
    Server,
    User,
    WatchEvent,
    iso_utc,
)
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.services.setup_probe import plextv_account
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
    # What to call them in a row title. "" clears the override and falls back to Tautulli's friendly
    # name, then their Plex username. Never touches the slug, so their label (and every share filter
    # that excludes it) is unaffected.
    nickname: str | None = Field(default=None, max_length=255)
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
        "nickname": user.nickname or "",
        # What Tautulli calls them, when it has its own name for them — the default a blank
        # nickname falls back to, shown in the UI so the field's placeholder can be honest.
        "friendly_name": user.friendly_name or "",
        "display_name": user.nickname or user.friendly_name or user.username,
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


def _watch_depths(session) -> dict[int, int]:
    """user_id -> how many DISTINCT titles we have watch events for.

    Read from the local watch mirror rather than `prefs["history_depth"]`, which is only written
    after a run actually processes someone — so a user who was skipped, or who has simply never had
    a successful run, showed "0 titles" forever. A beta user was looking at a Users page reporting 0
    for all 42 of his accounts while the log showed 170 events synced for one of them, which is a
    terrible thing to be told while working out why nothing was recommended.

    Distinct rating_key, not row count: watch_events holds one row per PLAY, so a 40-episode binge
    is one title here — matching what "N titles" claims.
    """
    rows = (
        session.query(WatchEvent.user_id, func.count(func.distinct(WatchEvent.rating_key)))
        .group_by(WatchEvent.user_id)
        .all()
    )
    return {user_id: count for user_id, count in rows}


@router.get("")
async def list_users(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        depths = _watch_depths(session)
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
            out.append(
                _serialize(user, depths.get(user.id, 0), last.run.finished_at if last else None, hit_rate, preview)
            )
        return out


async def _remove_users_rows(state, user_slug: str) -> None:
    """Remove ALL of a just-disabled user's Shortlist collections (their whole label). Best-effort +
    audited; removal only, so gate-exempt (it only makes the server more private). Runs in an executor
    because it does Plex I/O; a Plex outage/unconfigured server is logged, not fatal."""
    removed: list[str] = []

    def work() -> None:
        dry_run = force_dry_run()  # honour SHORTLIST_DRY_RUN safe-mode (this path is otherwise always live)
        ctx = state.run_service.build_context(dry_run=dry_run)
        removed.extend(
            remove_row_collections(
                ctx.plex, ctx.config, label=f"{ctx.config.label_prefix}_{user_slug}", displays=None, dry_run=dry_run
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
    renamed_slug: str | None = None
    with state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if patch.enabled is not None:
            if user.enabled and patch.enabled is False:
                # Turned off → remove their rows from Plex now, not just stop delivering to them.
                disabled_slug = user.slug
            user.enabled = patch.enabled
        if patch.nickname is not None:
            nickname = patch.nickname.strip()
            # Checked whether it is being SET or CLEARED: clearing falls back to the Tautulli or
            # Plex name, which is just as capable of colliding as one that was typed.
            _reject_display_name_clash(session, user, nickname or user.friendly_name or user.username)
            renamed_slug = user.slug if nickname != (user.nickname or "") else None
            user.nickname = nickname
        if patch.request_tag is not None:
            user.request_tag = patch.request_tag.strip()
        if patch.prefs is not None:
            prefs = dict(user.prefs or {})
            prefs.update({k: v for k, v in patch.prefs.model_dump().items() if v is not None})
            user.prefs = prefs
        session.commit()
        result = _serialize(user, _watch_depths(session).get(user.id, 0), None, None)
    if disabled_slug is not None:
        await _remove_users_rows(state, disabled_slug)
    if renamed_slug is not None:
        # A nickname changes what `{user}` renders to, so this person's existing collections carry a
        # title no future run will write. Renaming them in place is the same reconcile a row rename
        # uses; without it a multi-row user keeps the old-named copy alongside the new one.
        await _rename_after_nickname(state)
    return result


async def _rename_after_nickname(state) -> None:
    """Re-render every per-person row's titles so a nickname change lands on Plex now, not next run.

    Reuses the row-rename reconcile with each row's UNCHANGED template: it renames only the users
    whose rendered title actually drifted, which after one nickname edit is exactly that person.
    Best-effort and privacy-neutral — titles move, the label (and every filter excluding it) doesn't.
    """
    from shortlist.server.db.models import Collection
    from shortlist.server.services.collection_reconcile import run_row_rename

    with state.sessions() as session:
        rows = [
            (c.slug, c.name_template)
            for c in session.query(Collection).filter_by(enabled=True, build="per_person").all()
        ]
    for slug, template in rows:
        if "{user}" not in (template or ""):
            continue  # this row's title doesn't mention them, so a nickname can't have changed it
        await run_row_rename(state, slug=slug, new_template=template, scope="user.nickname")


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


def _display_names_drifted(session: Session, before: dict[int, str]) -> bool:
    """Did any EXISTING user's `{user}` name change? New users are excluded deliberately — they have
    no rows on Plex yet, so there is nothing to rename and no reason to make a sync do Plex I/O."""
    return any(
        user.id in before and (user.nickname or user.friendly_name or user.username) != before[user.id]
        for user in session.query(User)
    )


def _reject_display_name_clash(session: Session, user: User, nickname: str) -> None:
    """Refuse a nickname that renders to the same row title as somebody else's.

    `{user}` renders `display_name` (nickname → Tautulli friendly name → username). Only the
    username is unique on Plex, so two people resolving to the same display name ask for two
    collections with one title in one library — which PMS refuses, leaving that person's row failing
    every night with an error that reads as a generic Plex fault. Privacy is unaffected either way
    (collections are matched on `shortlist_<slug>` before title), so this is about a legible failure,
    not a leak: say so at the point of entry rather than in tomorrow's run log.
    """
    wanted = nickname.casefold()
    for other in session.query(User).filter(User.id != user.id):
        theirs = other.nickname or other.friendly_name or other.username
        if theirs.casefold() == wanted:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{other.username} already shows up as “{theirs}” — pick a different name so their rows stay apart"
                ),
            )


def _sync_owner(
    session: Session,
    account: dict | None,
    owner_account_id: int | None,
    friendly_names: dict[int, str] | None = None,
) -> str | None:
    """Add (or refresh) the server owner as a user. Returns "added", "updated", or None if skipped.

    plex.tv's `/api/users` lists everyone the server is shared WITH and never the account that owns
    it, so without this the person running Shortlist can never get a row of their own — the whole app
    is unusable for a one-person server. The owner is stored like any other user (history, labels and
    delivery all key off `plex_account_id`); `user_type="owner"` marks the one account Plex cannot
    restrict, which `sync_user_restrictions` skips and the UI badges.

    New rows land disabled like everyone else, so an existing install gains a user to switch on
    rather than a row that appears on the owner's Home unannounced.
    """
    if account is None or owner_account_id is None:
        return None
    if int(account.get("id") or 0) != owner_account_id:
        # The stored Plex token no longer belongs to the owner this instance was claimed by. Building
        # a row from THIS account's history and labelling it as the owner's would hand one person
        # another's picks, so skip — loudly, because it also means the token needs re-linking.
        logger.warning(
            "plex.tv token belongs to account {} but this server's owner is {} — owner not synced",
            account.get("id"),
            owner_account_id,
        )
        return None
    # `username`, never `title`: PMS names the owner's LOCAL account after their plex.tv username
    # ("S_FLIX"), not their display title ("SFLIX_Admin") — and that name is how their watch history
    # is found (see PlexClient.system_account_id + tests/fixtures/pms_accounts.xml.txt).
    username = account.get("username") or account.get("title") or "owner"
    # The owner is in Tautulli like anyone else, so their `{user}` row should honour the name set
    # there rather than falling straight through to their Plex username.
    friendly = (friendly_names or {}).get(owner_account_id, "")
    # Re-linking Plex under a different admin leaves the PREVIOUS owner marked `owner` forever, and
    # that type is the one `sync_user_restrictions` skips — so an account that is no longer the owner
    # must lose the badge, or it keeps its "never restricted" exemption on a server it merely shares.
    # SHARED is the safe landing type (it is simply "restrictable"); anyone still on the share was
    # already re-typed correctly by the roster loop, which commits before this runs.
    for stale in session.query(User).filter(
        User.user_type == UserType.OWNER.value, User.plex_account_id != owner_account_id
    ):
        logger.warning("{} is no longer this server's owner — demoting to a shared user", stale.username)
        stale.user_type = UserType.SHARED.value
    user = session.query(User).filter_by(plex_account_id=owner_account_id).one_or_none()
    if user is None:
        session.add(
            User(
                plex_account_id=owner_account_id,
                username=username,
                slug=unique_slug(session, username),
                avatar_url=account.get("thumb") or "",
                friendly_name=friendly,
                user_type=UserType.OWNER.value,
            )
        )
        return "added"
    user.username = username
    user.avatar_url = account.get("thumb") or ""
    user.friendly_name = friendly or user.friendly_name
    user.user_type = UserType.OWNER.value  # a pre-existing row for this account was never really "shared"
    return "updated"


@router.post("/sync")
async def sync_users(request: Request) -> dict:
    """Pull shared + Home users — and the owner — from plex.tv into the users table (idempotent)."""
    state = request.app.state
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        token = store.get("plex.token")
        server = session.query(Server).first()
    if not token or server is None:
        raise HTTPException(status_code=409, detail="Plex is not connected yet")
    machine_id = server.machine_id
    owner_account_id = server.owner_account_id
    client_id = state.client_id
    with state.sessions() as session:
        store = SettingsStore(session, state.secrets)
        tautulli_url, tautulli_key = store.get("tautulli.url"), store.get("tautulli.apikey")

    def fetch() -> tuple[list, dict | None, dict[int, str]]:
        # machine_id comes from the server table — no PMS round-trip needed to talk to plex.tv.
        users = PlexTvClient(token, machine_id).list_users()
        try:
            account = plextv_account(token, client_id)
        except Exception as e:
            # The owner is a bonus here; the shared users we already fetched are the point. Failing
            # the whole sync over it would leave the roster stale for everybody.
            logger.warning("could not read the owner account from plex.tv ({})", type(e).__name__)
            account = None
        friendly: dict[int, str] = {}
        if tautulli_url:
            try:
                friendly = TautulliClient(tautulli_url, tautulli_key or "").friendly_names()
            except Exception as e:
                # Nicer row titles are a bonus too — never fail a roster sync for them.
                logger.warning("could not read friendly names from Tautulli ({})", type(e).__name__)
        return users, account, friendly

    remote, owner_account, friendly_names = await asyncio.get_running_loop().run_in_executor(None, fetch)
    added = updated = 0
    # if plex.tv ever does list the owner, `_sync_owner` is the one that writes them — not both
    roster = [r for r in remote if r.id != owner_account_id]
    with state.sessions() as session:
        before = {u.id: u.nickname or u.friendly_name or u.username for u in session.query(User)}
        for r in roster:
            user = session.query(User).filter_by(plex_account_id=r.id).one_or_none()
            if user is None:
                session.add(
                    User(
                        plex_account_id=r.id,
                        username=r.username,
                        slug=unique_slug(session, r.username),
                        avatar_url=r.avatar_url,
                        user_type=r.user_type.value,
                        friendly_name=friendly_names.get(r.id, ""),
                    )
                )
                added += 1
            else:
                user.username = r.username
                user.avatar_url = r.avatar_url
                user.user_type = r.user_type.value
                # Refreshed every sync so a rename in Tautulli follows through — but `nickname`
                # (the owner's own choice) is never touched, so an override always survives.
                user.friendly_name = friendly_names.get(r.id, user.friendly_name)
                updated += 1
        # A Tautulli rename changes what `{user}` renders to, exactly like a nickname edit — and the
        # rows already on Plex still carry the old title. Without the same reconcile `patch_user`
        # does, a multi-row user keeps the stale copy alongside the new one forever: `remove_row`
        # matches by rendered title, so no sweep ever collects it.
        display_changed = _display_names_drifted(session, before)
        session.commit()

    # The owner gets their OWN transaction, deliberately. The roster above is the point of this
    # endpoint and is now safely committed; anything the owner upsert hits — a plex.tv payload that
    # isn't shaped how we expect, a slug collision — must not roll back everybody else's update.
    with state.sessions() as session:
        try:
            owner = _sync_owner(session, owner_account, owner_account_id, friendly_names)
            session.commit()
        except Exception as e:
            # redact: a plex.tv/DB error can carry a tokened URL (rule 9), like every other handler here.
            logger.warning("could not sync the server owner ({}: {})", type(e).__name__, redact(str(e)))
            owner = None
    if owner == "added":
        added += 1
    elif owner == "updated":
        updated += 1
    with state.sessions() as session:  # the owner's own name can drift on the same sync
        display_changed = display_changed or _display_names_drifted(session, before)
    if display_changed:
        await _rename_after_nickname(state)
    return {"added": added, "updated": updated, "total": len(roster) + (1 if owner else 0)}
