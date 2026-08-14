"""Collections API: define curated rows — how each is built (per-person | shared), who it's for
(audience), and its recipe (size, media, name). Owner-only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from shortlist.engine.candidates import KNOWN_SOURCES
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.delivery import target_sections
from shortlist.engine.models import MAX_REFRESH_DAYS, MAX_ROW_SIZE, MIN_ROW_SIZE, RowSpec, dedupe_slug, slugify
from shortlist.server.api.row_changes import (
    POSTER_RESET,
    PRIVACY_SYNC,
    RECONCILE,
    RENAME,
    PlannedWork,
    RowChange,
    plan_row_changes,
)
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import DEFAULT_SLUG, Collection, CollectionAudience, Event, PickRow, User
from shortlist.server.scheduler import rebuild_schedule
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services import jobs, poster_service, report_service
from shortlist.server.services.poster_service import load_upload
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/collections", tags=["collections"], dependencies=[Depends(require_owner)])

# Slugs reserved by the engine: `shared` prefixes every shared collection's label, and `probe` is
# kept reserved for backward compatibility. A user-defined collection may not claim either.
RESERVED_SLUGS = {"probe", "shared"}

BUILDS = {"per_person", "shared"}
AUDIENCES = {"everyone", "subset"}
MEDIA = {"movie", "show", "both"}
# "off" = neither surface. promote() browse-hides unconditionally, so an "off" row still exists and
# stays reachable from the library's Collections tab — it just claims no Home or Recommended slot.
PLACEMENTS = {"both", "home", "library", "off"}
#: How a row's picks are ordered in the delivered collection. Mirrors `engine.rows.ROW_ORDERS`.
ORDERS = {"best", "rating", "newest", "shuffle", "new_first", "rotate"}
#: What a row does for someone below the watch-history threshold. null -> inherit the global
#: `recommendations.cold_start`. Mirrors `engine.rows.effective_cold_start`.
COLD_STARTS = {"popular", "skip"}
# "" (Plex default), "upload", "text" (built-in Pillow), "ai" (image model). "generate" is the
# pre-text-engine name for "ai", accepted for backward compatibility.
POSTER_MODES = {"", "upload", "text", "ai", "generate"}


def _closed_set(values: set[str], default: str, description: str) -> Field:
    """A string field whose accepted values are ADVERTISED in the OpenAPI schema.

    `_validate` below is what actually rejects a bad value, and it stays the enforcement point — it
    raises one plain-English 422 naming the valid options, which a Pydantic enum error does not.
    But a schema that says only `str` is a schema that lies by omission: the SPA's types are
    generated from it, so every one of these closed sets arrived in TypeScript as a bare `string`
    and the UI had to re-declare the union by hand to get any checking at all.
    Sorted so the emitted schema is stable — `tests/unit/test_openapi_snapshot.py` compares it
    byte-for-byte, and a set's iteration order is not something to hang that on.
    """
    return Field(default=default, description=description, json_schema_extra={"enum": sorted(values)})


def _closed_set_out(values: set[str], description: str) -> Field:
    """`_closed_set` for a RESPONSE field: same advertised set, no default.

    Required rather than defaulted on purpose. A response model with a default can INVENT the key
    when a handler stops sending it — the mirror image of the drop that `extra="allow"` prevents —
    so every field a handler always returns is declared required, and a missing one fails loudly.
    """
    return Field(description=description, json_schema_extra={"enum": sorted(values)})


class HubAnchorIn(BaseModel):
    """A per-library shelf placement for one row: the very TOP (``top``), or after/before either
    another Shortlist ROW (``row``, a row slug) or a foreign collection (``anchor``, a title).
    ``top`` needs neither; otherwise exactly one of ``row``/``anchor`` must be set.

    ``row`` is a slug rather than a title because a per-person row is one Plex collection PER PERSON:
    a title names one account's copy and is meaningless for everyone else, which is what made the
    picker offer forty identical-looking options and place none of them (issue #81)."""

    anchor: str = Field(default="", max_length=255)
    row: str = Field(default="", max_length=255)
    before: bool = False
    top: bool = False


class PosterIn(BaseModel):
    """A row's custom-poster config. ``mode`` "" leaves Plex artwork alone; "upload" uses the image
    stored via the upload endpoint; "text" renders ``title``/``subtitle`` with the built-in Pillow
    engine (no AI); "ai" renders them with the curator provider's image model. The text fields share
    the row-name placeholders ({user}/{library_name}/{top_seed})."""

    mode: str = _closed_set(POSTER_MODES, "", 'Poster source; "" leaves Plex artwork alone.')
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=120)
    style: str = Field(default="", max_length=400)


class CollectionIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    build: str = _closed_set(BUILDS, "per_person", "Who the row is built for: one per person, or one shared row.")
    audience: str = _closed_set(AUDIENCES, "everyone", "Everyone, or the subset named by audience_user_ids.")
    audience_user_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    # This row's own run schedule (5-field cron); "" = never runs on a schedule. New rows default to
    # a nightly 03:30 so they work out of the box; there is no global schedule.
    schedule: str = Field(default="30 3 * * *", max_length=64)
    size: int = Field(default=15, ge=MIN_ROW_SIZE, le=MAX_ROW_SIZE)
    media: str = _closed_set(MEDIA, "both", "Which library types this row builds in.")
    sort_order: int = 0
    name_template: str = ""
    # What to call this row for someone whose name cannot be filled in — a `{top_seed}` row for a
    # person with nothing watched. "" means there is none, and the row is simply not built for them:
    # Shortlist never invents a name (issue #84).
    fallback_name: str = Field(default="", max_length=255)
    min_watchers: int = Field(default=2, ge=2)  # a public row must never be shaped by one person
    request_tag: str = Field(default="", max_length=64)  # tag added to titles requested via this row
    candidate_sources: list[str] = Field(default_factory=list)  # [] -> inherit global candidates.sources
    watched_pct: float | None = Field(default=None, ge=0.0, le=1.0)  # None -> inherit global watched cap
    # Set by a caller that is about to stream the rename itself (the rename page). The PATCH then
    # saves the template but leaves the Plex work alone, instead of renaming everything inline and
    # leaving the stream to report "renamed 0 collections" for a rename that did happen.
    defer_rename: bool = False
    # Lead the row with already-finished titles (a rewatch shelf) rather than merely permitting them.
    rewatch: bool = False
    # Shows only: exclude every series this person has started, not just the ones they finished.
    unstarted_only: bool = False
    refresh_days: int | None = Field(default=None, ge=0, le=MAX_REFRESH_DAYS)  # None -> inherit the global cadence
    # How much this row weights a title's release date. None -> inherit recommendations.recency.
    recency: float | None = Field(default=None, ge=0.0, le=1.0)
    recent_count: int | None = Field(default=None, ge=1, le=25)  # None -> inherit global recent_count
    max_seeds: int | None = Field(default=None, ge=1, le=100)  # None -> inherit the engine default (30)
    # "popular" | "skip" | None -> inherit the global recommendations.cold_start. Enforced in
    # `_validate`, like every other closed set here.
    cold_start: str | None = Field(
        default=None,
        json_schema_extra={"enum": [*sorted(COLD_STARTS), None]},
        description="What this row does for someone with too little watch history; null inherits the global setting.",
    )
    # How many recent watches the row cycles between, one per run. 1 = always the most recent.
    # Capped at 20: past that the "recent" the row's title claims stops being true, and the cycle takes
    # three weeks to come round — indistinguishable from the stuck row this exists to fix.
    seed_window: int = Field(default=1, ge=1, le=20)
    pick_order: str = _closed_set(ORDERS, "best", "How the delivered collection is ordered.")
    library_keys: list[str] = Field(default_factory=list)  # [] -> every library of the row's media type
    placement: str = _closed_set(PLACEMENTS, "both", "Where the OWNER's own collection appears.")
    placement_friends: str = _closed_set(PLACEMENTS, "both", "Where each FRIEND's own collection appears.")
    pin_top: bool = False  # pin to top of the library's Recommended shelf
    # Per-library Recommended-shelf override for this row, keyed by section key. {} -> inherit the
    # global default (settings `rows.hub_anchor`).
    hub_anchor: dict[str, HubAnchorIn] = Field(default_factory=dict)
    poster: PosterIn = Field(default_factory=PosterIn)


class HubAnchorOut(PassthroughModel):
    """A stored shelf placement. Defaulted, unlike the rest of these response fields: rows saved
    before ``top`` existed have only ``anchor``/``before``, and filling in the same default
    `HubAnchorIn` would have written is what those rows already mean."""

    anchor: str = ""
    row: str = ""
    before: bool = False
    top: bool = False


class PosterOut(PassthroughModel):
    """A row's poster config as the editor reads it — never the image bytes."""

    mode: str = _closed_set_out(POSTER_MODES, 'Poster source; "" leaves Plex artwork alone.')
    title: str
    subtitle: str
    style: str
    has_image: bool  # whether the image endpoint has something to serve for this row right now


class CollectionOut(PassthroughModel):
    """A curated-row definition — the response shape of :func:`_serialize`."""

    id: int
    slug: str
    # The DEFAULT row's title is the global template, not its own stale `name` column — see `_serialize`.
    name: str
    last_run_id: int | None  # None until the row has ever built
    build: str = _closed_set_out(BUILDS, "Who the row is built for: one per person, or one shared row.")
    audience: str = _closed_set_out(AUDIENCES, "Everyone, or the subset named by audience_user_ids.")
    audience_user_ids: list[int]
    enabled: bool
    schedule: str
    size: int
    media: str = _closed_set_out(MEDIA, "Which library types this row builds in.")
    sort_order: int
    name_template: str
    fallback_name: str
    min_watchers: int
    request_tag: str
    candidate_sources: list[str]
    watched_pct: float | None
    rewatch: bool
    unstarted_only: bool
    refresh_days: int | None
    recency: float | None
    recent_count: int | None
    max_seeds: int | None
    cold_start: str | None = Field(
        json_schema_extra={"enum": [*sorted(COLD_STARTS), None]},
        description="What this row does for someone with too little watch history; null inherits the global setting.",
    )
    seed_window: int
    pick_order: str = _closed_set_out(ORDERS, "How the delivered collection is ordered.")
    placement: str = _closed_set_out(PLACEMENTS, "Where the OWNER's own collection appears.")
    placement_friends: str = _closed_set_out(PLACEMENTS, "Where each FRIEND's own collection appears.")
    pin_top: bool
    hub_anchor: dict[str, HubAnchorOut]  # keyed by Plex section key, so the KEYS vary by library
    library_keys: list[str]
    poster: PosterOut


class CleanupOut(PassthroughModel):
    """What `POST /collections/{id}/cleanup` removed (or would remove, on a dry run)."""

    removed: list[str]
    dry_run: bool
    message: str


class PosterUploadOut(PassthroughModel):
    ok: bool
    mode: str


def _validate(body: CollectionIn) -> None:
    if body.build not in BUILDS:
        raise HTTPException(status_code=422, detail=f"build must be one of {sorted(BUILDS)}")
    if body.audience not in AUDIENCES:
        raise HTTPException(status_code=422, detail=f"audience must be one of {sorted(AUDIENCES)}")
    if body.media not in MEDIA:
        raise HTTPException(status_code=422, detail=f"media must be one of {sorted(MEDIA)}")
    unknown = [s for s in body.candidate_sources if s not in KNOWN_SOURCES]
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"unknown candidate source(s) {unknown}; valid: {sorted(KNOWN_SOURCES)}"
        )
    if body.pick_order not in ORDERS:
        raise HTTPException(status_code=422, detail=f"pick_order must be one of {sorted(ORDERS)}")
    if body.cold_start is not None and body.cold_start not in COLD_STARTS:
        raise HTTPException(status_code=422, detail=f"cold_start must be null or one of {sorted(COLD_STARTS)}")
    if body.placement not in PLACEMENTS:
        raise HTTPException(status_code=422, detail=f"placement must be one of {sorted(PLACEMENTS)}")
    if body.placement_friends not in PLACEMENTS:
        raise HTTPException(status_code=422, detail=f"placement_friends must be one of {sorted(PLACEMENTS)}")
    if body.poster.mode not in POSTER_MODES:
        raise HTTPException(status_code=422, detail=f"poster mode must be one of {sorted(POSTER_MODES)}")
    if body.schedule.strip():
        try:
            CronTrigger.from_crontab(body.schedule.strip())
        except ValueError as e:
            raise HTTPException(
                status_code=422, detail=f"invalid schedule — needs a 5-field cron (e.g. '30 3 * * *'): {e}"
            ) from e
    for lib, anchor in body.hub_anchor.items():
        if not anchor.top and not anchor.anchor.strip() and not anchor.row.strip():
            raise HTTPException(
                status_code=422, detail=f"hub_anchor[{lib}]: needs 'top', a 'row' slug, or a non-empty 'anchor'"
            )
        if anchor.row.strip() and anchor.anchor.strip():
            # The engine resolves `row` first, so accepting both would silently ignore one of them and
            # leave the editor showing a placement that is not the one in force.
            raise HTTPException(status_code=422, detail=f"hub_anchor[{lib}]: set either 'row' or 'anchor', not both")
    _validate_pairing(rewatch=body.rewatch, unstarted_only=body.unstarted_only, media=body.media)


def _validate_anchor_rows(session: Session, body: CollectionIn, editing_slug: str) -> None:
    """Refuse a row anchor that names a row which doesn't exist, itself, or a cycle.

    Checked HERE and not only in the engine because the engine's only sane response to a cycle is to
    leave those rows where they are — silently, once a night, in a log nobody is reading. The moment
    to say "these two rows point at each other" is while someone is looking at the screen that
    created it.

    ``editing_slug`` is "" when creating: a brand-new row has no slug yet and nothing can point at it,
    so it cannot be part of a cycle — only its own outgoing edges need checking.
    """
    wanted = {lib: a.row.strip() for lib, a in body.hub_anchor.items() if a.row.strip()}
    if not wanted:
        return
    rows = {c.slug: (c.hub_anchor or {}) for c in session.query(Collection).all()}
    for lib, target in wanted.items():
        if target == editing_slug:
            raise HTTPException(status_code=422, detail=f"hub_anchor[{lib}]: a row can't be positioned after itself")
        if target not in rows:
            raise HTTPException(status_code=422, detail=f"hub_anchor[{lib}]: there's no row called '{target}'")
        # Walk the chain this edge would create. Every row's own anchor for THIS library is the next
        # hop; landing back on the row being edited means the chain closes on itself.
        seen, hop = set(), target
        while hop:
            if hop == editing_slug:
                raise HTTPException(
                    status_code=422,
                    detail=f"hub_anchor[{lib}]: that would make a loop — '{target}' already leads back here",
                )
            if hop in seen:
                # A loop further down the chain that this edit did not create. Refusing here would
                # blame the person for someone else's tangle and give them nothing to act on; the
                # engine already declines to place a cycle, and this edit is genuinely fine.
                logger.warning(
                    "row placement: library {} already contains a loop at '{}' — leaving it to the engine "
                    "to skip; the row being saved is not part of it",
                    lib,
                    hop,
                )
                break
            seen.add(hop)
            entry = rows.get(hop, {})
            hop = str(entry.get(lib, {}).get("row") or "").strip() if isinstance(entry.get(lib), dict) else ""


def _forget_anchor_row(session: Session, gone: str) -> list[str]:
    """Drop every placement that positioned a row relative to ``gone``. Returns the slugs changed.

    Called when a row is deleted. Without it those rows keep pointing at a row that no longer exists,
    and the engine — which skips an anchor it cannot resolve, correctly, because a row may simply not
    have delivered into that library yet — would leave them unplaced every night from then on. That
    is the right response to a TRANSIENT miss and the wrong one to a permanent one, and the
    difference is invisible from inside the run. Clearing the reference falls them back to the
    library default, which is where a row with no placement of its own belongs.
    """
    changed: list[str] = []
    for row in session.query(Collection).all():
        anchors = row.hub_anchor or {}
        kept = {
            lib: entry
            for lib, entry in anchors.items()
            if not (isinstance(entry, dict) and str(entry.get("row") or "").strip() == gone)
        }
        if len(kept) != len(anchors):
            row.hub_anchor = kept
            changed.append(row.slug)
    return changed


def _validate_pairing(*, rewatch: bool, unstarted_only: bool, media: str) -> None:
    """Refuse the two combinations of these three fields that a row cannot honour.

    Its own function because a PATCH must check the MERGED row, not the request body — `CollectionIn`
    hands `_validate` its DEFAULTS for anything the request omitted, so a row already set to rewatch
    could be sent `unstarted_only: true` alone, `body.rewatch` would read as its default False, no
    contradiction would be seen, and the invalid pair would land in the database one field at a time.
    Same for `unstarted_only` surviving a narrowing to a movies-only row. This used to be written out
    twice, which is how the two copies could have drifted.
    """
    # Contradictory, and it fails SILENTLY rather than loudly: `unstarted_only` leaves the pool holding
    # only never-opened series, so the rewatch ordering finds nothing finished to lead with and the row
    # fills entirely with new titles — a shelf of unseen shows under a "things you've already seen"
    # title. Refusing is the only outcome that can't mislead.
    if rewatch and unstarted_only:
        raise HTTPException(
            status_code=422,
            detail="a rewatch row can't also exclude everything already started — "
            "they ask for opposite things, so the row would fill with titles nobody has seen",
        )
    # "Shows only" in the field's own docs, and structurally: `_started_shows` yields only show keys, so
    # on a movies row the flag is inert. Storing an inert setting the editor won't even show is how a
    # row ends up behaving unlike what its settings say.
    if unstarted_only and media == "movie":
        raise HTTPException(
            status_code=422,
            detail="unstarted_only applies to shows — a movie is finished the moment it is watched, "
            "so there is no 'started' state to exclude",
        )


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
        # Never ship the DEFAULT row's own column: its title is the global `row.name_template`,
        # already rendered into `name` above. A database written before the API guarded that column
        # still carries a stale value, and the SPA reads `name_template || name` in three places —
        # so the editor would show a title Plex no longer uses, and the rename screen would send it
        # as `old_template`, match nothing (`collection_reconcile.py:527`), report "renamed 0
        # collections", and leave the next run to build a second collection beside the old one.
        # Neutralising it here rather than in a migration keeps one place responsible for the rule.
        "name_template": "" if collection.slug == DEFAULT_SLUG else collection.name_template,
        "fallback_name": collection.fallback_name or "",
        "min_watchers": collection.min_watchers,
        "request_tag": collection.request_tag or "",
        "candidate_sources": list(collection.candidate_sources or []),
        "watched_pct": collection.watched_pct,
        "rewatch": bool(collection.rewatch),
        "unstarted_only": bool(collection.unstarted_only),
        "refresh_days": collection.refresh_days,
        "recency": collection.recency,
        "recent_count": collection.recent_count,
        "max_seeds": collection.max_seeds,
        "cold_start": collection.cold_start,
        "seed_window": int(collection.seed_window or 1),
        "pick_order": collection.pick_order or "best",
        "placement": collection.placement or "both",
        "placement_friends": collection.placement_friends or "both",
        "pin_top": bool(collection.pin_top),
        "hub_anchor": collection.hub_anchor or {},
        "library_keys": [str(k) for k in (collection.library_keys or [])],
        "poster": _poster_view(session, collection),
    }


def _reject_duplicate_name(
    session, secrets, template: str, *, exclude_slug: str = "", build: str = "", fallback_name: str = ""
) -> None:
    """Refuse a row title another row is already titled from — see `reconcile.row_titled_from` for
    what "already titled from" means and why the `name` column is the wrong thing to compare.

    ``template`` is the EFFECTIVE template being proposed (`name_template or name`), not the raw name:
    a row that carries its own template is titled from that, so changing only its `name` cannot clash
    with anything, and changing only its `name_template` very much can.
    """
    clash = reconcile.row_titled_from(
        session, template, secrets=secrets, exclude_slug=exclude_slug, build=build, fallback_name=fallback_name
    )
    if clash is None:
        return
    # Name the row by SLUG as well. The clashing row's `name` is usually the very string being
    # rejected, so the message read "'Friday Films' is already the title of the row 'Friday Films'" —
    # a tautology that named nothing the owner could go and find.
    whose = (
        "your default row, whose title is the template in Settings → Defaults"
        if clash.slug == DEFAULT_SLUG
        else f"the row {clash.name!r} ({clash.slug})"
    )
    raise HTTPException(
        status_code=422,
        detail=f"{template!r} is already the title of {whose} — two rows with the same title become a "
        "single collection on Plex, so pick a different name",
    )


def _unique_slug(session, base: str) -> str:
    base = base if base not in RESERVED_SLUGS else f"{base}_row"
    return dedupe_slug(base, lambda slug: session.query(Collection).filter_by(slug=slug).first() is not None)


def _set_audience(session, collection: Collection, body: CollectionIn) -> None:
    """Replace this row's audience with the requested user ids, refusing any that don't exist.

    The ids are RESOLVED first, deliberately. `CollectionAudience.user_id` is a foreign key and the
    connection runs with `PRAGMA foreign_keys=ON`, so an unknown id used to surface as an
    `IntegrityError` at commit — an unhandled 500 carrying a SQL string, where every other bad input
    on this router is a 422. And on a SHARED row this list is what decides who is excluded from the
    share filter, so silently dropping an id nobody recognises is the wrong direction to fail in.
    """
    session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
    if body.audience != "subset":
        return
    wanted = list(dict.fromkeys(body.audience_user_ids))  # dedupe, keep order
    if not wanted:
        return
    known = {user_id for (user_id,) in session.query(User.id).filter(User.id.in_(wanted)).all()}
    unknown = [user_id for user_id in wanted if user_id not in known]
    if unknown:
        raise HTTPException(status_code=422, detail=f"no such user(s): {unknown} — the audience must be existing users")
    for user_id in wanted:
        session.add(CollectionAudience(collection_id=collection.id, user_id=user_id))


@router.get("", response_model=list[CollectionOut])
async def list_collections(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        collections = session.query(Collection).order_by(Collection.sort_order, Collection.id).all()
        return [_serialize(session, c) for c in collections]


@router.post("", status_code=201, response_model=CollectionOut)
async def create_collection(body: CollectionIn, request: Request) -> dict:
    _validate(body)
    with request.app.state.sessions() as session:
        # The template this row will actually be titled from, not the bare name — a POST may set both.
        _reject_duplicate_name(
            session,
            request.app.state.secrets,
            body.name_template or body.name,
            build=body.build,
            fallback_name=body.fallback_name,
        )
        _validate_anchor_rows(session, body, editing_slug="")
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
            fallback_name=body.fallback_name or None,
            min_watchers=body.min_watchers,
            request_tag=body.request_tag.strip(),
            candidate_sources=body.candidate_sources,
            watched_pct=body.watched_pct,
            rewatch=body.rewatch,
            unstarted_only=body.unstarted_only,
            refresh_days=body.refresh_days,
            recency=body.recency,
            recent_count=body.recent_count,
            max_seeds=body.max_seeds,
            cold_start=body.cold_start,
            seed_window=body.seed_window,
            pick_order=body.pick_order,
            placement=body.placement,
            placement_friends=body.placement_friends,
            pin_top=body.pin_top,
            hub_anchor={k: v.model_dump() for k, v in body.hub_anchor.items()},
            library_keys=body.library_keys,
            poster=body.poster.model_dump(),
        )
        session.add(collection)
        session.flush()
        _set_audience(session, collection, body)
        session.commit()
        result = _serialize(session, collection)
    rebuild_schedule(request.app)  # a new row may carry a schedule — register its cron job now
    return result


# Columns a PATCH may set directly, name (needs a dup check) and audience (needs shaping)
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
    "fallback_name",
    "min_watchers",
    "request_tag",
    "candidate_sources",
    "watched_pct",
    "rewatch",
    "unstarted_only",
    "refresh_days",
    "recency",
    "recent_count",
    "max_seeds",
    "cold_start",
    "seed_window",
    "pick_order",
    "placement",
    "placement_friends",
    "pin_top",
    "library_keys",
)


def _stranded_sections(state, *, old_media: str, old_keys: list[str], new_media: str, new_keys: list[str]) -> set[str]:
    """Section keys this row USED to deliver into and no longer does.

    Narrowing a row is not the same as removing it: the collections in the libraries it still targets
    are live. So the set is a difference, computed with the engine's own `target_sections` so it can
    never drift from where delivery actually writes.

    Empty when the row widened, when nothing moved, or when Plex cannot be reached — the last of those
    deliberately: not knowing which libraries exist must mean "delete nothing", never "delete
    everything". The next edit or a sync check picks it up.
    """
    if (old_media, sorted(old_keys)) == (new_media, sorted(new_keys)):
        return set()
    try:
        sections = state.run_service.build_context(dry_run=True).plex.sections()
    except Exception as e:
        logger.warning("could not read libraries to narrow row scope ({}) — nothing removed", type(e).__name__)
        return set()

    def targeted(media: str, keys: list[str]) -> set[str]:
        spec = RowSpec(slug="", name_template="", size=0, media=media, library_keys=list(keys))
        return {str(s.key) for s in target_sections(sections, spec)}

    return targeted(old_media, old_keys) - targeted(new_media, new_keys)


def _queue_reconcile(
    state,
    *,
    slug: str,
    build: str,
    scope: str,
    only_user_ids: list[int] | None = None,
    template: str | None = None,
    in_sections: list[str] | None = None,
) -> None:
    """Queue the removal of a row's Plex collections as a durable job.

    Every one of these used to be a bare ``run_in_executor``: no retry, no record, and no check that a
    run was not writing to the same server at that moment. A Plex outage at the instant of the edit lost
    the work permanently — and nothing revisits a deleted or switched-off row, so those collections
    stayed on the server for ever. As a job it retries with backoff, survives a container restart, waits
    for a run to finish, and shows up on the Jobs page whether it succeeds or gives up.

    The caller drains afterwards, so in the normal case it still happens immediately.
    """
    payload: dict = {"slug": slug, "build": build, "scope": scope}
    if only_user_ids is not None:
        payload["only_user_ids"] = only_user_ids
    if template is not None:
        payload["template"] = template  # the DELETE path: no row left to read it from on a retry
    if in_sections is not None:
        payload["in_sections"] = in_sections  # a NARROWED row: only the libraries it walked away from
    jobs.enqueue(state.sessions, "row.reconcile", payload)


@router.patch("/{collection_id}", response_model=CollectionOut)
async def update_collection(collection_id: int, body: CollectionIn, request: Request) -> dict:
    """Edit a row: validate → apply → plan the Plex work → enqueue it → drain.

    The decision table for "what does this edit owe Plex" lives in `api/row_changes.py`, not here.
    It used to be eleven mutable flags accumulated down this handler and eight conditional
    dispatches at the bottom — untestable without a Plex context, and the place a missed branch
    silently left someone's row on the wrong Home screen.
    """
    _validate(body)
    # Only touch fields the request actually sent, so a partial PATCH (e.g. an enable toggle) never
    # resets the columns it omitted back to CollectionIn's defaults.
    sent = body.model_fields_set
    state = request.app.state
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="collection not found")
        _validate_anchor_rows(session, body, editing_slug=collection.slug)
        before = _snapshot(session, collection)
        is_default = collection.slug == DEFAULT_SLUG
        # A rename only matters for a NON-default per-person row (the default row's title follows the
        # global Settings template, not this column). The old effective template is what the
        # collections on Plex are titled with right now — delivery renders from `name_template or name`.
        touching_name = before["build"] == "per_person" and not is_default and bool(sent & {"name", "name_template"})
        template_before = (collection.name_template or collection.name) if touching_name else ""
        template_after = template_before
        # The clash check runs on the MERGED effective template, for the same reason `_validate_pairing`
        # does: a PATCH may send either half. Sending `name_template` ALONE changes the title and used
        # to be checked by nothing at all, while sending `name` alone on a row that carries its own
        # template changes no title and was checked as though it did.
        if not is_default and sent & {"name", "name_template"}:
            merged = (body.name_template if "name_template" in sent else collection.name_template) or (
                body.name if "name" in sent else collection.name
            )
            # Only when the TITLE actually moves. The editor re-sends `name` on every save, so
            # checking on "was the field present" refused a size-only edit on a row that already
            # clashes — with a message about names, for a change that was not about names. A row in
            # that state (created before this guard, or restored from a backup) must stay editable;
            # the rule is "no NEW clashes", not "no clashing row may be touched".
            if reconcile.title_key(merged) != reconcile.title_key(collection.name_template or collection.name):
                _reject_duplicate_name(
                    session, state.secrets, merged, exclude_slug=collection.slug, build=before["build"]
                )
        # The default row has no per-collection name: its title IS the global `row.name_template`
        # (Settings → Defaults), which delivery renders per library. So a rename of it writes that
        # global setting — NOT this column — because a per-collection template would win over each
        # user's own `row_name_tpl` override in `resolve_row_template`. Its `name` column is never
        # touched (the editor round-trips the template as the name, which must not clobber it).
        if "name" in sent and is_default:
            store = SettingsStore(session, state.secrets)
            new_template = body.name.strip()
            previous = store.get("row.name_template") or ""
            if new_template and new_template != previous:
                # Renaming the default row retitles it on Plex just as surely as renaming any other,
                # so it owes the same clash check — onto the title EVERY other row already renders.
                _reject_duplicate_name(
                    session, state.secrets, new_template, exclude_slug=DEFAULT_SLUG, build="per_person"
                )
                store.set("row.name_template", new_template)
                template_before, template_after = previous, new_template
        elif "name" in sent:
            collection.name = body.name
        # Checked against the MERGED row, never the request body — see `_validate_pairing`.
        _validate_pairing(
            rewatch=body.rewatch if "rewatch" in sent else bool(collection.rewatch),
            unstarted_only=body.unstarted_only if "unstarted_only" in sent else bool(collection.unstarted_only),
            media=body.media if "media" in sent else collection.media,
        )
        for column in _PATCHABLE_COLUMNS:
            if column in sent:
                # The DEFAULT row must never carry its own `name_template`: its title IS the global
                # `row.name_template`, written just above. The engine already knows this and forces
                # the field empty for this row when it builds specs (`context_builder.py:604,698`),
                # so a stored value never reaches delivery — but `report_service.py` PREFERS it over
                # the global, so a row that has one shows a stale name in reports the moment
                # Settings → Defaults changes. The rename screen sends `name` and `name_template`
                # together, which is right for every other row, so the guard belongs here rather
                # than in one caller: any client sending the field would otherwise reintroduce it.
                if column == "name_template" and is_default:
                    continue
                setattr(collection, column, getattr(body, column))
        if "schedule" in sent:
            collection.schedule = body.schedule.strip()  # a whitespace-only cron means "no schedule"
        if "poster" in sent:
            collection.poster = body.poster.model_dump()
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
        after = _snapshot(session, collection)
        if touching_name:
            template_after = collection.name_template or collection.name
        result = _serialize(session, collection)
    # A schedule or enable/disable change alters which cron jobs should exist — re-derive them.
    if sent & {"schedule", "enabled"}:
        rebuild_schedule(request.app)

    change = RowChange(
        slug=before["slug"],
        build_before=before["build"],
        build_after=after["build"],
        enabled_before=before["enabled"],
        enabled_after=after["enabled"],
        media_before=before["media"],
        media_after=after["media"],
        libraries_before=before["libraries"],
        libraries_after=after["libraries"],
        audience_before=before["audience"],
        audience_after=after["audience"],
        template_before=template_before,
        template_after=template_after,
        poster_mode_before=before["poster_mode"],
        poster_mode_after=after["poster_mode"],
        defer_rename=body.defer_rename,
    )

    # Narrowing a row is not the same as removing it, and answering "which libraries did it leave"
    # costs a Plex read — so it is passed as a callable and only paid for when the plan needs it.
    def stranded() -> set[str]:
        return _stranded_sections(
            state,
            old_media=change.media_before,
            old_keys=list(change.libraries_before),
            new_media=change.media_after,
            new_keys=list(change.libraries_after),
        )

    await _apply_plan(state, plan_row_changes(change, stranded), slug=change.slug, build=change.build_before)
    return result


def _snapshot(session, collection: Collection) -> dict:
    """The fields a row edit is judged on, read off the row as it stands right now.

    Taken once before the patch and once after, so `plan_row_changes` compares two like-for-like
    pictures instead of the handler tracking eleven "did this change?" flags down its own body.
    """
    if collection.audience == "everyone":
        # "everyone" is not a stable set — it resolves to whoever is on the roster at this moment,
        # which is what makes a row switched from a subset to everyone drop nobody.
        audience = frozenset(user_id for (user_id,) in session.query(User.id).all())
    else:
        audience = frozenset(
            a.user_id for a in session.query(CollectionAudience).filter_by(collection_id=collection.id)
        )
    return {
        "slug": collection.slug,
        "build": collection.build,
        "enabled": bool(collection.enabled),
        # Narrowing either of these — "both" media down to movies only, or dropping a library from the
        # list — leaves the collections in the libraries it walked away from with nothing to ever
        # revisit them: delivery no longer targets those libraries, so they are never refreshed, never
        # removed, and re-promoted every run by promotion's no-spec fallback.
        "media": collection.media,
        "libraries": tuple(str(k) for k in (collection.library_keys or [])),
        "audience": audience,
        "poster_mode": (collection.poster or {}).get("mode") or "",
    }


async def _apply_plan(state, plan: list[PlannedWork], *, slug: str, build: str) -> None:
    """Carry out a planned edit, in order, then drain whatever is still queued.

    The order matters and is the planner's, not this function's: a `privacy.sync` drains the queue as
    it goes, so every removal planned before it has actually happened by the time each account's
    excludes are recomputed (plex-safety rule 1).

    `build` is the row's build BEFORE the edit — the collections being removed are the old build's.
    """
    for work in plan:
        if work.kind == RECONCILE:
            _queue_reconcile(
                state,
                slug=slug,
                build=build,
                scope=work.scope,
                only_user_ids=work.only_user_ids,
                in_sections=work.in_sections,
            )
        elif work.kind == PRIVACY_SYNC:
            await jobs.queue_privacy_sync(state, work.scope)
        elif work.kind == RENAME:
            # Re-renders per user and skips anyone whose title didn't actually change — so a user with
            # their own `row_name_tpl` override is left alone. Best-effort + audited.
            await reconcile.run_row_rename_from_plex(
                state,
                slug=slug,
                new_template=work.new_template,
                old_template=work.old_template,
                scope=work.scope,
            )
        elif work.kind == POSTER_RESET:
            await reconcile.run_poster_reset(state, slug=slug, build=build, scope=work.scope)
    # Anything queued above happens NOW when Plex is reachable; when it isn't, the worker retries it.
    # AWAITED, unlike the delete below. An edit's Plex work IS the request: narrowing a row's
    # libraries means "take it off those libraries", so returning 200 before that happened would
    # report a change that has not been made. A delete is different — the row is gone from the DB,
    # which is what the page is waiting to hear, and the cleanup is bookkeeping after the fact.
    await jobs.drain_now(state, f"row '{slug}' was edited")


@router.delete("/{collection_id}", status_code=204)
async def delete_collection(collection_id: int, request: Request) -> None:
    state = request.app.state
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="collection not found")
        # The default row is deletable like any other. It used to 422 here, on the reasoning that
        # there must "always be a home for users with no other row" — but rows are user-created now,
        # `EngineConfig.rows_defined` means an empty list is "everything is off" rather than
        # "resurrect the default", and an un-deletable item with no visible reason is its own bug
        # report. Disabling it is still the reversible option; this is the permanent one.
        slug, build = collection.slug, collection.build
        # Captured while the row still exists and carried in the job payload: after the DB row is gone
        # there is nothing left to resolve the title its collections were built under, so a retry
        # (Plex down at this moment, container killed mid-write) would have nothing to address.
        template = reconcile.row_template(session, slug, state.secrets)

    # Queue the Plex removal FIRST, then drop the DB row. Draining after the delete is deliberate:
    # `is_default` aside, the removal no longer needs the row to exist, and queueing means a Plex outage
    # right now leaves a retried job rather than orphaned collections nothing will ever revisit.
    _queue_reconcile(state, slug=slug, build=build, scope="collection.delete", template=template)
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is not None:
            session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
            session.delete(collection)
            orphaned = _forget_anchor_row(session, slug)
            if orphaned:
                logger.info(
                    "row '{}' was deleted — {} row(s) positioned relative to it now follow the library "
                    "default instead: {}",
                    slug,
                    len(orphaned),
                    ", ".join(orphaned),
                )
            session.commit()
    if build == "shared":
        # The row's own `shortlist__shared_<slug>` label is no longer declared shared by the config, so
        # every account's excludes need recomputing — otherwise the label lingers in all of them.
        jobs.enqueue(state.sessions, "privacy.sync", {"reason": f"row '{slug}' was deleted"})
    rebuild_schedule(request.app)  # the deleted row's cron job (if any) must stop firing
    # Drained in the BACKGROUND, not awaited. The row is gone from the DB the moment this returns,
    # which is what the page is waiting to hear — while the Plex side is a per-user walk over every
    # library, and for a shared row a privacy pass across every account on the server. Awaiting that
    # held the request open for the length of both and left the UI sitting on a spinner.
    #
    # Nothing is lost by not waiting: the jobs are committed rows, the worker retries them with
    # backoff, and they are visible in the header's activity popover and on the Jobs page while they
    # run. A restart mid-drain re-queues them.
    jobs.drain_in_background(state, f"row '{slug}' was deleted")


class RenameRequest(BaseModel):
    name_template: str = ""
    # The title this row rendered as BEFORE the rename. It is the only thing that tells this row's
    # collection apart from the person's other rows (they all share one label), so without it the
    # reconcile skips the user rather than renaming whatever it finds.
    old_template: str = ""
    #: Preview only — report what would be renamed and write nothing (plex-safety rule 8).
    dry_run: bool = False


@router.post("/{collection_id}/rename")
async def rename_collection_stream(collection_id: int, body: RenameRequest, request: Request) -> StreamingResponse:
    """Rename this row's collections on Plex, streaming SSE events as each user's collection is
    renamed. Returns a text/event-stream: one 'rename' event per user, then a 'done' event."""
    import json
    from queue import Queue

    with request.app.state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="row not found")
        slug = collection.slug
        build = collection.build
        # If a new template is provided, save it now (standalone use without the dialog PATCH).
        # If called from the dialog flow, the PATCH already saved it — this is idempotent.
        new_template = body.name_template.strip()
        if new_template:
            # The SIXTH door onto the title collision, and the only one that goes on to retitle the
            # collections on Plex itself (`reconcile_row_rename_iter`, below). The SPA reaches it
            # through the rename page, which PATCHes first and so is already guarded — but this
            # endpoint documents standalone use one line up, and an API client taking that route
            # could hand two rows one title, or overwrite the global template, with nothing to stop
            # it. Checked BEFORE either write, so a refusal renames nothing here or on Plex.
            _reject_duplicate_name(session, request.app.state.secrets, new_template, exclude_slug=slug, build=build)
            # Same rule as the PATCH handler: the DEFAULT row's title IS the global setting, and its
            # own column must stay empty. Writing it here would undo that guard within the same
            # flow — the rename screen PATCHes and then immediately POSTs to this endpoint, so a
            # column cleared one request ago came straight back.
            if slug == DEFAULT_SLUG:
                SettingsStore(session).set("row.name_template", new_template)
            else:
                collection.name_template = new_template
            session.commit()
        else:
            # No template in the body — read the current one from the DB (already saved by PATCH).
            new_template = collection.name_template or collection.name
            if slug == DEFAULT_SLUG:
                new_template = SettingsStore(session).get("row.name_template") or new_template

    old_template = body.old_template.strip() or None
    state = request.app.state
    q: Queue = Queue()

    def _run():
        try:
            for event in reconcile.reconcile_row_rename_iter(
                state,
                slug=slug,
                new_template=new_template,
                old_template=old_template,
                # A shared row is ONE collection under a different label; walking the per-user labels
                # found nothing and reported success.
                build=build,
                dry_run=body.dry_run,
            ):
                q.put(event)
        except Exception as e:
            # Redacted: this catches anything the generator raises BEFORE its own per-collection
            # handler — a `plex.sections()` failure carrying a tokened URL — and it goes straight to
            # the browser (rule 9).
            q.put({"error": redact(f"{type(e).__name__}: {e}")})
        finally:
            q.put(None)  # sentinel

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run)

    async def generate():
        while True:
            # `Queue.get` is a BLOCKING stdlib call. Awaiting it here used to be a plain call with a
            # 0.1s timeout, which froze the event loop for that long on every empty tick — and a
            # rename walks every user over plex.tv, so the loop (SSE, other requests, the Docker
            # HEALTHCHECK) was unavailable roughly two thirds of the time. The wait belongs on a
            # worker thread. `_run`'s `finally` always puts the sentinel, so this can't hang.
            event = await loop.run_in_executor(None, q.get)
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class CleanupRequest(BaseModel):
    dry_run: bool = False  # preview which collections would be removed (rule 8)


@router.post("/{collection_id}/cleanup", response_model=CleanupOut)
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
            raise HTTPException(status_code=404, detail="collection not found")
        slug, build, name = collection.slug, collection.build, collection.name

    removed, error = await reconcile.run_reconcile(
        state, slug=slug, build=build, dry_run=body.dry_run, scope="collection.cleanup"
    )
    if error:
        raise HTTPException(status_code=502, detail=f"Cleanup failed part-way; removed {len(removed)} before: {error}")
    verb = "Would remove" if body.dry_run else "Removed"
    return {
        "removed": removed,
        "dry_run": body.dry_run,
        "message": f"{verb} {len(removed)} collection(s) for “{name}”.",
    }


def _require_collection(session, collection_id: int) -> Collection:
    collection = session.get(Collection, collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="collection not found")
    return collection


@router.post("/{collection_id}/poster/upload", response_model=PosterUploadOut)
async def upload_poster_image(collection_id: int, request: Request, file: Annotated[UploadFile, File()]) -> dict:
    """Store an uploaded poster image for a row and switch it into upload mode.

    Normalizes the image (downscale to poster size + JPEG) before it hits the DB, so a phone photo
    doesn't bloat /config. Any generate-mode text the user typed is preserved.
    """
    # Refuse on the declared length BEFORE reading the body: `await file.read()` buffers the whole
    # upload (Starlette spills past ~1 MB to a temp file), so checking the size afterwards means a
    # 500 MB post is fully received and written to disk only to be rejected. A missing or lying
    # Content-Length still hits the real check below — this is a cheap early out, not the guard.
    # The 4 KB allowance is the multipart envelope (boundaries + part headers), which Content-Length
    # counts and the image bytes do not — without it a file a few bytes under the cap would be
    # refused here by a check that is only meant to catch the obviously-too-big.
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > poster_service.MAX_UPLOAD_BYTES + 4096:
        raise HTTPException(status_code=413, detail="that image is too large — keep it under 8 MB")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="no file was uploaded")
    if len(raw) > poster_service.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="that image is too large — keep it under 8 MB")
    try:
        image, content_type = poster_service.normalize_upload(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    raise HTTPException(status_code=404, detail="no poster image for this row")


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
                raise HTTPException(status_code=422, detail=status["reason"])
        studio = poster_service.make_studio(store, state.sessions)
    try:
        image = await run_in_threadpool(
            poster_service.preview_poster, studio, mode, body.title, body.subtitle, body.style
        )
    except Exception as exc:
        # Type only in both the log and the response — an image-provider error can carry the API key
        # (Google embeds it in the URL). Without this line the operator sees only a browser 502.
        logger.warning("poster preview failed (mode {!r}, {})", mode, type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"couldn't generate a preview ({type(exc).__name__})") from exc
    if not image:
        raise HTTPException(status_code=502, detail="couldn't produce a poster image")
    return Response(image, media_type="image/png")


@router.delete("/{collection_id}/poster/image", status_code=204)
async def delete_poster_image(collection_id: int, request: Request) -> None:
    """Remove a row's uploaded poster image, and put the artwork on Plex back to default.

    Clearing the stored bytes used to be all this did, leaving `mode` as "upload" with nothing to
    upload — so the row kept the artwork Shortlist had already pushed to Plex, for ever, with no way
    to reach it: the PATCH path only reverts when a row that HAD a mode drops to none, and the mode
    never dropped. "Delete the image" now means the image is gone from both places.
    """
    state = request.app.state
    with state.sessions() as session:
        collection = _require_collection(session, collection_id)
        slug, build = collection.slug, collection.build
        had_custom = bool((collection.poster or {}).get("mode"))
        poster_service.clear_assets(session, collection_id)
        collection.poster = {**(collection.poster or {}), "mode": ""}
        session.commit()
    if had_custom:
        # Cosmetic and privacy-neutral (the hiding label and promotion are untouched), so gate-exempt.
        # Best-effort + audited, exactly like the same revert from the row editor.
        await reconcile.run_poster_reset(state, slug=slug, build=build, scope="collection.poster")


class RowLibraryEffectiveness(PassthroughModel):
    """One library's share of a row's matured cohort. A row that builds in two libraries is two Plex
    collections and genuinely performs differently in each, so they are never merged into one line."""

    library: str
    delivered: int
    watched: int
    rate: float | None  # None when nothing was delivered there


class RowMaturedCohort(PassthroughModel):
    """The picks old enough to be judged: delivered at least `matured_days` ago, so every one of them
    has had its full window to be watched."""

    delivered: int
    watched: int
    rate: float | None
    cohort_to: str


class RowEffectivenessOut(PassthroughModel):
    """Whether one row is working, for the panel beside its settings."""

    delivered: int  # all time, distinct person+title
    watched: int
    # Runs still on record that built this row — counted exactly as `/api/runs?collection=<slug>`
    # selects them, since the panel's Runs tile links there. Pruned runs are in neither.
    runs: int
    first_delivered_at: str | None  # None = this row has never delivered anything
    last_delivered_at: str | None  # None = same; otherwise the most recent delivery
    matured_days: int
    matured: RowMaturedCohort | None  # None = nothing is old enough to judge yet
    per_library: list[RowLibraryEffectiveness]


@router.get("/{collection_id}/effectiveness", response_model=RowEffectivenessOut)
async def collection_effectiveness(collection_id: int, request: Request) -> dict:
    """How this row has actually performed — delivered, watched, and the landing rate.

    Its own endpoint rather than a slice of `/api/report/effectiveness`, which is ~30 queries and
    runs in a worker thread for that reason; this is four, so opening the row editor costs nothing
    like opening the dashboard. Both read the same columns through `report_service`, so they cannot
    drift apart on what counts as a hit.
    """
    with request.app.state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="collection not found")
        # Keyed on the SLUG, which is what picks are stamped with — a row renamed or re-created keeps
        # its slug, and that is the identity its history hangs off.
        return report_service.row_effectiveness(session, collection.slug)
