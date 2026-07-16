"""Collections API: define curated rows — how each is built (per-person | shared), who it's for
(audience), and its recipe (size, media, name, prompt). Owner-only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func

from shortlist.engine.candidates import KNOWN_SOURCES
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.curator.base import TONE_PRESETS
from shortlist.engine.delivery import (
    DEFAULT_ROW_NAME,
    remove_row_collections,
    rename_row_collections,
    render_row_name,
    row_marker,
)
from shortlist.engine.models import SHARED_LABEL_PREFIX, UserProfile, UserType, dedupe_slug, slugify
from shortlist.server.auth import require_owner
from shortlist.server.db.models import DEFAULT_SLUG, Collection, CollectionAudience, Event, Run, User

router = APIRouter(prefix="/collections", tags=["collections"], dependencies=[Depends(require_owner)])

# Slugs reserved by the engine: `probe` is the throwaway Privacy Check row; `shared` prefixes every
# shared collection's label. A user-defined collection may not claim either.
RESERVED_SLUGS = {"probe", "shared"}

BUILDS = {"per_person", "shared"}
AUDIENCES = {"everyone", "subset"}
MEDIA = {"movie", "show", "both"}
PLACEMENTS = {"both", "home", "library"}


class PromptIn(BaseModel):
    """A row's curation recipe. EVERY field is blank-means-inherit-the-global-one.

    `tone` defaulted to "balanced", which is indistinguishable from "unset" — so a row could never
    inherit Settings -> Curation style, and every row silently overrode it with a bare balanced
    recipe. Blank is the only honest default.
    """

    tone: str = ""
    guidance: str = ""
    template: str = ""


class HubAnchorIn(BaseModel):
    """A per-library shelf anchor for one row: sit after (or before) a collection, by title."""

    anchor: str = Field(min_length=1, max_length=255)
    before: bool = False


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    build: str = "per_person"
    audience: str = "everyone"
    audience_user_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    size: int = Field(default=15, ge=5, le=40)
    media: str = "both"
    sort_order: int = 0
    name_template: str = ""
    min_watchers: int = Field(default=2, ge=2)  # a public row must never be shaped by one person
    request_tag: str = Field(default="", max_length=64)  # tag added to titles requested via this row
    candidate_sources: list[str] = Field(default_factory=list)  # [] -> inherit global candidates.sources
    watched_pct: float | None = Field(default=None, ge=0.0, le=1.0)  # None -> inherit global watched cap
    freshness: float | None = Field(default=None, ge=0.0, le=1.0)  # None -> inherit global freshness
    library_keys: list[str] = Field(default_factory=list)  # [] -> every library of the row's media type
    placement: str = "both"  # both | home | library — which surfaces the row shows on
    pin_top: bool = False  # pin to top of the library's Recommended shelf
    # Per-library Recommended-shelf override for this row, keyed by section key. {} -> inherit the
    # global default (settings `rows.hub_anchor`).
    hub_anchor: dict[str, HubAnchorIn] = Field(default_factory=dict)
    prompt: PromptIn = Field(default_factory=PromptIn)


def _validate(body: CollectionIn) -> None:
    if body.build not in BUILDS:
        raise HTTPException(422, f"build must be one of {sorted(BUILDS)}")
    if body.audience not in AUDIENCES:
        raise HTTPException(422, f"audience must be one of {sorted(AUDIENCES)}")
    if body.media not in MEDIA:
        raise HTTPException(422, f"media must be one of {sorted(MEDIA)}")
    unknown = [s for s in body.candidate_sources if s not in KNOWN_SOURCES]
    if unknown:
        raise HTTPException(422, f"unknown candidate source(s) {unknown}; valid: {sorted(KNOWN_SOURCES)}")
    if body.prompt.tone and body.prompt.tone not in TONE_PRESETS:
        raise HTTPException(422, f"unknown tone {body.prompt.tone!r}; valid: {sorted(TONE_PRESETS)} (or blank)")
    if body.placement not in PLACEMENTS:
        raise HTTPException(422, f"placement must be one of {sorted(PLACEMENTS)}")


def _serialize(session, collection: Collection) -> dict:
    audience_ids = [
        row.user_id for row in session.query(CollectionAudience).filter_by(collection_id=collection.id).all()
    ]
    return {
        "id": collection.id,
        "slug": collection.slug,
        "name": collection.name,
        "build": collection.build,
        "audience": collection.audience,
        "audience_user_ids": audience_ids,
        "enabled": collection.enabled,
        "size": collection.size,
        "media": collection.media,
        "sort_order": collection.sort_order,
        "name_template": collection.name_template,
        "min_watchers": collection.min_watchers,
        "request_tag": collection.request_tag or "",
        "candidate_sources": list(collection.candidate_sources or []),
        "watched_pct": collection.watched_pct,
        "freshness": collection.freshness,
        "placement": collection.placement or "both",
        "pin_top": bool(collection.pin_top),
        "hub_anchor": collection.hub_anchor or {},
        "library_keys": [str(k) for k in (collection.library_keys or [])],
        "prompt": collection.prompt or {},
    }


def _prompt_for(slug: str, body: CollectionIn) -> dict:
    """The recipe to persist for ``slug`` — always empty on the default row, which the engine
    curates with the global recipe. Keeps DB state from disagreeing with what a run will do."""
    return {} if slug == DEFAULT_SLUG else body.prompt.model_dump()


def _reject_duplicate_name(session, name: str, *, exclude_id: int | None = None) -> None:
    """Two rows with the same name render to the same Plex collection title and would collide into
    one (silent data loss). Names must be distinct (case-insensitively)."""
    clash = (
        session.query(Collection)
        .filter(func.lower(Collection.name) == name.strip().lower())
        .filter(Collection.id != exclude_id)
        .first()
    )
    if clash is not None:
        raise HTTPException(422, f"a row named {name!r} already exists — pick a different name")


def _unique_slug(session, base: str) -> str:
    base = base if base not in RESERVED_SLUGS else f"{base}_row"
    return dedupe_slug(base, lambda slug: session.query(Collection).filter_by(slug=slug).first() is not None)


def _set_audience(session, collection: Collection, body: CollectionIn) -> None:
    session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
    if body.audience == "subset":
        for user_id in dict.fromkeys(body.audience_user_ids):  # dedupe, keep order
            session.add(CollectionAudience(collection_id=collection.id, user_id=user_id))


@router.get("")
async def list_collections(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        collections = session.query(Collection).order_by(Collection.sort_order, Collection.id).all()
        return [_serialize(session, c) for c in collections]


@router.post("", status_code=201)
async def create_collection(body: CollectionIn, request: Request) -> dict:
    _validate(body)
    with request.app.state.sessions() as session:
        _reject_duplicate_name(session, body.name)
        slug = _unique_slug(session, slugify(body.name))
        collection = Collection(
            slug=slug,
            name=body.name,
            build=body.build,
            audience=body.audience,
            enabled=body.enabled,
            size=body.size,
            media=body.media,
            sort_order=body.sort_order,
            name_template=body.name_template,
            min_watchers=body.min_watchers,
            request_tag=body.request_tag.strip(),
            candidate_sources=body.candidate_sources,
            watched_pct=body.watched_pct,
            freshness=body.freshness,
            placement=body.placement,
            pin_top=body.pin_top,
            hub_anchor={k: v.model_dump() for k, v in body.hub_anchor.items()},
            library_keys=body.library_keys,
            prompt=_prompt_for(slug, body),
        )
        session.add(collection)
        session.flush()
        _set_audience(session, collection, body)
        session.commit()
        return _serialize(session, collection)


# Columns a PATCH may set directly, name (needs a dup check) and audience/prompt (need shaping)
# handled separately.
_PATCHABLE_COLUMNS = (
    "build",
    "audience",
    "enabled",
    "size",
    "media",
    "sort_order",
    "name_template",
    "min_watchers",
    "request_tag",
    "candidate_sources",
    "watched_pct",
    "freshness",
    "placement",
    "pin_top",
    "library_keys",
)


@router.patch("/{collection_id}")
async def update_collection(collection_id: int, body: CollectionIn, request: Request) -> dict:
    _validate(body)
    # Only touch fields the request actually sent, so a partial PATCH (e.g. an enable toggle) never
    # resets the columns it omitted back to CollectionIn's defaults.
    sent = body.model_fields_set
    state = request.app.state
    dropped_user_ids: set[int] = set()
    new_row_template: str | None = None  # set when a rename should be reconciled onto Plex
    slug = build = None
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        slug, build = collection.slug, collection.build
        is_default = collection.slug == DEFAULT_SLUG
        # Capture the audience BEFORE the patch so we can tell WHO was dropped (their row is now stale
        # on Plex and must be removed). "everyone" resolves to the full user set.
        touching_audience = build == "per_person" and bool(sent & {"audience", "audience_user_ids"})
        if touching_audience:
            all_ids = {u.id for u in session.query(User).all()}
            old_users = (
                all_ids
                if collection.audience == "everyone"
                else {a.user_id for a in session.query(CollectionAudience).filter_by(collection_id=collection.id)}
            )
        # A rename only matters for a NON-default per-person row (the default row's title follows the
        # global Settings template, not this column). Capture the old effective template to tell whether
        # the title actually changed — delivery renders from `name_template or name`.
        touching_name = build == "per_person" and not is_default and bool(sent & {"name", "name_template"})
        old_template = (collection.name_template or collection.name) if touching_name else None
        if "name" in sent:
            _reject_duplicate_name(session, body.name, exclude_id=collection_id)
            collection.name = body.name
        for column in _PATCHABLE_COLUMNS:
            if column in sent:
                setattr(collection, column, getattr(body, column))
        if "prompt" in sent:
            collection.prompt = _prompt_for(collection.slug, body)
        if "hub_anchor" in sent:
            collection.hub_anchor = {k: v.model_dump() for k, v in body.hub_anchor.items()}
        if sent & {"audience", "audience_user_ids"}:
            _set_audience(session, collection, body)
        session.commit()
        if touching_audience:
            new_users = (
                all_ids
                if collection.audience == "everyone"
                else {a.user_id for a in session.query(CollectionAudience).filter_by(collection_id=collection.id)}
            )
            dropped_user_ids = old_users - new_users  # users no longer in the audience → clean them up
        if touching_name:
            new_effective = collection.name_template or collection.name
            if new_effective != old_template:
                new_row_template = new_effective
        result = _serialize(session, collection)
    build_changed = "build" in sent and body.build != build

    # A build flip (per-person ↔ shared) makes the OLD build's collections stale — a shared collection,
    # or every user's per-person one, under a label the new build won't touch. Remove them so the next
    # run rebuilds the row cleanly under its new build; otherwise both live on Home at once. This is a
    # removal (gate-exempt), and it supersedes the audience/rename reconciles (which act on the old
    # build that's being fully removed). Best-effort + audited.
    if build_changed:
        await _run_reconcile(state, slug=slug, build=build, dry_run=False, scope="collection.build")
        return result
    # Removing a dropped user's row is a removal (gate-exempt); a newly-ADDED user's row is a create,
    # so it's left for the next run's gated delivery. Best-effort + audited.
    if dropped_user_ids:
        await _run_reconcile(
            state, slug=slug, build=build, dry_run=False, scope="collection.audience", only_user_ids=dropped_user_ids
        )
    # A rename updates each user's collection title IN PLACE (multi-row users would otherwise keep the
    # old-named copy until the next run rebuilt it). Privacy-neutral, so gate-exempt. Best-effort + audited.
    if new_row_template is not None:
        await _run_row_rename(state, slug=slug, new_template=new_row_template, scope="collection.rename")
    return result


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(collection_id: int, request: Request) -> None:
    state = request.app.state
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        if collection.slug == DEFAULT_SLUG:
            # The default per-person row is what makes an upgrade behaviour-neutral; disable it
            # instead of deleting so there's always a home for users with no other row.
            raise HTTPException(422, "the default 'picked' row can't be deleted — disable it instead")
        slug, build = collection.slug, collection.build

    # Remove the row's Plex collections FIRST — while we still have the slug + the last run's breakdown
    # to find them — then drop the DB row. Best-effort: if Plex is down or not yet configured the
    # cleanup is audited and we still remove the config row (no worse than before, when delete never
    # cleaned up at all); when Plex is reachable this leaves nothing orphaned.
    await _run_reconcile(state, slug=slug, build=build, dry_run=False, scope="collection.delete")
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is not None:
            session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
            session.delete(collection)
            session.commit()


class CleanupRequest(BaseModel):
    dry_run: bool = False  # preview which collections would be removed (rule 8)


@router.post("/{collection_id}/cleanup")
async def cleanup_collection(collection_id: int, body: CleanupRequest, request: Request) -> dict:
    """Remove this row's collections from Plex, for everyone who has it, without waiting for a run.

    Removal only — it never creates or promotes, so it is gate-exempt (deleting a row can only make
    the server more private, the same reasoning as the remedy pass and uninstall). A per-person row's
    collection for each user is pinned by the exact title the last run delivered (recorded in that
    run's breakdown); a shared row is addressed by its own label. dry_run previews the plan.
    """
    state = request.app.state
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        slug, build, name = collection.slug, collection.build, collection.name

    removed, error = await _run_reconcile(
        state, slug=slug, build=build, dry_run=body.dry_run, scope="collection.cleanup"
    )
    if error:
        raise HTTPException(502, f"Cleanup failed part-way; removed {len(removed)} before: {error}")
    verb = "Would remove" if body.dry_run else "Removed"
    return {
        "removed": removed,
        "dry_run": body.dry_run,
        "message": f"{verb} {len(removed)} collection(s) for “{name}”.",
    }


def _reconcile_row_removal(
    state, *, slug: str, build: str, dry_run: bool, removed: list[str], only_user_ids: set[int] | None = None
) -> None:
    """Remove a row's collections from Plex. Accumulates the display titles into the ``removed``
    out-param (so a mid-loop PMS failure still leaves the partial list for the audit).

    Shared rows go by their own label (one membership); per-person rows are pinned per user by the
    exact title the last run delivered for THIS row (its persisted breakdown), scoped to that user's
    own label — so it can never reach another user's row or a foreign (Kometa) collection.
    ``only_user_ids`` limits the per-person sweep to specific users (audience-shrink cleanup); ``None``
    means everyone (delete-row / manual cleanup). Removal only, so gate-exempt. Runs in an executor."""
    ctx = state.run_service.build_context(dry_run=dry_run)
    if build == "shared":
        # A shared row is one collection for everyone; who SEES it is a share-filter concern handled
        # by the next run's privacy sync, not a per-user collection to remove here.
        if only_user_ids is None:
            removed.extend(
                remove_row_collections(
                    ctx.plex, ctx.config, label=f"{SHARED_LABEL_PREFIX}{slug}", displays=None, dry_run=dry_run
                )
            )
        return
    with state.sessions() as session:
        latest = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
        breakdown_by_user = {ru.user_id: (ru.breakdown or []) for ru in latest.users} if latest else {}
        users = session.query(User).all()
    for user in users:
        if only_user_ids is not None and user.id not in only_user_ids:
            continue
        displays = {
            entry["row_title"]
            for entry in breakdown_by_user.get(user.id, [])
            if entry.get("row_slug") == slug and entry.get("row_title")
        }
        if not displays:
            continue
        removed.extend(
            remove_row_collections(
                ctx.plex, ctx.config, label=f"{ctx.config.label_prefix}_{user.slug}", displays=displays, dry_run=dry_run
            )
        )


async def _run_reconcile(
    state, *, slug: str, build: str, dry_run: bool, scope: str, only_user_ids: set[int] | None = None
) -> tuple[list[str], str | None]:
    """Run ``_reconcile_row_removal`` in an executor and audit it (rule 10) — even a mid-loop failure
    records what was already removed. Returns ``(removed, error)``."""
    removed: list[str] = []
    error: str | None = None
    try:
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: _reconcile_row_removal(
                state, slug=slug, build=build, dry_run=dry_run, removed=removed, only_user_ids=only_user_ids
            ),
        )
    except Exception as e:  # a destructive write is never silent: audit the partial removal, then surface it
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    with state.sessions() as session:
        session.add(
            Event(
                scope=scope,
                level="warn",
                message={
                    "slug": slug,
                    "removed": removed,
                    "dry_run": dry_run,
                    "error": error,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()
    logger.warning("{} '{}': {} collection(s){}", scope, slug, len(removed), f" then FAILED: {error}" if error else "")
    return removed, error


def _reconcile_row_rename(state, *, slug: str, new_template: str, entries: list[dict]) -> None:
    """Rename a per-person row's collections IN PLACE for every user who has it — multi-row users would
    otherwise keep the old-named copy alongside the one the next run builds under the new name.

    Each user's collection is found by the exact title the last run delivered for THIS row (its
    persisted breakdown), scoped to that user's own label, and renamed to the freshly-rendered new
    title (same account marker). Privacy-neutral, so gate-exempt (the hiding filter is keyed on the
    label, which never changes here). STATIC titles only: a ``{top_seed}`` template renders to the
    default row's name with no picks, so a dynamic new template is skipped — its title changes every
    run anyway, and the next run's delivery already renames the sole-row case. Runs in an executor.

    Accumulates one ``{user, old, new, libraries}`` entry per user actually renamed into ``entries``,
    so the audit can answer "whose row went from what to what, in which libraries" (rule 10)."""
    with state.sessions() as session:
        latest = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
        breakdown_by_user = {ru.user_id: (ru.breakdown or []) for ru in latest.users} if latest else {}
        users = session.query(User).all()
    ctx = state.run_service.build_context(dry_run=False)
    for user in users:
        old_titles = {
            entry["row_title"]
            for entry in breakdown_by_user.get(user.id, [])
            if entry.get("row_slug") == slug and entry.get("row_title")
        }
        if not old_titles:
            continue
        profile = UserProfile(
            username=user.username,
            plex_account_id=user.plex_account_id,
            user_type=UserType(user.user_type),
            slug=user.slug,
        )
        new_display = render_row_name(new_template, profile, [])
        if new_display == DEFAULT_ROW_NAME:
            logger.debug("rename reconcile: '{}' renders to the default title with no picks — left for a run", slug)
            continue
        marker = row_marker(user.plex_account_id)
        for old_display in old_titles:
            if old_display == new_display:
                continue  # this user's title didn't actually change (e.g. a {user} template)
            libraries = rename_row_collections(
                ctx.plex,
                ctx.config,
                label=f"{ctx.config.label_prefix}_{user.slug}",
                marker=marker,
                old_display=old_display,
                new_display=new_display,
                dry_run=False,
            )
            if libraries:
                entries.append({"user": user.slug, "old": old_display, "new": new_display, "libraries": libraries})


async def _run_row_rename(state, *, slug: str, new_template: str, scope: str) -> tuple[list[dict], str | None]:
    """Run ``_reconcile_row_rename`` in an executor and audit it with per-user old→new detail (rule 10).
    Best-effort — a Plex outage is logged, never fatal to the PATCH. Returns ``(rename_entries, error)``."""
    entries: list[dict] = []
    error: str | None = None
    try:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: _reconcile_row_rename(state, slug=slug, new_template=new_template, entries=entries)
        )
    except Exception as e:
        error = redact(f"{type(e).__name__}: {e}")  # a PMS error can carry a tokened URL (rule 9)
    with state.sessions() as session:
        session.add(
            Event(
                scope=scope,
                level="info",
                message={
                    "slug": slug,
                    "renames": entries,  # per user: {user, old, new, libraries} — answers rule 10's "whose, what→what"
                    "new_template": new_template,
                    "error": error,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()
    total = sum(len(e["libraries"]) for e in entries)
    logger.info(
        "{} '{}': renamed {} collection(s) for {} user(s){}",
        scope,
        slug,
        total,
        len(entries),
        f" then FAILED: {error}" if error else "",
    )
    return entries, error
