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
    iso_utc,
)
from shortlist.server.services import jobs
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
        "library": pick.library or "",
        "section_key": pick.section_key or "",
        "seed_title": pick.seed_title,
        # Provenance: which source(s) surfaced it, and how strongly they vouched. Blank/1.0 on rows
        # written before 0035, which the UI renders as "not recorded" rather than "a perfect match".
        "sources": [s for s in (pick.sources or "").split(",") if s],
        "affinity": pick.affinity,
    }


class UserPrefs(BaseModel):
    # `row_size` and `max_rating` used to live here. Neither did anything: max_rating filtered no
    # content at all, and a row's own size always won. Per-person row size lives on the row override
    # (PUT /users/{id}/rows/{collection_id}), which the UI actually exposes.
    row_name_tpl: str | None = None
    excluded_genres: list[str] | None = None
    blocked_seeds: list[int] | None = None
    paused: bool | None = None


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
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "user_type": user.user_type,
        "restricted": user.restricted,
        # "" when no parental preset is set. A managed account WITHOUT one is an ordinary user that
        # gets rows and privacy filters; one WITH a preset sees no collections at all and Plex refuses
        # to filter it. The UI needs the difference to say anything true (#20).
        "restriction_profile": user.restriction_profile or "",
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
    """user_id -> how many DISTINCT watched titles we last read for them.

    ``prefs["history_depth"]`` is the count of their watched set from the last read, which the
    share-token source returns as one item per distinct title (a 40-episode binge is one title). The
    daily ``sync_watched`` job refreshes it for EVERY enabled user, not just ones a run processed — so
    a skipped or never-run user no longer shows "0 titles" forever (the beta bug that reported 0 for
    all 42 accounts while the log showed watches synced).
    """
    depths: dict[int, int] = {}
    for user in session.query(User).all():
        depth = (user.prefs or {}).get("history_depth")
        if isinstance(depth, int):
            depths[user.id] = depth
    return depths


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


async def _remove_users_rows(state, user_slugs: list[str]) -> None:
    """Remove ALL of every just-disabled user's Shortlist collections (their whole label).

    Queued as a durable job rather than done inline. This path used to be fire-and-forget: if Plex
    was down at the moment of disabling, the removal was lost and NOTHING ever retried it, because
    no run revisits a disabled user — so their rows stayed on Plex indefinitely. As a job it is
    retried with backoff, survives a container restart, and raises a notification if it finally
    gives up. Removal only, so still gate-exempt (it can only make the server more private).

    Deleting their collections is only half of "disabled". The other half is their OWN share filter:
    with `privacy.hide_shared_from_disabled`, an opted-out account must stop seeing even the PUBLIC
    shared rows, and only a filter write does that. Without the second job, disabling someone removed
    their row from everyone else's Home and left every shared row on theirs.

    Everything is queued before anything drains, so it runs in that order out of one FIFO queue:
    every cleanup deletes its collections, then ONE privacy pass computes excludes from what is
    actually left. Taking the whole list at once is what keeps "disable all" to a single pass rather
    than one full share-filter sweep per user.
    """
    from shortlist.server.services.jobs import enqueue, queue_privacy_sync

    if not user_slugs:
        return
    for slug in user_slugs:
        enqueue(state.sessions, "user.cleanup", {"slug": slug})
    # Drains straight away so the operator sees it happen now — the queue is the SAFETY NET, not a
    # delay. If this attempt fails (Plex down, container killed mid-write) the jobs are still there
    # and the worker retries them; before, that work was simply lost.
    reason = (
        f"'{user_slugs[0]}' was turned off" if len(user_slugs) == 1 else f"{len(user_slugs)} people were turned off"
    )
    await queue_privacy_sync(state, reason)


@router.post("/set-enabled")
async def set_all_users_enabled(body: BulkEnabled, request: Request) -> dict:
    """Enable or disable EVERY user at once. Disabling removes each newly-disabled user's rows from
    Plex now and writes their share filters — the same cleanup the per-user toggle does, so 'off'
    means gone, not merely 'not refreshed'. Enabling gives back the shared rows that disabling hid;
    their own rows rebuild on the next run. Best-effort + audited."""
    state = request.app.state
    to_clean: list[str] = []
    reinstated = 0
    with state.sessions() as session:
        users = session.query(User).all()
        for user in users:
            if body.enabled is False and user.enabled:
                to_clean.append(user.slug)  # was on, now off -> remove their rows from Plex
            if body.enabled is True and not user.enabled:
                reinstated += 1  # was off, now on -> the excludes that hid every shared row must go
            user.enabled = body.enabled
        session.commit()
        total = len(users)
    await _remove_users_rows(state, to_clean)
    if reinstated:
        await jobs.queue_privacy_sync(state, f"{reinstated} people were turned back on")
    return {"updated": total, "cleaned": len(to_clean), "enabled": body.enabled}


@router.patch("/{user_id}")
async def patch_user(user_id: int, patch: UserPatch, request: Request) -> dict:
    state = request.app.state
    disabled_slug: str | None = None
    enabled_slug: str | None = None
    paused_slug: str | None = None
    unpaused_slug: str | None = None
    was_called: dict[str, str] = {}  # {slug -> the display name their collections are still titled with}
    with state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if patch.enabled is not None:
            if user.enabled and patch.enabled is False:
                # Turned off → remove their rows from Plex now, not just stop delivering to them.
                disabled_slug = user.slug
            if not user.enabled and patch.enabled is True:
                # Turned back on → the excludes that hid every shared row from them must come off.
                # Their own row is a rebuild, so that part still waits for the next run.
                enabled_slug = user.slug
            user.enabled = patch.enabled
        if patch.nickname is not None:
            nickname = patch.nickname.strip()
            # Checked whether it is being SET or CLEARED: clearing falls back to the Tautulli or
            # Plex name, which is just as capable of colliding as one that was typed.
            _reject_display_name_clash(session, user, nickname or user.friendly_name or user.username)
            if nickname != (user.nickname or ""):
                was_called[user.slug] = user.display_name  # captured BEFORE the write
            user.nickname = nickname
        if patch.request_tag is not None:
            user.request_tag = patch.request_tag.strip()
        if patch.prefs is not None:
            prefs = dict(user.prefs or {})
            was_paused = bool(prefs.get("paused"))
            prefs.update({k: v for k, v in patch.prefs.model_dump().items() if v is not None})
            user.prefs = prefs
            # Pausing means "stop showing their row", so it has to come down NOW — a paused person is
            # absent from every run by definition, so nothing else would ever act on it. Unpausing is
            # the exact mirror: the collections still exist, they are merely demoted, so putting them
            # back is a re-promote. Leaving it to "the next run" was wrong — a row whose schedule is
            # blank has no next run, and neither does one while `paused_all` is set, so an unpaused
            # person could stay invisible indefinitely.
            now_paused = bool(prefs.get("paused"))
            if now_paused and not was_paused:
                paused_slug = user.slug
            elif was_paused and not now_paused:
                unpaused_slug = user.slug
        session.commit()
        result = _serialize(user, _watch_depths(session).get(user.id, 0), None, None)
    if disabled_slug is not None:
        await _remove_users_rows(state, [disabled_slug])
    if enabled_slug is not None:
        await jobs.queue_privacy_sync(state, f"'{enabled_slug}' was turned back on")
    if paused_slug is not None:
        await _hide_paused_users_rows(state, paused_slug)
    if unpaused_slug is not None:
        await _restore_paused_users_rows(state, unpaused_slug)
    # A nickname changes what `{user}` renders to, so this person's existing collections carry a title
    # no future run will write. Renaming them in place is the same reconcile a row rename uses; without
    # it a multi-row user keeps the old-named copy alongside the new one.
    await _rename_after_nickname(state, was_called)
    return result


async def _rename_after_nickname(state, was_called: dict[str, str]) -> None:
    """Re-render every per-person row's titles so a name change lands on Plex now, not next run.

    Reuses the row-rename reconcile with each row's UNCHANGED template. What moved is not the template
    but what `{user}` renders to, so `was_called` ({user slug -> their PREVIOUS display name}) is how
    the collection already on Plex is recognised: rendering the CURRENT name on both sides would match
    nothing, leave the old-titled collection in place, and let the next run build a second one beside
    it. Best-effort and privacy-neutral — titles move, the label (and every filter excluding it) doesn't.
    """
    from shortlist.server.db.models import Collection
    from shortlist.server.services.collection_reconcile import row_template, run_row_rename_from_plex

    if not was_called:
        return
    with state.sessions() as session:
        # `row_template`, not `c.name_template`: the DEFAULT row deliberately has no per-collection
        # template — its title IS the global `row.name_template`. Reading the column left it blank, so
        # the `{user}` test below always failed and a nickname change never touched the one row nearly
        # every server actually has.
        rows = [
            (c.slug, row_template(session, c.slug, state.secrets))
            for c in session.query(Collection).filter_by(enabled=True, build="per_person").all()
        ]
    for slug, template in rows:
        if "{user}" not in (template or ""):
            continue  # this row's title doesn't mention them, so a name change can't have moved it
        await run_row_rename_from_plex(
            state,
            slug=slug,
            new_template=template,
            old_template=template,
            scope="user.nickname",
            old_display_names=was_called,
        )


class BlockSeedBody(BaseModel):
    tmdb_id: int
    title: str = ""


@router.post("/{user_id}/blocked-seeds")
async def block_seed(user_id: int, body: BlockSeedBody, request: Request) -> dict:
    """Block a title from being used as a seed for this user's recommendations."""
    with request.app.state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        prefs = dict(user.prefs or {})
        blocked = set(prefs.get("blocked_seeds") or [])
        blocked.add(body.tmdb_id)
        prefs["blocked_seeds"] = sorted(blocked)
        user.prefs = prefs
        session.commit()
    return {"blocked_seeds": sorted(blocked)}


@router.delete("/{user_id}/blocked-seeds/{tmdb_id}")
async def unblock_seed(user_id: int, tmdb_id: int, request: Request) -> dict:
    """Unblock a title so it can be used as a seed again."""
    with request.app.state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        prefs = dict(user.prefs or {})
        blocked = set(prefs.get("blocked_seeds") or [])
        blocked.discard(tmdb_id)
        prefs["blocked_seeds"] = sorted(blocked)
        user.prefs = prefs
        session.commit()
    return {"blocked_seeds": sorted(blocked)}


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


def _display_names_drifted(session: Session, before: dict[int, str]) -> dict[str, str]:
    """{slug -> their PREVIOUS display name} for every EXISTING user whose `{user}` name just changed.

    The previous name, not merely the fact that one changed: it is what their collections on Plex are
    still titled with, and therefore the only way to find them. New users are excluded deliberately —
    they have no rows on Plex yet, so there is nothing to rename and no reason to make a sync do Plex I/O.
    """
    return {
        user.slug: before[user.id]
        for user in session.query(User)
        if user.id in before and user.display_name != before[user.id]
    }


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
    demoted: list[str] | None = None,
) -> str | None:
    """Add (or refresh) the server owner as a user. Returns "added", "updated", or None if skipped.

    ``demoted`` collects the usernames of accounts that LOST the owner badge. That is a privacy event,
    not a cosmetic one: an owner is never restricted (rule 5), so while typed owner an account has no
    `label!=` excludes at all. The moment it becomes a shared user it needs the full set, and nothing
    else would notice — the caller queues a share-filter pass when this list is non-empty.

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
    # `username`, never `title`: store the owner's plex.tv username ("S_FLIX"), not their display
    # title ("SFLIX_Admin"), to match how every other account is keyed on the roster.
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
        if demoted is not None:
            demoted.append(stale.username)
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
    return await sync_users_from_state(request.app.state)


async def sync_users_from_state(state) -> dict:
    """The roster sync itself, taking `app.state` rather than a Request.

    Split out so the scheduled sync can run as a durable JOB. It used to build a fake `Request` just
    to hand this function something to read `app.state` off — which meant the only caller that runs
    unattended was also the one going through the most contrived path.

    Streams ``sync.progress``/``sync.finished`` (``kind="users"``) over the SSE bus so the Jobs page
    can show a live bar: an indeterminate ``fetch`` phase for the one opaque plex.tv round-trip, then
    a determinate ``save`` bar over the roster upsert.
    """
    bus = state.bus

    def emit(event: str, data: dict) -> None:
        # This handler runs on the event loop, so publish directly — no thread hop needed.
        bus.publish(event, {"kind": "users", **data})

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

    emit("sync.progress", {"phase": "fetch"})  # indeterminate: one opaque plex.tv + Tautulli round-trip
    remote, owner_account, friendly_names = await asyncio.get_running_loop().run_in_executor(None, fetch)
    added = updated = 0
    # if plex.tv ever does list the owner, `_sync_owner` is the one that writes them — not both
    roster = [r for r in remote if r.id != owner_account_id]
    total = len(roster) + 1  # + the owner's own transaction below
    emit("sync.progress", {"phase": "save", "done": 0, "total": total})
    with state.sessions() as session:
        before = {u.id: u.nickname or u.friendly_name or u.username for u in session.query(User)}
        for i, r in enumerate(roster, start=1):
            user = session.query(User).filter_by(plex_account_id=r.id).one_or_none()
            if user is None:
                session.add(
                    User(
                        plex_account_id=r.id,
                        username=r.username,
                        slug=unique_slug(session, r.username),
                        avatar_url=r.avatar_url,
                        user_type=r.user_type.value,
                        restricted=r.restricted,
                        restriction_profile=r.restriction_profile,
                        friendly_name=friendly_names.get(r.id, ""),
                    )
                )
                added += 1
            else:
                user.username = r.username
                user.avatar_url = r.avatar_url
                user.user_type = r.user_type.value
                user.restricted = r.restricted
                user.restriction_profile = r.restriction_profile
                # Refreshed every sync so a rename in Tautulli follows through — but `nickname`
                # (the owner's own choice) is never touched, so an override always survives.
                user.friendly_name = friendly_names.get(r.id, user.friendly_name)
                updated += 1
            emit("sync.progress", {"phase": "save", "done": i, "total": total})
        # A Tautulli rename changes what `{user}` renders to, exactly like a nickname edit — and the
        # rows already on Plex still carry the old title. Without the same reconcile `patch_user`
        # does, a multi-row user keeps the stale copy alongside the new one forever: `remove_row`
        # matches by rendered title, so no sweep ever collects it.
        display_changed = _display_names_drifted(session, before)
        # Anyone Shortlist knows who is NO LONGER on the share has lost access to the server, so their
        # rows must come down. Nothing used to notice: they stayed enabled, so every run tried their
        # dead share token and failed, and their collection sat promoted to Shared Home indefinitely
        # with no user left to see it. Turning them OFF (rather than deleting the row) reuses the whole
        # tested disable path — remove their collections, then write the filters — and keeps their
        # history, so if they come back the owner switches them on again.
        #
        # Bounded twice over, because this runs UNATTENDED on the daily user sync and its follow-up
        # DELETES collections. "plex.tv returned nothing" is indistinguishable from "nobody shares this
        # server any more", and a truncated read is indistinguishable from a mass departure — so an
        # incomplete picture must do nothing rather than guess, the same way `_converge_phase` gates
        # orphan deletion on a non-empty user list.
        on_roster = {r.id for r in roster} | ({owner_account_id} if owner_account_id else set())
        enabled = session.query(User).filter(User.enabled).all()
        departed = [u.slug for u in enabled if u.plex_account_id not in on_roster] if roster else []
        if not roster:
            logger.info("plex.tv listed no shared users — skipping the departure sweep rather than disabling everyone")
        elif len(departed) > 1 and len(departed) * 2 >= len(enabled):
            # One person leaving the share is routine and acts immediately. HALF of them leaving at once
            # is not a thing that happens — it is a partial read — and acting on it would switch off a
            # whole server's worth of people, each needing a manual re-enable and a full LLM rebuild.
            # Reported as a notification rather than silently dropped, so a genuine mass removal is
            # still visible and the owner can do it by hand.
            logger.error(
                "plex.tv says {} of {} enabled users left the share — refusing to act on what looks like "
                "a partial read. Disable them by hand if it is real.",
                len(departed),
                len(enabled),
            )
            session.add(
                Event(
                    scope="user.departed.refused",
                    level="error",
                    message={"users": departed, "enabled": len(enabled), "at": datetime.now(UTC).isoformat()},
                )
            )
            departed = []
        for user in session.query(User).filter(User.slug.in_(departed)) if departed else []:
            logger.warning("{} no longer shares this server — turning them off and removing their rows", user.username)
            user.enabled = False
        if departed:
            session.add(
                Event(
                    scope="user.departed",
                    level="warn",
                    message={"users": departed, "at": datetime.now(UTC).isoformat()},
                )
            )
        session.commit()

    # The owner gets their OWN transaction, deliberately. The roster above is the point of this
    # endpoint and is now safely committed; anything the owner upsert hits — a plex.tv payload that
    # isn't shaped how we expect, a slug collision — must not roll back everybody else's update.
    ex_owners: list[str] = []
    with state.sessions() as session:
        try:
            owner = _sync_owner(session, owner_account, owner_account_id, friendly_names, demoted=ex_owners)
            session.commit()
        except Exception as e:
            # redact: a plex.tv/DB error can carry a tokened URL (rule 9), like every other handler here.
            logger.warning("could not sync the server owner ({}: {})", type(e).__name__, redact(str(e)))
            owner = None
    if owner == "added":
        added += 1
    elif owner == "updated":
        updated += 1
    emit("sync.progress", {"phase": "save", "done": total, "total": total})
    with state.sessions() as session:  # the owner's own name can drift on the same sync
        display_changed = {**display_changed, **_display_names_drifted(session, before)}
    await _rename_after_nickname(state, display_changed)
    with state.sessions() as session:
        SettingsStore(session, state.secrets).set("report.users_synced_at", datetime.now(UTC).isoformat())
        session.commit()
    # Everyone who lost access: rows off Plex, then filters. Queued before the new-account pass so one
    # drain covers both — and so the filters are computed once, after every collection change.
    await _remove_users_rows(state, departed)
    if added:
        await _hide_existing_rows_from_new_accounts(state, added)
    elif ex_owners:
        # An account demoted out of `owner` had NO excludes while it held the badge, because the owner
        # is the one account Plex will not restrict. It needs the full set now. Skipped when `added`
        # already queued a pass — it is the same work.
        await jobs.queue_privacy_sync(state, f"{', '.join(ex_owners)} is no longer the server owner")
    result = {"added": added, "updated": updated, "total": len(roster) + (1 if owner else 0)}
    emit("sync.finished", {"ok": True, **result})
    return result


async def _hide_paused_users_rows(state, user_slug: str) -> None:
    """Queue the take-down for a just-paused user, and drain it so it happens now.

    Durable rather than fire-and-forget for the same reason disable cleanup is: a paused user is
    absent from every subsequent run, so if this write is lost to a Plex outage nothing would ever
    retry it and their row would stay up indefinitely.
    """
    from shortlist.server.services.jobs import enqueue, run_pending

    enqueue(state.sessions, "user.hide", {"slug": user_slug})
    try:
        await run_pending(state)
    except Exception as e:
        logger.warning(
            "paused {} but their rows could not be hidden right now ({}: {}) — queued for retry",
            user_slug,
            type(e).__name__,
            redact(str(e)),
        )


async def _restore_paused_users_rows(state, user_slug: str) -> None:
    """Put an un-paused user's rows back.

    ONE job, deliberately. `user.restore` merges every account's share filters itself before promoting
    anything — plex-safety rule 1's ordering, in straight-line code. Splitting it into a queued
    `privacy.sync` followed by a queued `user.restore` would NOT have been ordered: a job whose retry
    backoff has not elapsed is stepped over, so a filter pass that failed against a 503 plex.tv would
    be skipped and the promotion would land anyway.
    """
    from shortlist.server.services.jobs import drain_now, enqueue

    enqueue(state.sessions, "user.restore", {"slug": user_slug})
    await drain_now(state, f"'{user_slug}' was un-paused")


async def _hide_existing_rows_from_new_accounts(state, added: int) -> None:
    """Write share filters immediately when accounts we have never seen appear on the roster.

    A brand-new account has NO ``label!=`` excludes, and every existing row is already promoted to
    Shared Home — so until something writes their filter they can see EVERY user's private row on
    their own Home screen. Waiting for the nightly run leaves that open for up to a day, which is
    precisely the leak the label system exists to prevent.

    Queued as a ``privacy.sync`` JOB rather than run inline. The work is a full engine pass (sweep +
    merge every share filter), and ``RunService`` holds a lock precisely so a nightly and a manual
    run can never overlap — firing it straight from this handler would bypass that lock and let two
    passes merge the same accounts' filters from independent roster snapshots, where a lost update
    silently drops an exclude. Going through the queue picks up the run-in-progress check, retries
    and durability across a restart, and keeps a multi-minute pass off the request path.
    """
    from shortlist.server.services.jobs import enqueue, run_pending

    enqueue(state.sessions, "privacy.sync", {})
    try:
        # Drain now when nothing else is writing to Plex; if something is, the worker picks it up.
        await run_pending(state)
        logger.info("{} new account(s) synced — share-filter pass queued so they cannot see other rows", added)
    except Exception as e:
        # Named loudly: until this succeeds the new accounts CAN see everyone's rows. The job stays
        # queued regardless, so the worker retries it with backoff.
        logger.warning(
            "{} new account(s) synced but the share-filter pass could not run now ({}: {}) — queued for retry",
            added,
            type(e).__name__,
            redact(str(e)),
        )
