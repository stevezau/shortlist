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
from shortlist.engine.curator.base import TONE_PRESETS
from shortlist.engine.delivery import remove_row_collections
from shortlist.engine.models import SHARED_LABEL_PREFIX, dedupe_slug, slugify
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


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    build: str = "per_person"
    audience: str = "everyone"
    audience_user_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    size: int = Field(default=15, ge=5, le=30)
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
    with request.app.state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        if "name" in sent:
            _reject_duplicate_name(session, body.name, exclude_id=collection_id)
            collection.name = body.name
        for column in _PATCHABLE_COLUMNS:
            if column in sent:
                setattr(collection, column, getattr(body, column))
        if "prompt" in sent:
            collection.prompt = _prompt_for(collection.slug, body)
        if sent & {"audience", "audience_user_ids"}:
            _set_audience(session, collection, body)
        session.commit()
        return _serialize(session, collection)


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(collection_id: int, request: Request) -> None:
    with request.app.state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        if collection.slug == DEFAULT_SLUG:
            # The default per-person row is what makes an upgrade behaviour-neutral; disable it
            # instead of deleting so there's always a home for users with no other row.
            raise HTTPException(422, "the default 'picked' row can't be deleted — disable it instead")
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

    # `removed` lives out here so a mid-loop PMS failure still audits what was already deleted (rule 10).
    removed: list[str] = []

    def do_cleanup() -> None:
        ctx = state.run_service.build_context(dry_run=body.dry_run)
        if build == "shared":
            # A shared row carries its own label, one membership — no per-user titles needed.
            removed.extend(
                remove_row_collections(
                    ctx.plex, ctx.config, label=f"{SHARED_LABEL_PREFIX}{slug}", displays=None, dry_run=body.dry_run
                )
            )
            return
        # Per-person rows share each user's label, so pin the exact collection by the title the last
        # run delivered for THIS row (from that run's persisted breakdown).
        with state.sessions() as session:
            latest = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
            breakdown_by_user = {ru.user_id: (ru.breakdown or []) for ru in latest.users} if latest else {}
            users = session.query(User).all()
        for user in users:
            displays = {
                entry["row_title"]
                for entry in breakdown_by_user.get(user.id, [])
                if entry.get("row_slug") == slug and entry.get("row_title")
            }
            if not displays:
                continue
            removed.extend(
                remove_row_collections(
                    ctx.plex,
                    ctx.config,
                    label=f"{ctx.config.label_prefix}_{user.slug}",
                    displays=displays,
                    dry_run=body.dry_run,
                )
            )

    error: str | None = None
    try:
        await asyncio.get_running_loop().run_in_executor(None, do_cleanup)
    except Exception as e:  # audit whatever WAS removed before re-raising — a destructive write is never silent
        error = f"{type(e).__name__}: {e}"
    with state.sessions() as session:
        session.add(
            Event(
                scope="collection.cleanup",
                level="warn",
                message={
                    "slug": slug,
                    "removed": removed,
                    "dry_run": body.dry_run,
                    "error": error,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
        session.commit()
    logger.warning("cleanup {} for row '{}': {} collection(s){}", "preview" if body.dry_run else "removed", slug, len(removed), f" then FAILED: {error}" if error else "")  # noqa: E501
    if error:
        raise HTTPException(502, f"Cleanup failed part-way; removed {len(removed)} before: {error}")
    verb = "Would remove" if body.dry_run else "Removed"
    return {"removed": removed, "dry_run": body.dry_run, "message": f"{verb} {len(removed)} collection(s) for “{name}”."}
