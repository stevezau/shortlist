"""Users API: list with badges, enable/prefs, sync from plex.tv.

The roster reconciliation itself lives in ``services/user_sync.py`` — it has no HTTP in it and the
nightly job is its other caller, so it is not this layer's to own.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from shortlist.engine.clients.http_retry import redact
from shortlist.engine.models import DEFAULT_ROW_TEMPLATE
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.api.serializers import UserOut, UserPickOut, pick_dict, user_dict
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    Event,
    PickRow,
    Run,
    RunUser,
    SharedRowWatch,
    User,
    iso_utc,
)
from shortlist.server.prefs import blocked_entries
from shortlist.server.services import jobs, report_service
from shortlist.server.services.user_sync import (
    remove_users_rows,
    rename_after_nickname,
)
from shortlist.server.services.watch_events import _as_utc
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_owner)])


class BlockSeedBody(BaseModel):
    tmdb_id: int
    title: str = ""
    media_type: str = ""  # movie | show; blank on a client that predates it
    year: int | None = None


class UserPrefs(BaseModel):
    # `row_size` and `max_rating` used to live here. Neither did anything: max_rating filtered no
    # content at all, and a row's own size always won. Per-person row size lives on the row override
    # (PUT /users/{id}/rows/{collection_id}), which the UI actually exposes.
    row_name_tpl: str | None = None
    excluded_genres: list[str] | None = None
    # int entries are the original storage shape and are still accepted for ever; dict entries carry
    # the title, so the UI can show a name instead of "tmdb 346648". See `blocked_entries`.
    # `BlockSeedBody`, not a bare `dict`: an untyped element publishes as an opaque object in the
    # OpenAPI schema, so the SPA's generated type lost the record shape and had to re-declare it.
    blocked_seeds: list[int | BlockSeedBody] | None = None
    paused: bool | None = None

    @field_validator("row_name_tpl")
    @classmethod
    def _no_seed_in_a_per_user_name(cls, value: str | None) -> str | None:
        """A per-user row name may not use ``{top_seed}``.

        A name that follows a watch needs a fallback for people who have not got one, and that field
        is per-ROW — there is no per-USER one. Delivery does pass the ROW's fallback whichever template
        won, so this override is only safe when the row already has one; set on a row without, it
        leaves exactly the person who set it with a default row that stops being built while their
        history is thin. Nothing surfaces who is in that state either: the "no name for newcomers"
        alert reads rows, not user prefs. So the honest answer is to refuse it and point at the field
        that has a fallback beside it.
        """
        if value and "{top_seed}" in value:
            raise ValueError(
                "a per-person row name can't use {top_seed} — it needs a fallback name for people "
                "with nothing watched yet, and that lives on the row, not the person. Set it on the "
                "row instead."
            )
        return value


class UserPatch(BaseModel):
    enabled: bool | None = None
    # False = never touch this account's Plex sharing settings again, and take back out the excludes
    # already added. The account can then see other people's rows, so the UI says so at the point of
    # the choice; the API takes the owner at their word.
    manage_sharing: bool | None = None
    # What to call them in a row title. "" clears the override and falls back to Tautulli's friendly
    # name, then their Plex username. Never touches the slug, so their label (and every share filter
    # that excludes it) is unaffected.
    nickname: str | None = Field(default=None, max_length=255)
    request_tag: str | None = Field(default=None, max_length=64)  # tag added to titles requested for this user
    prefs: UserPrefs | None = None


class BulkEnabled(BaseModel):
    enabled: bool


class BulkEnabledOut(PassthroughModel):
    """What `POST /users/set-enabled` did: how many were touched, and how many had rows removed."""

    updated: int
    cleaned: int
    enabled: bool


class BlockedSeedOut(PassthroughModel):
    """One blocked seed, always as a RECORD.

    The bare-int list an old install stores is normalised by `blocked_entries` before it reaches here,
    so this shape is what both endpoints return whichever way the prefs are stored — see
    `shortlist/server/prefs.py`.
    """

    tmdb_id: int
    title: str
    media_type: str
    year: int | None


class BlockedSeedsOut(PassthroughModel):
    blocked_seeds: list[BlockedSeedOut]


class TitleMatchOut(PassthroughModel):
    """TMDB's own best guess for a title search, for the "block a seed" picker."""

    tmdb_id: int | None  # None if TMDB answered without an id — the caller can't block that one
    title: str
    media_type: str
    year: int | None


class UserRunOut(PassthroughModel):
    """One run as it went for ONE person: their outcome, not the run's."""

    run_id: int
    started_at: str | None
    finished_at: str | None
    status: str
    error: str | None
    reason: str
    duration_ms: int
    run_status: str | None
    dry_run: bool
    # Free-form: `{}` for a user the run left alone, else some subset of added/removed/kept/deleted.
    # Which keys exist varies by what happened, so a model with defaults would invent the rest.
    diff: dict
    picks: list[UserPickOut]


class UserRunsSummaryOut(PassthroughModel):
    included: int
    total: int


class WatchItemOut(PassthroughModel):
    """One recent watch, from the same source recommendations are built from."""

    title: str
    tmdb_id: int | None  # None with no tmdb:// GUID — such a watch cannot be blocked as a seed
    media_type: str
    watched_at: str
    year: int | None
    season: int | None
    episode: int | None
    episode_title: str | None


class WatchedTitleOut(PassthroughModel):
    """One TITLE from the cached watched set — the set recommendations are actually filtered against.

    One title, not one stored row: a title held in two Plex libraries is cached once per library, and
    those copies are merged here (issue #111). `libraries` names the ones it was found in, and every
    other field is merged to the claim the engine acts on — see `_merge_watched_copies`.
    """

    title: str
    tmdb_id: int | None
    media_type: str
    watched_at: str
    year: int | None
    watch_count: int
    # Display names of the Plex libraries holding this title, sorted. Usually one; two or more is the
    # duplicate this page used to render as separate rows. Empty for rows cached before 0087, whose
    # library name is filled in by that person's next sync.
    libraries: list[str]
    # A show's progress straight from Plex. Both None for movies and for anything reporting no
    # episode totals — which is NOT the same claim as "none of it watched", so the UI must not
    # render 0 of 0 for it.
    viewed_leaf_count: int | None
    leaf_count: int | None
    # What THIS person rated it in Plex, 0..10, or None if they never did — which is almost always.
    # Read with their own share token, so it is their rating and nobody else's.
    user_rating: float | None


class WatchedLibraryOut(PassthroughModel):
    """One Plex library this person has a cached watch in.

    The `media_type` is what lets the page decide whether a library filter is worth showing: one
    library per type means the Movies/Shows buttons already draw every distinction a library choice
    could, and a second control offering the same two words is noise (#111).
    """

    name: str
    media_type: str  # movie | show


class WatchedPageOut(PassthroughModel):
    """A page of the watched set, plus how complete the set behind it is.

    The staleness fields travel WITH the page on purpose: this list is a cache, and a page that
    doesn't say when it was last filled invites "I watched that, why is it recommended?" — the exact
    question this endpoint exists to answer.
    """

    items: list[WatchedTitleOut]
    # How many TITLES match the filters — the same thing `items` counts. Smaller than `synced_titles`
    # on a server that holds anything in two libraries.
    total: int
    # Every library this person has a cached watch in, sorted, for the page's library filter. Never
    # narrowed by the `library` parameter, or picking one would empty the control that picked it.
    libraries: list[WatchedLibraryOut]
    # None when any library has never had a full read — see `user_watched`.
    last_full_sync_at: str | None
    # Rows in the cache: one per library COPY, summed across this person's libraries. Deliberately
    # not the same number as `total` — the UI says "library copies" so the two can't read as a
    # contradiction.
    synced_titles: int
    # At or below this 0..10 rating, a title stops seeding this person's rows. None = Plex ratings
    # are switched off server-wide, so no rating is acting on anything.
    dislike_threshold: float | None
    # False when this account's ratings look tool-written (Kometa and friends sync IMDb scores into
    # the same field) — none of them are used, whatever the threshold says. The page has to state
    # this, or a row of visible low ratings that change nothing reads as a broken feature.
    ratings_trusted: bool
    # How many titles they have rated AT ALL, across the whole set rather than this page — the
    # difference between "nobody rates things" and "you are looking at the wrong page".
    rated_count: int


class UserSyncOut(PassthroughModel):
    added: int
    updated: int
    total: int


def _watch_depths(session) -> dict[int, int]:
    """user_id -> how many DISTINCT watched titles we last read for them.

    ``prefs["history_depth"]`` is the count of their watched set from the last read, which the
    share-token source returns as one item per distinct title (a 40-episode binge is one title). The
    daily ``sync_watched`` job refreshes it for EVERY enabled user (and the owner), not just ones a
    run processed — so a skipped or never-run user no longer shows "0 titles" forever (the beta bug
    that reported 0 for all 42 accounts while the log showed watches synced).
    """
    depths: dict[int, int] = {}
    for user in session.query(User).all():
        depth = (user.prefs or {}).get("history_depth")
        if isinstance(depth, int):
            depths[user.id] = depth
    return depths


def _unhidden_row_counts(session) -> dict[str, int]:
    """username -> how many of OTHER people's rows the last run found this account able to see.

    Plex refuses a label hide-list for a managed account with a parental Restriction Profile, so those
    accounts are left out of the privacy filters entirely. Shortlist used to assume that was harmless
    because such an account sees no collections at all — true of `little_kid`, false of `older_kid`.
    The run now measures it instead of assuming, and this is how the measurement reaches the UI.

    Keyed by username because that is the identifier the engine has at run time: the slug is ours, and
    a nickname can change between the run and this request.
    """
    # The latest run that MEASURED, not merely the latest that finished — see `_rows_we_cannot_hide`
    # in notifications.py. An errored or aborted run carries no measurement, and reading it as "nobody
    # is exposed" would wipe the badge off the Users list while the exposure is still there.
    run = next(
        (
            r
            for r in session.query(Run).filter(Run.finished_at.isnot(None)).order_by(Run.finished_at.desc()).limit(50)
            if "unhideable_rows" in (r.stats or {})
        ),
        None,
    )
    exposed = ((run.stats or {}).get("unhideable_rows") or {}) if run else {}
    return {name: len(keys) for name, keys in exposed.items()}


@router.get("", response_model=list[UserOut])
def list_users(request: Request) -> list[dict]:
    """Every user with their badges, watch depth, lifetime hit rate and a pick preview.

    Deliberately a plain `def`, not `async def`: it issues four synchronous queries PER USER,
    which on a 40-account server is ~160 round-trips. On the event loop that stalls SSE,
    `/api/system/health` and every other request for the duration; as a sync handler Starlette
    runs it in a worker thread instead.
    """
    with request.app.state.sessions() as session:
        depths = _watch_depths(session)
        exposed = _unhidden_row_counts(session)
        out = []
        for user in session.query(User).filter(User.removed_at.is_(None)).order_by(User.username).all():
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
                user_dict(
                    user,
                    depths.get(user.id, 0),
                    last.run.finished_at if last else None,
                    hit_rate,
                    preview,
                    exposed.get(user.username, 0),
                )
            )
        return out


@router.post("/set-enabled", response_model=BulkEnabledOut)
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
    await remove_users_rows(state, to_clean)
    if reinstated:
        await jobs.queue_privacy_sync(state, f"{reinstated} people were turned back on")
    return {"updated": total, "cleaned": len(to_clean), "enabled": body.enabled}


@router.patch("/{user_id}", response_model=UserOut)
async def patch_user(user_id: int, patch: UserPatch, request: Request) -> dict:
    state = request.app.state
    disabled_slug: str | None = None
    enabled_slug: str | None = None
    paused_slug: str | None = None
    unpaused_slug: str | None = None
    sharing_slug: tuple[str, bool] | None = None  # (slug, now managed?) when the setting actually changed
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
        if patch.manage_sharing is not None and user.user_type == "owner":
            # Plex has no share filters for the account that owns the server (rule 5), so this flag
            # can never mean anything for them. Ignored rather than stored: persisting it would badge
            # the owner "Sharing untouched" in the Users list, which describes a state that does not
            # exist. The UI already hides the switch for them; this is the same answer at the API.
            patch.manage_sharing = None
        if patch.manage_sharing is not None and patch.manage_sharing != user.manage_sharing:
            # Both directions need the same pass, and it is the same pass everything else uses:
            # `privacy.sync` is `engine_run(ctx, [])`, which walks every account's filter and builds,
            # delivers and promotes nothing. Turning management OFF makes it remove our excludes from
            # this one account; turning it back ON merges them in again. Neither creates a row, so the
            # leak-safe ordering of §12 has nothing to order here — no row becomes visible that was not
            # already on the server.
            sharing_slug = (user.slug, patch.manage_sharing)
            user.manage_sharing = patch.manage_sharing
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
        result = user_dict(
            user,
            _watch_depths(session).get(user.id, 0),
            None,
            None,
            unhidden_rows=_unhidden_row_counts(session).get(user.username, 0),
        )
    if disabled_slug is not None:
        await remove_users_rows(state, [disabled_slug])
    if enabled_slug is not None:
        await jobs.queue_privacy_sync(state, f"'{enabled_slug}' was turned back on")
    if sharing_slug is not None:
        slug, managed = sharing_slug
        # Both read as a clause after the job toast's "Share filters merged for every account
        # after …", the same shape the enable/disable reasons already use.
        await jobs.queue_privacy_sync(
            state,
            f"'{slug}' was set back to managed Plex sharing"
            if managed
            else f"'{slug}' was set to leave their Plex sharing alone",
        )
    if paused_slug is not None:
        await _hide_paused_users_rows(state, paused_slug)
    if unpaused_slug is not None:
        await _restore_paused_users_rows(state, unpaused_slug)
    # A nickname changes what `{user}` renders to, so this person's existing collections carry a title
    # no future run will write. Renaming them in place is the same reconcile a row rename uses; without
    # it a multi-row user keeps the old-named copy alongside the new one.
    await rename_after_nickname(state, was_called)
    return result


@router.post("/{user_id}/blocked-seeds", response_model=BlockedSeedsOut)
async def block_seed(user_id: int, body: BlockSeedBody, request: Request) -> dict:
    """Stop a title being used as a seed for this person's recommendations.

    Blocking a seed does not ban the title outright — it stops that watch from SHAPING their picks,
    which is the thing you want after a one-off that isn't representative of them.
    """
    with request.app.state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        prefs = dict(user.prefs or {})
        entries = [e for e in blocked_entries(prefs) if e["tmdb_id"] != body.tmdb_id]
        entries.append(
            {
                "tmdb_id": body.tmdb_id,
                "title": body.title,
                "media_type": body.media_type,
                "year": body.year,
            }
        )
        entries.sort(key=lambda e: (e["title"].lower(), e["tmdb_id"]))
        prefs["blocked_seeds"] = entries
        user.prefs = prefs
        session.commit()
    return {"blocked_seeds": entries}


@router.delete("/{user_id}/blocked-seeds/{tmdb_id}", response_model=BlockedSeedsOut)
async def unblock_seed(user_id: int, tmdb_id: int, request: Request) -> dict:
    """Unblock a title so it can be used as a seed again."""
    with request.app.state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        prefs = dict(user.prefs or {})
        entries = [e for e in blocked_entries(prefs) if e["tmdb_id"] != tmdb_id]
        prefs["blocked_seeds"] = entries
        user.prefs = prefs
        session.commit()
    return {"blocked_seeds": entries}


@router.get("/search/titles", response_model=list[TitleMatchOut])
async def search_titles(request: Request, q: str, media_type: str = "movie") -> list[dict]:
    """Look a title up on TMDB, for the "block a seed" picker.

    Owner-gated like the rest of this router. Returns at most one match per query — TMDB's own best
    guess — because the only thing the caller needs is a tmdb_id to attach a name to.
    """
    if media_type not in ("movie", "show"):
        raise HTTPException(status_code=422, detail="media_type must be 'movie' or 'show'")
    query = q.strip()
    if not query:
        return []
    # The requests-only context: a TMDB client without connecting to the PMS or building the LLM
    # curator, neither of which a title lookup needs.
    _config, tmdb = request.app.state.run_service.build_requests_context()
    if tmdb is None:
        raise HTTPException(status_code=503, detail="TMDB is not configured — add an API key in Settings.")

    def lookup():
        return tmdb.search(query, media_type)

    try:
        found = await asyncio.get_running_loop().run_in_executor(None, lookup)
    except Exception as e:
        logger.warning("TMDB title search failed ({})", type(e).__name__)
        raise HTTPException(status_code=502, detail=redact(f"{type(e).__name__}: {e}")) from e
    if not found:
        return []
    date = found.get("release_date") or found.get("first_air_date") or ""
    return [
        {
            "tmdb_id": found.get("id"),
            "title": found.get("title") or found.get("name") or query,
            "media_type": media_type,
            "year": int(date[:4]) if date[:4].isdigit() else None,
        }
    ]


class UserPickOutcomeOut(PassthroughModel):
    """One title this person was recommended and then played."""

    tmdb_id: int
    media_type: str
    title: str
    #: The row that was showing it when they pressed play.
    row: str
    #: `finished` | `dropped` | `bounced` | `watching`. See `resolve_outcomes` — `watching` means
    #: either no percentage was ever observed, or it is too recent to call (`SETTLING_HOURS`).
    outcome: str
    #: How far in they got, or null when no live session ever measured it. Always null for a series:
    #: an episode's progress is not the show's.
    percent: int | None
    watched_at: str | None
    finished_at: str | None


@router.get("/{user_id}/outcomes", response_model=list[UserPickOutcomeOut])
def user_outcomes(user_id: int, request: Request) -> list[dict]:
    """What this person did with the picks they were given: finished, part-watched, or abandoned.

    The user page could show what Shortlist DELIVERED and what they had watched on Plex, but not the
    join of the two — whether the recommendations were actually seen out. That is the question the
    dashboard answers for the whole server, and it is at least as interesting per person.

    Reuses `resolve_outcomes`, the same function the dashboard reads, rather than re-deriving the
    classification here. Two places deciding what "finished" means is how the user page and the
    dashboard come to disagree about the same title.

    A plain `def`: `resolve_outcomes` is synchronous and walks the picks table, so Starlette runs it
    in a worker thread instead of stalling the event loop (see the effectiveness handler).
    """
    with request.app.state.sessions() as session:
        if session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="user not found")
        outcomes = report_service.resolve_outcomes(session, None)
        # Filtered on the USER only. An extra `watched_at is not None` test used to sit here, which
        # was redundant with `resolve_outcomes`' own gate for the ordinary case and actively wrong for
        # the rest: an entry it lets through — finished, never separately credited — is one the
        # dashboard counts and this page hid, so the two disagreed about the same title. One place
        # decides what an outcome is, and it is not this one.
        mine = [(key, entry) for key, entry in outcomes.items() if key[0] == user_id]
        namer = report_service._RowNamer(
            session, SettingsStore(session).get("row.name_template") or DEFAULT_ROW_TEMPLATE
        )
        # Newest first: "what did they just watch" is the question, not "what did they watch in 2019".
        #
        # Sorted on the DATETIME, with a floor — not on `str(...)`. Dropping the `watched_at` filter
        # above admitted one new class, finished-but-never-separately-credited, and those carry
        # `watched_at = None`. `str(None)` is `"None"`, which compares ABOVE every `"2026-…"`, so
        # reversed it sorted to the very top: an untimestamped row from any era announced as the most
        # recent thing they watched.
        floor = datetime.min.replace(tzinfo=UTC)
        mine.sort(key=lambda kv: _as_utc(kv[1]["watched_at"] or kv[1]["finished_at"] or floor), reverse=True)
        return [
            {
                "tmdb_id": key[1],
                "media_type": key[2],
                "title": entry["title"],
                "row": namer.label(entry["row"], entry["library"]),
                "outcome": entry["outcome"],
                "percent": entry["percent"],
                "watched_at": iso_utc(entry["watched_at"]) if entry["watched_at"] else None,
                "finished_at": iso_utc(entry["finished_at"]) if entry["finished_at"] else None,
            }
            for key, entry in mine
        ]


@router.get("/{user_id}/runs", response_model=list[UserRunOut])
async def user_runs(user_id: int, request: Request, limit: int = Query(15, ge=1, le=50)) -> list[dict]:
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
                    # Why a non-failing outcome happened ("no watch history yet"). Without it a
                    # `skipped` reads as a failure, which is the opposite of what it means.
                    "reason": ru.reason or "",
                    "duration_ms": ru.duration_ms,
                    "run_status": run.status if run else None,
                    "dry_run": run.dry_run if run else False,
                    "diff": ru.diff or {},
                    "picks": [pick_dict(p) for p in picks],
                }
            )
        return out


@router.get("/{user_id}/runs/summary", response_model=UserRunsSummaryOut)
async def user_runs_summary(user_id: int, request: Request) -> dict:
    """How many runs included this person, against how many there have been.

    A run is server-wide, so "6 runs" on a person's page is only honest next to "of 148" — otherwise
    the page reads as though the server has run six times.
    """
    with request.app.state.sessions() as session:
        if session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="user not found")
        included = session.query(func.count(RunUser.user_id)).filter(RunUser.user_id == user_id).scalar() or 0
        total = session.query(func.count(Run.id)).scalar() or 0
        return {"included": included, "total": total}


@router.get("/{user_id}/history", response_model=list[WatchItemOut])
async def user_history(user_id: int, request: Request, limit: int = Query(25, ge=1, le=100)) -> list[dict]:
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


@router.get("/{user_id}/watched", response_model=WatchedPageOut)
async def user_watched(
    user_id: int,
    request: Request,
    q: str = Query("", max_length=200, description="Case-insensitive substring of the title."),
    media_type: str = Query("", pattern="^(movie|show)?$"),
    library: str = Query("", max_length=255, description="Display name of a Plex library; empty for all."),
    limit: int = Query(25, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    """Search one person's watched set.

    Reads the local `watched_titles` cache, so unlike `/history` it never touches Plex: it is a DB
    query, it can search the WHOLE set rather than the page on screen, and it shows the same titles
    the recommender excludes from. One row per TITLE — a title held in two libraries is merged, and
    names both.
    """
    page = request.app.state.run_service.user_watched(
        user_id, q=q, media_type=media_type, library=library, limit=limit, offset=offset
    )
    if page is None:
        raise HTTPException(status_code=404, detail="user not found")
    return page


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


@router.post("/sync", response_model=UserSyncOut)
async def sync_users(request: Request) -> dict:
    """Pull shared + Home users — and the owner — from plex.tv into the users table (idempotent).

    Queued as a `sync.users` JOB and drained inline, rather than called directly. `sync.users` is a
    WRITER despite its name — it renames Shortlist collections on the PMS when a display name has
    drifted — and both things that make that safe live in the job runner, not in the function:
    `_claimable` refuses to start a writer while a run is in flight, and the runner holds
    `plex_writer_lock()` for the duration. Calling it straight from here bypassed both, so pressing
    "Sync from Plex" mid-run could rename a collection the run was converging against. The run
    matches collections by rendered TITLE, so that makes a live row look orphaned — and converge
    deletes orphans. (jobs-and-runs-design.md §12; the CATALOG entry for this kind spells out the
    same failure.)

    Drained inline so the button still returns the counts it always has.
    """
    from shortlist.server.db.models import Job
    from shortlist.server.services.jobs import enqueue, run_pending

    state = request.app.state
    job_id = enqueue(state.sessions, "sync.users")
    await run_pending(state)
    with state.sessions() as session:
        job = session.get(Job, job_id)
        result = dict(job.result or {})
        # Read INSIDE the session. It worked outside only because the factory is
        # `expire_on_commit=False` and `close()` detaches without expiring — a property of the
        # factory, not of this code, and one that would turn into a DetachedInstanceError the day it
        # changed. `job` is never None (we just enqueued it), but reading it here says so.
        status = job.status if job is not None else "queued"
    # Still queued means a run holds the writer lock. The row stays, the worker retries — say so
    # rather than reporting a sync that has not happened yet.
    result.setdefault("added", 0)
    result.setdefault("updated", 0)
    result.setdefault("total", 0)
    result["queued"] = status in ("queued", "running")
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


class RemovedOut(PassthroughModel):
    """What `DELETE /users/{id}` dropped. `user_id` is still valid — the row is archived, not deleted."""

    user_id: int
    picks_deleted: int
    runs_deleted: int


@router.delete("/{user_id}", response_model=RemovedOut)
async def remove_departed_user(user_id: int, request: Request) -> dict:
    """File away someone Plex no longer has: drop their picks and run history, hide them from the list.

    NOT a delete, and the difference is load-bearing. `restriction_snapshots` is keyed to `users.id`
    with `ON DELETE RESTRICT` (migration 0055) and holds the only copy of this account's share filters
    as they were BEFORE Shortlist touched them — what uninstall restores from (plex-safety rule 2).
    Uninstall skips any snapshot whose user row has gone, so deleting the row would quietly cost that
    person their restore. The row stays as the snapshot's anchor and leaves the UI instead.

    Refused for anyone still on the share (409): on an active account this would read as "delete this
    user", dropping their history while the nightly run keeps rebuilding their row.

    Their share-filter excludes are not touched here. Once their collection is gone, the next privacy
    pass prunes the dead label on its own — under both guards in `sync_user_restrictions`, which is a
    safer place for that decision than a button.
    """
    state = request.app.state
    with state.sessions() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="user not found")
        if user.departed_at is None:
            raise HTTPException(
                status_code=409,
                detail=f"{user.display_name} still shares this server — turn them off instead of removing them",
            )
        picks = session.query(PickRow).filter_by(user_id=user_id).delete(synchronize_session=False)
        # Shared-row watches go with the picks: they are the same fact about the same person for a row
        # that happens to have no pick rows, and leaving them would keep a departed account in the
        # engagement report after their history was dropped.
        session.query(SharedRowWatch).filter_by(user_id=user_id).delete(synchronize_session=False)
        runs = session.query(RunUser).filter_by(user_id=user_id).delete(synchronize_session=False)
        user.removed_at = datetime.now(UTC)
        user.enabled = False
        session.add(
            Event(
                scope="user.removed",
                level="warning",
                message={
                    "user": user.username,
                    "slug": user.slug,
                    "picks_deleted": picks,
                    "runs_deleted": runs,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()
        logger.warning("{} removed by the owner — {} picks and {} run rows dropped", user.username, picks, runs)
        return {"user_id": user_id, "picks_deleted": picks, "runs_deleted": runs}
