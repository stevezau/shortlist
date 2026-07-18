"""Collections API: define curated rows — how each is built (per-person | shared), who it's for
(audience), and its recipe (size, media, name, prompt). Owner-only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func

from shortlist.engine.candidates import KNOWN_SOURCES
from shortlist.engine.curator.base import TONE_PRESETS
from shortlist.engine.models import dedupe_slug, slugify
from shortlist.server.auth import require_owner
from shortlist.server.db.models import DEFAULT_SLUG, Collection, CollectionAudience, Event, PickRow, User
from shortlist.server.scheduler import rebuild_schedule
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services import poster_service
from shortlist.server.services.poster_service import load_upload
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/collections", tags=["collections"], dependencies=[Depends(require_owner)])

# Slugs reserved by the engine: `shared` prefixes every shared collection's label, and `probe` is
# kept reserved for backward compatibility. A user-defined collection may not claim either.
RESERVED_SLUGS = {"probe", "shared"}

BUILDS = {"per_person", "shared"}
AUDIENCES = {"everyone", "subset"}
MEDIA = {"movie", "show", "both"}
PLACEMENTS = {"both", "home", "library"}
# "" (Plex default), "upload", "text" (built-in Pillow), "ai" (image model). "generate" is the
# pre-text-engine name for "ai", accepted for backward compatibility.
POSTER_MODES = {"", "upload", "text", "ai", "generate"}


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
    """A per-library shelf placement for one row: the very TOP (``top``), or after/before a collection
    by title. ``top`` needs no anchor; otherwise ``anchor`` must be a non-empty title."""

    anchor: str = Field(default="", max_length=255)
    before: bool = False
    top: bool = False


class PosterIn(BaseModel):
    """A row's custom-poster config. ``mode`` "" leaves Plex artwork alone; "upload" uses the image
    stored via the upload endpoint; "text" renders ``title``/``subtitle`` with the built-in Pillow
    engine (no AI); "ai" renders them with the curator provider's image model. The text fields share
    the row-name placeholders ({user}/{library_name}/{top_seed})."""

    mode: str = ""
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=120)
    style: str = Field(default="", max_length=400)


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    build: str = "per_person"
    audience: str = "everyone"
    audience_user_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    # This row's own run schedule (5-field cron); "" = never runs on a schedule. New rows default to
    # a nightly 03:30 so they work out of the box; there is no global schedule.
    schedule: str = Field(default="30 3 * * *", max_length=64)
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
    poster: PosterIn = Field(default_factory=PosterIn)


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
    if body.poster.mode not in POSTER_MODES:
        raise HTTPException(422, f"poster mode must be one of {sorted(POSTER_MODES)}")
    if body.schedule.strip():
        try:
            CronTrigger.from_crontab(body.schedule.strip())
        except ValueError as e:
            raise HTTPException(422, f"invalid schedule — needs a 5-field cron (e.g. '30 3 * * *'): {e}") from e
    for lib, anchor in body.hub_anchor.items():
        if not anchor.top and not anchor.anchor.strip():
            raise HTTPException(422, f"hub_anchor[{lib}]: needs 'top' or a non-empty 'anchor'")


def _poster_view(session, collection: Collection) -> dict:
    """The row's poster config for the editor — never the image bytes, just what's set plus whether an
    image is viewable (so the editor/row card can show a thumbnail via the image endpoint).

    A "text" poster is always renderable; "upload"/"ai" report an image only when one is stored/cached.
    """
    cfg = collection.poster or {}
    mode = (cfg.get("mode") or "").strip()
    if mode == "upload":
        has_image = load_upload(session, collection.id) is not None
    elif mode == "text":
        has_image = True
    elif mode in ("ai", "generate"):
        has_image = (
            poster_service.load_preview(
                session, mode, cfg.get("title") or "", cfg.get("subtitle") or "", cfg.get("style") or ""
            )
            is not None
        )
    else:
        has_image = False
    return {
        "mode": mode,
        "title": cfg.get("title") or "",
        "subtitle": cfg.get("subtitle") or "",
        "style": cfg.get("style") or "",
        "has_image": has_image,
    }


def _serialize(session, collection: Collection) -> dict:
    audience_ids = [
        row.user_id for row in session.query(CollectionAudience).filter_by(collection_id=collection.id).all()
    ]
    # The default row's real title is the global template (Settings → Defaults), which the engine
    # renders per library — not its stale seeded `name` column. Surface the template so the Rows UI
    # shows the actual default ("✨ {library_name} Picked for You"), consistent with what delivers.
    name = collection.name
    if collection.slug == DEFAULT_SLUG:
        name = SettingsStore(session).get("row.name_template") or collection.name
    # The most recent run that delivered picks for THIS row — so the Rows UI can link straight to what
    # happened (the run detail groups its results by row). None until the row has ever built.
    last_run_id = session.query(func.max(PickRow.run_id)).filter(PickRow.collection_slug == collection.slug).scalar()
    return {
        "id": collection.id,
        "slug": collection.slug,
        "name": name,
        "last_run_id": last_run_id,
        "build": collection.build,
        "audience": collection.audience,
        "audience_user_ids": audience_ids,
        "enabled": collection.enabled,
        "schedule": collection.schedule or "",
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
        "poster": _poster_view(session, collection),
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
            schedule=body.schedule.strip(),
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
            poster=body.poster.model_dump(),
        )
        session.add(collection)
        session.flush()
        _set_audience(session, collection, body)
        session.commit()
        result = _serialize(session, collection)
    rebuild_schedule(request.app)  # a new row may carry a schedule — register its cron job now
    return result


# Columns a PATCH may set directly, name (needs a dup check) and audience/prompt (need shaping)
# handled separately.
_PATCHABLE_COLUMNS = (
    "build",
    "audience",
    "enabled",
    "schedule",
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
    poster_reset_needed = False  # set when a row drops a custom poster back to Plex default
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
        # The default row's name follows the global Settings → Defaults template, never this column, so
        # a save of it is ignored here (the editor now shows the template as its name, and would
        # otherwise round-trip that value back into the column).
        if "name" in sent and not is_default:
            _reject_duplicate_name(session, body.name, exclude_id=collection_id)
            collection.name = body.name
        for column in _PATCHABLE_COLUMNS:
            if column in sent:
                setattr(collection, column, getattr(body, column))
        if "schedule" in sent:
            collection.schedule = body.schedule.strip()  # a whitespace-only cron means "no schedule"
        if "prompt" in sent:
            collection.prompt = _prompt_for(collection.slug, body)
        if "poster" in sent:
            old_poster_mode = (collection.poster or {}).get("mode") or ""
            collection.poster = body.poster.model_dump()
            # Switching a row that HAD a custom poster back to Plex default must actually revert the
            # artwork on Plex, not just stop managing it.
            poster_reset_needed = bool(old_poster_mode) and not body.poster.mode
            session.add(
                Event(
                    scope="collection.poster",
                    level="info",
                    message={
                        "slug": collection.slug,
                        "mode": body.poster.mode or "default",
                        "at": datetime.now(UTC).isoformat(),
                    },
                )
            )
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
    # A schedule or enable/disable change alters which cron jobs should exist — re-derive them.
    if sent & {"schedule", "enabled"}:
        rebuild_schedule(request.app)
    build_changed = "build" in sent and body.build != build

    # A build flip (per-person ↔ shared) makes the OLD build's collections stale — a shared collection,
    # or every user's per-person one, under a label the new build won't touch. Remove them so the next
    # run rebuilds the row cleanly under its new build; otherwise both live on Home at once. This is a
    # removal (gate-exempt), and it supersedes the audience/rename reconciles (which act on the old
    # build that's being fully removed). Best-effort + audited.
    if build_changed:
        await reconcile.run_reconcile(state, slug=slug, build=build, dry_run=False, scope="collection.build")
        return result
    # Removing a dropped user's row is a removal (gate-exempt); a newly-ADDED user's row is a create,
    # so it's left for the next run's gated delivery. Best-effort + audited.
    if dropped_user_ids:
        await reconcile.run_reconcile(
            state, slug=slug, build=build, dry_run=False, scope="collection.audience", only_user_ids=dropped_user_ids
        )
    # A rename updates each user's collection title IN PLACE (multi-row users would otherwise keep the
    # old-named copy until the next run rebuilt it). Privacy-neutral, so gate-exempt. Best-effort + audited.
    if new_row_template is not None:
        await reconcile.run_row_rename(state, slug=slug, new_template=new_row_template, scope="collection.rename")
    # Dropping a custom poster back to Plex default reverts the artwork on Plex now, not just in config.
    # Cosmetic + privacy-neutral, so gate-exempt. Best-effort + audited.
    if poster_reset_needed:
        await reconcile.run_poster_reset(state, slug=slug, build=build, scope="collection.poster")
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
    await reconcile.run_reconcile(state, slug=slug, build=build, dry_run=False, scope="collection.delete")
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is not None:
            session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
            session.delete(collection)
            session.commit()
    rebuild_schedule(request.app)  # the deleted row's cron job (if any) must stop firing


class CleanupRequest(BaseModel):
    dry_run: bool = False  # preview which collections would be removed (rule 8)


@router.post("/{collection_id}/cleanup")
async def cleanup_collection(collection_id: int, body: CleanupRequest, request: Request) -> dict:
    """Remove this row's collections from Plex, for everyone who has it, without waiting for a run.

    Removal only — it never creates or promotes, so it can never leak: deleting a row can only make
    the server more private. A per-person row's
    collection for each user is pinned by the exact title the last run delivered (recorded in that
    run's breakdown); a shared row is addressed by its own label. dry_run previews the plan.
    """
    state = request.app.state
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(404, "collection not found")
        slug, build, name = collection.slug, collection.build, collection.name

    removed, error = await reconcile.run_reconcile(
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


def _require_collection(session, collection_id: int) -> Collection:
    collection = session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(404, "collection not found")
    return collection


@router.post("/{collection_id}/poster/upload")
async def upload_poster_image(collection_id: int, request: Request, file: Annotated[UploadFile, File()]) -> dict:
    """Store an uploaded poster image for a row and switch it into upload mode.

    Normalizes the image (downscale to poster size + JPEG) before it hits the DB, so a phone photo
    doesn't bloat /config. Any generate-mode text the user typed is preserved.
    """
    raw = await file.read()
    if not raw:
        raise HTTPException(422, "no file was uploaded")
    if len(raw) > poster_service.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "that image is too large — keep it under 8 MB")
    try:
        image, content_type = poster_service.normalize_upload(raw)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    with request.app.state.sessions() as session:
        collection = _require_collection(session, collection_id)
        poster_service.store_upload(session, collection_id, image, content_type)
        cfg = dict(collection.poster or {})
        cfg["mode"] = "upload"
        collection.poster = cfg
        session.add(
            Event(
                scope="collection.poster",
                level="info",
                message={"slug": collection.slug, "mode": "upload", "at": datetime.now(UTC).isoformat()},
            )
        )
        session.commit()
    return {"ok": True, "mode": "upload"}


@router.get("/{collection_id}/poster/image")
async def get_poster_image(collection_id: int, request: Request) -> Response:
    """Serve a row's current poster image: the uploaded original, or a rendered preview.

    For a built-in "text" poster the preview is cheap, so it's rendered on demand if not already
    cached (the thumbnail always shows). For an "ai" poster only a previously-generated image is
    served — a GET never spends money generating one.
    """
    state = request.app.state
    with state.sessions() as session:
        collection = _require_collection(session, collection_id)
        stored = poster_service.load_upload(session, collection_id)
        if stored is not None:
            return Response(stored[0], media_type=stored[1])
        cfg = collection.poster or {}
        mode = (cfg.get("mode") or "").strip()
        if mode in ("text", "ai", "generate"):
            cached = poster_service.load_preview(
                session, mode, cfg.get("title") or "", cfg.get("subtitle") or "", cfg.get("style") or ""
            )
            if cached is not None:
                return Response(cached, media_type="image/png")
            if poster_service.preview_engine(mode) == "text":
                studio = poster_service.make_studio(SettingsStore(session, state.secrets), state.sessions)
                image = await run_in_threadpool(
                    poster_service.preview_poster,
                    studio,
                    mode,
                    cfg.get("title") or "",
                    cfg.get("subtitle") or "",
                    cfg.get("style") or "",
                )
                if image:
                    return Response(image, media_type="image/png")
    raise HTTPException(404, "no poster image for this row")


@router.post("/{collection_id}/poster/preview")
async def preview_poster(collection_id: int, body: PosterIn, request: Request) -> Response:
    """Render a sample poster from the given text and return the image.

    Uses sample placeholder values (a name + the Movies library) so the owner can see what a poster
    will look like. A "text" poster always renders (no provider needed); an "ai" poster needs an
    image-capable provider. The result is cached, so warming the preview also speeds the next run.
    """
    state = request.app.state
    with state.sessions() as session:
        _require_collection(session, collection_id)
        store = SettingsStore(session, state.secrets)
        mode = body.mode or "text"
        if poster_service.preview_engine(mode) == "ai":
            status = poster_service.image_provider_status(store)
            if not status["capable"]:
                raise HTTPException(422, status["reason"])
        studio = poster_service.make_studio(store, state.sessions)
    try:
        image = await run_in_threadpool(
            poster_service.preview_poster, studio, mode, body.title, body.subtitle, body.style
        )
    except Exception as exc:
        raise HTTPException(502, f"couldn't generate a preview ({type(exc).__name__})") from exc
    if not image:
        raise HTTPException(502, "couldn't produce a poster image")
    return Response(image, media_type="image/png")


@router.delete("/{collection_id}/poster/image", status_code=204)
async def delete_poster_image(collection_id: int, request: Request) -> None:
    """Remove a row's uploaded poster image (its config/mode is cleared via the normal save)."""
    with request.app.state.sessions() as session:
        _require_collection(session, collection_id)
        poster_service.clear_assets(session, collection_id)
        session.commit()
