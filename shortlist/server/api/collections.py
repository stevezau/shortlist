"""Collections API: define curated rows — how each is built (per-person | shared), who it's for
(audience), and its recipe (size, media, name). Owner-only."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from loguru import logger
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse, StreamingResponse

import shortlist.server.services.context_builder as context_builder
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.models import (
    LANGUAGE_MODES,
    SONARR_MONITOR_MODES,
)
from shortlist.engine.placeholders import fill_theme, uses_season, uses_theme
from shortlist.engine.web_guidance import INSTRUCTION_MODES
from shortlist.server.assistant.row_effects import (
    privacy_sync_step,
    queue_convergence_in_session,
    reconcile_step,
    schedule_rebuild_step,
)
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    Event,
    Theme,
    ThemeHistory,
    User,
    iso_utc,
)
from shortlist.server.safe_mode import force_dry_run
from shortlist.server.schema_base import PassthroughModel, StrictRequestModel
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services import jobs, poster_service, report_service
from shortlist.server.services.audit import add_audit, write_audit
from shortlist.server.services.row_changes import (
    POSTER_RESET,
    PRIVACY_SYNC,
    RECONCILE,
    RENAME,
    VISIBILITY,
    PlannedWork,
    RowChange,
    plan_row_changes,
)
from shortlist.server.services.row_editing import (
    AUDIENCES,
    BUILDS,
    COLD_STARTS,
    EXPLORE_COLUMNS,
    MEDIA,
    ORDERS,
    PLACEMENTS,
    POSTER_MODES,
    TITLE_MOVING_FIELDS,
    CollectionIn,
    PosterIn,
    closed_set_out,
    known_seasons,
    merged_template,
    preview_titles,
    projected_snapshot,
    reject_duplicate_name,
    reject_new_person_title_clash,
    reject_season_name_without_seasons,
    row_change,
    row_snapshot,
    rows_anchored_to,
    serialize_row,
    stranded_sections,
    validate_anchor_rows,
    validate_audience_ids,
    validate_explore,
    validate_pairing,
    validate_requests_row,
    validate_row,
    validate_theme,
)
from shortlist.server.services.row_mutations import (
    apply_prevalidated_row_update_in_session,
    create_row_in_session,
    delete_row_in_session,
)
from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.services.theme_store import spec_from_row
from shortlist.server.settings_store import SettingsStore

router = APIRouter(prefix="/collections", tags=["collections"], dependencies=[Depends(require_owner)])


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

    mode: str = closed_set_out(POSTER_MODES, 'Poster source; "" leaves Plex artwork alone.')
    title: str
    subtitle: str
    style: str
    has_image: bool  # whether the image endpoint has something to serve for this row right now


class PlanEntryOut(PassthroughModel):
    """One unit of Plex work an edit would cause."""

    kind: str = closed_set_out({RECONCILE, PRIVACY_SYNC, RENAME, POSTER_RESET, VISIBILITY}, "What this step would do.")
    #: The audit scope the real edit would use — why this step is owed.
    reason: str
    #: The collection titles a RECONCILE would remove from Plex. Empty for every other kind, and
    #: empty on a RECONCILE whose Plex walk failed (`preview_incomplete` then says so).
    collections: list[str]
    #: WHOSE copies a RECONCILE would remove; empty means everyone who has the row. Reported rather
    #: than left implicit because it is what an audience shrink turns on, and a preview that names
    #: the right KIND of work against the wrong people is the shape of bug this whole item exists to
    #: prevent.
    only_user_ids: list[int]
    #: WHICH libraries a RECONCILE is limited to (Plex section keys); empty means every library.
    in_sections: list[str]


class SeasonWindowOut(PassthroughModel):
    """One season's run for a row: which season, and the first and last days the row shows it."""

    slug: str
    name: str
    emoji: str
    starts: str  # ISO date, on the server's clock
    ends: str


class SeasonStatusOut(PassthroughModel):
    """Where a seasonal row is in its calendar today, judged on the SERVER's clock."""

    #: The season it shows today, or null between seasons (the row is hidden).
    showing: SeasonWindowOut | None
    #: The next season to start after today, or null when it follows none.
    next: SeasonWindowOut | None


class PreviewTitleOut(PassthroughModel):
    """One title from a row's latest delivery, for the Rows list's poster collage."""

    #: The Plex ratingKey, which `/api/picks/{rating_key}/poster` serves the artwork for.
    rating_key: int
    title: str


class AiInstructionsOut(PassthroughModel):
    """A row's AI web search instructions as the editor reads them."""

    mode: str = closed_set_out(set(INSTRUCTION_MODES), "default, add or own")
    text: str


class CollectionOut(PassthroughModel):
    """A curated-row definition — the response shape of :func:`serialize_row` (``services/row_editing.py``)."""

    id: int
    slug: str
    # The DEFAULT row's title is the global template, not its own stale `name` column — see `serialize_row`.
    name: str
    last_run_id: int | None  # None until the row has ever built
    #: Up to four titles from the row's most recent delivery, best ranked first. Empty until it has built.
    preview_titles: list[PreviewTitleOut]
    build: str = closed_set_out(BUILDS, "Who the row is built for: one per person, or one shared row.")
    audience: str = closed_set_out(AUDIENCES, "Everyone, or the subset named by audience_user_ids.")
    audience_user_ids: list[int]
    enabled: bool
    schedule: str
    size: int
    media: str = closed_set_out(MEDIA, "Which library types this row builds in.")
    sort_order: int
    name_template: str
    fallback_name: str
    description: str
    sort_title_prefix: str
    min_watchers: int
    request_tag: str
    candidate_sources: list[str]
    watched_pct: float | None
    rewatch: bool
    rewatch_cooldown_days: int
    requests_row: bool
    requests_window_days: int
    requests_tag_pattern: str
    unstarted_only: bool
    refresh_days: int | None
    idle_hold_days: int | None
    recency: float | None
    recent_count: int | None
    favourite_count: int | None
    older_count: int | None
    max_seeds: int | None
    max_runtime: int | None
    min_year: int | None
    max_year: int | None
    min_rating: float | None
    cold_start: str | None = Field(
        json_schema_extra={"enum": [*sorted(COLD_STARTS), None]},
        description="What this row does for someone with too little watch history; null inherits the global setting.",
    )
    seed_window: int
    req_min_rating: float | None
    req_min_votes: int | None
    req_min_demand: int | None
    req_min_year: int | None
    req_max_year: int | None
    req_auto_send: bool | None
    req_auto_min_demand: int | None
    req_auto_min_rating: float | None
    req_max_per_row: int | None
    req_radarr_quality_profile_id: int | None
    req_radarr_root_folder: str | None
    req_sonarr_quality_profile_id: int | None
    req_sonarr_root_folder: str | None
    # Required, not defaulted (see `closed_set_out`): every one of these comes from `serialize_row`,
    # and a default would let a handler that stopped sending it INVENT the key instead of failing.
    req_sonarr_monitor: str | None = Field(
        description=(
            "How much of a show Sonarr monitors for this row's requests "
            "(Sonarr's Add Series 'Monitor' choice); null inherits the global requests.sonarr.monitor."
        ),
        json_schema_extra={"enum": [*SONARR_MONITOR_MODES, None]},
    )
    req_language_mode: str | None = Field(
        description=(
            "How this row treats a title's original language when requesting: 'any' (one bar for "
            "everything), 'prefer' (other languages need a higher rating to auto-send), or 'only' "
            "(never request another language); null inherits the global requests.language_mode."
        ),
        json_schema_extra={"enum": [*LANGUAGE_MODES, None]},
    )
    req_preferred_languages: list[str] | None = Field(
        description=(
            "ISO 639-1 codes this row treats as preferred; null inherits the global "
            "requests.preferred_languages. An empty list is a row that cleared its languages."
        )
    )
    req_min_rating_other: float | None = Field(
        description=(
            "Rating another language must reach for this row to auto-send it. Null inherits the "
            "global requests.min_rating_other, which may itself be unset — in which case this row "
            "derives from its own req_min_rating plus 1.5."
        )
    )
    req_auto_user_tag: bool | None = Field(
        default=None,
        description=(
            "Tag this row's Sonarr/Radarr requests with the wanting person's slug; "
            "null inherits the global requests.auto_user_tag."
        ),
    )
    pick_order: str = closed_set_out(ORDERS, "How the delivered collection is ordered.")
    placement: str = closed_set_out(PLACEMENTS, "Where the OWNER's own collection appears.")
    placement_friends: str = closed_set_out(PLACEMENTS, "Where each FRIEND's own collection appears.")
    show_days: list[int] = Field(
        description="Days this row appears, as ISO weekdays (1=Monday .. 7=Sunday). Empty means every day."
    )
    shown_today: bool = Field(
        description=(
            "Whether this row is on its surfaces today, judged on the SERVER's clock — which is the "
            "clock the midnight schedule and Plex follow, not the viewer's."
        )
    )
    seasons: list[str] = Field(description="Seasons this row follows, in calendar order. Empty means not seasonal.")
    season_lead_days: int = Field(description="How many days before each season's day the row starts showing.")
    season_after_days: int = Field(description="How many days after each season's day the row stays up.")
    season_status: SeasonStatusOut | None = Field(
        description="Where the row is in its calendar today; null for a row that follows no season."
    )
    pin_top: bool
    hub_anchor: dict[str, HubAnchorOut]  # keyed by Plex section key, so the KEYS vary by library
    library_keys: list[str]
    poster: PosterOut
    ai_instructions: AiInstructionsOut
    theme_id: int | None = Field(description="The theme an AI row follows; null for an ordinary row.")
    theme_name: str | None = Field(description="The fixed theme's name; null for an ordinary row or an Explore row.")
    theme_emoji: str | None = Field(description="The fixed theme's emoji; null when it has none, or for Explore.")
    ai_paused: bool = Field(description="Whether the row's AI is paused: it keeps its theme but spends no tokens.")
    ai_tokens: int = Field(description="Tokens the AI has spent writing this row's themes.")
    theme_mode: Literal["fixed", "explore"] = Field(description="Whether an AI row keeps one theme or explores.")
    explore_brief: str = Field(description="What Explore is asked to look for; blank lets the AI choose.")
    theme_days: int | None = Field(description="Days a theme lasts in Explore; null is 7.")
    refresh_share: float | None = Field(description="Share of picks swapped on a refresh night; null keeps two thirds.")
    repeat_cooldown_days: int | None = Field(description="No repeats within this many days; null is off.")
    avoid_rows: list[str] | None = Field(description="Slugs of rows whose titles this row keeps out; null is none.")
    # The three keys below exist ONLY on a dry-run PATCH, where the row comes back unchanged and the
    # preview rides alongside it. Optional-with-None is a deliberate exception to `closed_set_out`'s
    # "declare responses required so a dropped field fails loudly": these are genuinely absent on a
    # live edit, and making them required would force every real save to invent them.
    #: True on a preview. Absent on a live edit, which HAS written by the time it answers.
    dry_run: bool | None = None
    #: What this edit would owe Plex, in the order it would happen. Empty means it owes Plex nothing.
    plan: list[PlanEntryOut] | None = None
    #: Why this preview may UNDER-report, in plain English, or null when it is complete.
    #:
    #: The third state, and the reason it is a field rather than an inference. `stranded_sections`
    #: answers an unreachable Plex with an EMPTY set — correct for a live edit ("not knowing which
    #: libraries exist must mean delete nothing") and a lie in a preview, because the planner then
    #: emits no reconcile at all and "this edit is harmless" and "I could not find out" arrive as the
    #: same empty plan. Nothing can be read back off `plan` to tell them apart, so it is said here.
    preview_incomplete: str | None = None


class RowDeletePreviewOut(PassthroughModel):
    """What `DELETE /collections/{id}?dry_run=true` WOULD do. Nothing is written."""

    dry_run: bool
    #: Collection titles that would be removed from Plex, for everyone who has this row.
    collections: list[str]
    #: Slugs of OTHER rows that would lose their shelf placement because it is positioned relative to
    #: this one. `delete_row_in_session` clears these; nothing warned about it before the fact.
    anchors_cleared: list[str]
    #: Whether every account's share filter would be recomputed (this row's label stops being
    #: declared shared, so the exclude has to come out of all of them).
    privacy_sync: bool
    #: Whether this row's cron schedule would stop firing.
    schedule_cleared: bool
    #: Why this preview may under-report, in plain English, or null when it is complete.
    preview_incomplete: str | None
    message: str


class CleanupOut(PassthroughModel):
    """What `POST /collections/{id}/cleanup` removed (or would remove, on a dry run)."""

    removed: list[str]
    dry_run: bool
    message: str


class PosterUploadOut(PassthroughModel):
    ok: bool
    mode: str


@router.get("", response_model=list[CollectionOut])
async def list_collections(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        collections = session.query(Collection).order_by(Collection.sort_order, Collection.id).all()
        # ONE clock read for the whole response, the same rule `_build_rows` follows: served a
        # millisecond either side of midnight, two rows in one list would otherwise report different
        # days.
        now = context_builder.local_now()
        catalogue = load_catalogue(session)
        previews = preview_titles(session, [c.slug for c in collections])
        return [serialize_row(session, c, now, catalogue=catalogue, previews=previews) for c in collections]


@router.post("", status_code=201, response_model=CollectionOut)
async def create_collection(body: CollectionIn, request: Request) -> dict:
    # `CollectionIn` is the body model for PATCH as well, which is where `dry_run` belongs — but that
    # makes it part of the POST schema too, and creation has nothing to preview. Silently ignoring it
    # would mean `POST {"dry_run": true}` answers 201 having created the row: a documented preview
    # flag that writes, which is the exact shape plex-safety rule 8 exists to prevent.
    if body.dry_run:
        raise HTTPException(status_code=422, detail="dry_run is only supported on PATCH and DELETE")
    validate_row(body)
    reject_season_name_without_seasons(
        body.name_template or body.name, body.seasons, row_has_theme=body.theme_id is not None
    )
    with request.app.state.sessions() as session:
        collection = create_row_in_session(session, request.app.state.secrets, body)
        catalogue = load_catalogue(session)
        queue_convergence_in_session(
            session,
            [schedule_rebuild_step()],
            domain="rows",
            effect_key=f"row-{collection.id}-create",
        )
        session.commit()
        created_slug = collection.slug
        result = serialize_row(session, collection, catalogue=catalogue)
    await jobs.drain_now(request.app.state, f"row '{created_slug}' was created")
    return result


@router.patch("/{collection_id}", response_model=CollectionOut)
async def update_collection(collection_id: int, body: CollectionIn, request: Request) -> dict:
    """Edit a row: validate → apply → plan the Plex work → enqueue it → drain.

    The decision table for "what does this edit owe Plex" lives in `shortlist/server/services/row_changes.py`, not here.
    It used to be eleven mutable flags accumulated down this handler and eight conditional
    dispatches at the bottom — untestable without a Plex context, and the place a missed branch
    silently left someone's row on the wrong Home screen.
    """
    validate_row(body)
    # Only touch fields the request actually sent, so a partial PATCH (e.g. an enable toggle) never
    # resets the columns it omitted back to CollectionIn's defaults.
    sent = body.model_fields_set
    state = request.app.state
    # A library narrowing needs Plex's current section list. Read it before the
    # database transaction; the mutation service only consumes this bounded
    # snapshot and never performs network I/O while local writes are pending.
    section_snapshot = None
    if not body.dry_run and sent & {"media", "library_keys"}:
        try:
            section_snapshot = state.run_service.build_context(dry_run=True).plex.sections()
        except Exception as e:
            logger.warning("could not read libraries to narrow row scope ({}) — nothing removed", type(e).__name__)
            section_snapshot = []
    queued_effects = False
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="collection not found")
        catalogue = load_catalogue(session)
        if "seasons" in sent:
            body.seasons = known_seasons(body.seasons, catalogue=catalogue)
        validate_anchor_rows(session, body, editing_slug=collection.slug)
        before = row_snapshot(session, collection)
        is_default = collection.slug == DEFAULT_SLUG
        merged_theme_id = body.theme_id if "theme_id" in sent else collection.theme_id
        merged_seasons = body.seasons if "seasons" in sent else list(collection.seasons or [])
        theme = None
        if is_default and body.theme_id is not None:
            raise HTTPException(
                status_code=422, detail="The default row can't be an AI row — add a new row from the AI template."
            )
        if sent & {"theme_id", "build", "seasons", "rewatch", "requests_row"}:
            theme = validate_theme(
                session,
                merged_theme_id,
                build=body.build if "build" in sent else collection.build,
                seasons=merged_seasons,
                rewatch=body.rewatch if "rewatch" in sent else bool(collection.rewatch),
                requests_row=body.requests_row if "requests_row" in sent else bool(collection.requests_row),
            )
        # The theme the row will follow once this lands: it fills `{theme}` in every title check below.
        stored_theme = session.get(Theme, merged_theme_id) if merged_theme_id is not None else None
        merged_spec = None if stored_theme is None else spec_from_row(stored_theme)
        if sent & (set(EXPLORE_COLUMNS) | {"theme_id"}):
            # Judged on the merged row. Clearing the theme resets these (`apply_row_patch`), so only what the
            # request itself sent counts against a row that has none.
            validate_explore(
                session,
                {c: getattr(body, c) if c in sent else getattr(collection, c) for c in EXPLORE_COLUMNS},
                theme_id=merged_theme_id,
                own_slug=collection.slug,
                only=sent & set(EXPLORE_COLUMNS),
                already_avoided=tuple(collection.avoid_rows or ()),
            )
        if is_default:
            # The default row is everyone's everyday row and its title is the global template, which every
            # person's row renders: it follows no season, so it can neither take one nor wear its name.
            if "seasons" in sent and body.seasons:
                raise HTTPException(
                    status_code=422,
                    detail="The default row can't follow seasons — add a new row from the Seasonal template.",
                )
            if "name" in sent:
                reject_season_name_without_seasons(body.name, [])
        elif sent & {"name", "name_template", "seasons", "theme_id"}:
            # Merged, like the title checks below: a PATCH that sends only the seasons, or only the name,
            # is judged against what the row will be once it lands.
            reject_season_name_without_seasons(
                merged_template(collection, body, sent), merged_seasons, row_has_theme=merged_theme_id is not None
            )
        # A rename only matters for a NON-default per-person row (the default row's title follows the
        # global Settings template, not this column). The old effective template is what the
        # collections on Plex are titled with right now — delivery renders from `name_template or name`.
        touching_name = before["build"] == "per_person" and not is_default and bool(sent & {"name", "name_template"})
        template_before = (collection.name_template or collection.name) if touching_name else ""
        template_after = template_before
        # Where the row builds, merged from the request and the row: a title only has to be unique among rows that could
        # build in one library with it (issue #121), so moving the LIBRARIES — or flipping a shared row
        # to per-person, the one build that collides with per-person rows — is a door onto the same
        # collision as renaming. "No NEW clashes" is tracked per ROW, not per library: a row that already
        # shared a title and a library with another (only a database from before this check) may move to
        # a second library they share. Tracking libraries would need the server's library list here.
        old_keys = [str(k) for k in (collection.library_keys or [])]
        merged_media = body.media if "media" in sent else collection.media
        merged_keys = [str(k) for k in body.library_keys] if "library_keys" in sent else old_keys
        merged_build = body.build if "build" in sent else collection.build
        if (merged_media, sorted(merged_keys), merged_build) != (collection.media, sorted(old_keys), collection.build):
            title_now = reconcile.row_template(session, collection.slug, state.secrets)
            fallback_now = collection.fallback_name or ""
            before_clashes = frozenset(
                row.slug
                for row in reconcile.rows_titled_from(
                    session,
                    title_now,
                    secrets=state.secrets,
                    exclude_slug=collection.slug,
                    build=collection.build,
                    fallback_name=fallback_now,
                    media=collection.media,
                    library_keys=old_keys,
                    theme=reconcile.theme_of(session, collection),
                )
            )
            reject_duplicate_name(
                session,
                state.secrets,
                title_now if is_default else merged_template(collection, body, sent),
                exclude_slug=collection.slug,
                build=merged_build,
                fallback_name=(body.fallback_name if "fallback_name" in sent else fallback_now) or "",
                media=merged_media,
                library_keys=merged_keys,
                already_clashing=before_clashes,
                theme=merged_spec,
            )
        # The clash check runs on the MERGED effective template, for the same reason `validate_pairing`
        # does: a PATCH may send either half. Sending `name_template` ALONE changes the title and used
        # to be checked by nothing at all, while sending `name` alone on a row that carries its own
        # template changes no title and was checked as though it did.
        # `fallback_name` is checked for the DEFAULT row too. Its `name`/`name_template` are exempt
        # because its title is the global setting (handled below), but its fallback IS a per-row
        # column, and the new "no name for newcomers" alert points operators straight at that field —
        # so it was the one write on the row editor with no duplicate-title check behind it. Two rows
        # rendering one title for one person in one library share a single Plex collection.
        if (sent & {"fallback_name"}) or (not is_default and sent & {"name", "name_template"}):
            merged = merged_template(collection, body, sent)
            # The FALLBACK is a real title that really gets written, so two rows carrying the same one
            # land on a single collection for every person who needs it — the same trap the template
            # check exists for. POST checked it from the start; PATCH did not, and PATCH is the path
            # the row editor saves through, so the state POST returns 422 for was reachable in one
            # ordinary edit.
            merged_fallback = (
                body.fallback_name if "fallback_name" in sent else (collection.fallback_name or "")
            ) or ""
            # A `{theme}` template keys on "" until filled, so "{theme} too" -> "{theme}" looked like no move at
            # all: compare the titles the row wears before and after, each with its own theme filled in.
            title_before = fill_theme(
                collection.name_template or collection.name, reconcile.theme_of(session, collection)
            )
            moved = reconcile.title_key(fill_theme(merged, merged_spec)) != reconcile.title_key(title_before)
            fallback_moved = reconcile.title_key(merged_fallback) != reconcile.title_key(collection.fallback_name or "")
            # Only when the TITLE actually moves. The editor re-sends `name` on every save, so
            # checking on "was the field present" refused a size-only edit on a row that already
            # clashes — with a message about names, for a change that was not about names. A row in
            # that state (created before this guard, or restored from a backup) must stay editable;
            # the rule is "no NEW clashes", not "no clashing row may be touched".
            if moved or fallback_moved:
                reject_duplicate_name(
                    session,
                    state.secrets,
                    merged,
                    exclude_slug=collection.slug,
                    build=merged_build,
                    fallback_name=merged_fallback,
                    media=merged_media,
                    library_keys=merged_keys,
                    theme=merged_spec,
                )
        # A season newly ticked gives a `{season}` row a title it never wore: "{season} picks" becomes
        # "Thanksgiving picks", the title a plain row beside it may already have (#137 I-2). The checks above run
        # only when the name, libraries or build move, so ticking a season was the one door left open. Only
        # the seasons ADDED are checked, each as the row would be titled in it: unticking can add no clash.
        if "seasons" in sent and not is_default:
            ticked = [slug for slug in body.seasons if slug not in (collection.seasons or [])]
            seasonal_template = merged_template(collection, body, sent)
            for slug in ticked if uses_season(seasonal_template) else []:
                reject_duplicate_name(
                    session,
                    state.secrets,
                    reconcile.season_title(seasonal_template, catalogue[slug]),
                    exclude_slug=collection.slug,
                    build=merged_build,
                    media=merged_media,
                    library_keys=merged_keys,
                    theme=merged_spec,
                )
        # A theme newly set gives a `{theme}` row a title it never wore, as a ticked season does.
        if theme is not None and "theme_id" in sent and theme.id != collection.theme_id and not is_default:
            themed_template = merged_template(collection, body, sent)
            if uses_theme(themed_template):
                reject_duplicate_name(
                    session,
                    state.secrets,
                    themed_template,
                    exclude_slug=collection.slug,
                    build=merged_build,
                    media=merged_media,
                    library_keys=merged_keys,
                    theme=merged_spec,
                )
        if sent & TITLE_MOVING_FIELDS and not is_default:
            reject_new_person_title_clash(
                session,
                state.secrets,
                collection,
                body,
                sent,
                media=merged_media,
                library_keys=merged_keys,
                theme=merged_spec,
            )
        # The default row has no per-collection name: its title IS the global `row.name_template`
        # (Settings → Defaults), which delivery renders per library. So a rename of it writes that
        # global setting — NOT this column — because a per-collection template would win over each
        # user's own `row_name_tpl` override in `resolve_row_template`. Its `name` column is never
        # touched (the editor round-trips the template as the name, which must not clobber it).
        #
        # RESOLVED here, WRITTEN below the dry-run return: `SettingsStore.set` commits inside itself,
        # so writing it at this point would make a PREVIEW of this rename permanent — the one edit on
        # this handler that an end-of-request rollback could not take back.
        if "name" in sent and is_default:
            new_template = body.name.strip()
            previous = SettingsStore(session, state.secrets).get("row.name_template") or ""
            if new_template and new_template != previous:
                # Renaming the default row retitles it on Plex just as surely as renaming any other,
                # so it owes the same clash check — onto the title EVERY other row already renders.
                reject_duplicate_name(
                    session,
                    state.secrets,
                    new_template,
                    exclude_slug=DEFAULT_SLUG,
                    build="per_person",
                    media=merged_media,
                    library_keys=merged_keys,
                )
                template_before, template_after = previous, new_template
        merged_min_year = body.min_year if "min_year" in sent else collection.min_year
        merged_max_year = body.max_year if "max_year" in sent else collection.max_year
        if merged_min_year is not None and merged_max_year is not None and merged_min_year > merged_max_year:
            raise HTTPException(status_code=422, detail="The earliest year can't be later than the latest year.")
        # Checked against the MERGED row, never the request body — see `validate_pairing`.
        validate_pairing(
            rewatch=body.rewatch if "rewatch" in sent else bool(collection.rewatch),
            unstarted_only=body.unstarted_only if "unstarted_only" in sent else bool(collection.unstarted_only),
            media=body.media if "media" in sent else collection.media,
        )
        validate_requests_row(
            requests_row=body.requests_row if "requests_row" in sent else bool(collection.requests_row),
            build=body.build if "build" in sent else collection.build,
            rewatch=body.rewatch if "rewatch" in sent else bool(collection.rewatch),
            seasons=body.seasons if "seasons" in sent else list(collection.seasons or []),
            has_theme=merged_theme_id is not None,
        )
        # Hoisted above the writes: `set_audience` raises this from inside the apply half, which on a
        # default-row rename meant answering 422 after `SettingsStore.set` had already committed.
        if sent & {"audience", "audience_user_ids"}:
            validate_audience_ids(session, body)

        # Everything above VALIDATES; everything below WRITES. A preview leaves between the two, so it
        # is refused by exactly what would refuse the save and has still written nothing.
        if body.dry_run:
            if touching_name:
                template_after = merged_template(collection, body, sent)
            preview_change = row_change(
                before,
                projected_snapshot(session, collection, body, sent),
                template_before=template_before,
                template_after=template_after,
                defer_rename=body.defer_rename,
            )
            preview_row = serialize_row(session, collection, catalogue=catalogue)
        else:
            collection, steps, _diff = apply_prevalidated_row_update_in_session(
                session,
                state.secrets,
                collection_id,
                body,
                sent,
                library_sections=section_snapshot,
            )
            if steps:
                queue_convergence_in_session(session, steps, domain="rows", effect_key=f"row-{collection.id}")
                queued_effects = True
            updated_slug = collection.slug
            result = serialize_row(session, collection, catalogue=catalogue)
            session.commit()

    if body.dry_run:
        warnings: list[str] = []

        def preview_stranded() -> set[str]:
            return stranded_sections(
                state,
                old_media=preview_change.media_before,
                old_keys=list(preview_change.libraries_before),
                new_media=preview_change.media_after,
                new_keys=list(preview_change.libraries_after),
                unreadable=warnings,
            )

        plan = await run_in_threadpool(plan_row_changes, preview_change, preview_stranded)
        return {
            **preview_row,
            "dry_run": True,
            "plan": await _plan_view(state, plan, preview_change, warnings=warnings),
            "preview_incomplete": " ".join(warnings) or None,
        }

    if queued_effects:
        await jobs.drain_now(state, f"row '{updated_slug}' was edited")
    return result


async def _plan_view(state, plan: list[PlannedWork], change: RowChange, *, warnings: list[str]) -> list[dict]:
    """`PlannedWork` rendered as the would-be diff, resolving the removals against Plex.

    A reconcile is the only kind that DELETES, so it is the only one that pays for a real read — the
    same walk, at the same cost, that `POST /{id}/cleanup?dry_run=true` already makes. The other
    kinds are declarative and need no read, which is why an edit that owes Plex nothing destructive
    previews for free.

    A failed walk appends to ``warnings`` rather than raising: the rest of the plan is still worth
    showing, but the entry's empty ``collections`` must not read as "this would remove nothing".

    Titles are resolved against the row's CURRENT template, because that is what the collections on
    Plex are wearing at the moment the operator is looking at them. The live job resolves the same
    question after the edit has committed, so an edit that renames AND narrows in one save previews
    the old titles and removes the new ones. Both find the same collections — `reconcile_row_removal`
    matches on rendered titles UNIONED with the delivery ledger's recorded ones — but the strings
    shown here are the ones on the server today, which is the pair a person can actually check.
    """
    view: list[dict] = []
    for work in plan:
        entry = {
            "kind": work.kind,
            "reason": work.scope,
            "collections": [],
            "only_user_ids": list(work.only_user_ids or ()),
            "in_sections": list(work.in_sections or ()),
        }
        if work.kind != RECONCILE:
            view.append(entry)
            continue
        removed, error = await reconcile.preview_row_removal(
            state,
            slug=change.slug,
            build=change.build_before,  # the collections at stake are the OLD build's
            only_user_ids=set(work.only_user_ids) if work.only_user_ids is not None else None,
            in_sections=set(work.in_sections) if work.in_sections is not None else None,
        )
        if error:
            warnings.append(
                f"Plex could not be read all the way through, so this list may be incomplete ({error}). "
                "Check the connection and preview again."
            )
        view.append({**entry, "collections": removed})
    return view


# `response_model=None` because the return annotation is a union: FastAPI would otherwise try to
# build a body model from it, which a 204 route may not have. The preview's shape is documented via
# `responses` instead, so the SPA's generated types still know about it.
@router.delete(
    "/{collection_id}",
    status_code=204,
    response_model=None,
    responses={200: {"model": RowDeletePreviewOut, "description": "A dry-run preview; nothing was deleted."}},
)
async def delete_collection(collection_id: int, request: Request, dry_run: bool = False) -> Response | None:
    """Delete a row, or with ``dry_run=true`` report what deleting it would do and write nothing.

    A query parameter rather than a body: `DELETE` bodies are awkward through both `fetch` and
    FastAPI, and this works with the SPA's existing `request()` helper unchanged.

    `POST /{id}/cleanup?dry_run=true` already previews the PLEX half. What only this can show is the
    LOCAL half, and one part of it is a genuine surprise: deleting this row silently strips every
    OTHER row's shelf placement that was positioned relative to it, changing where two other people's
    rows appear. That was logged after the fact and warned about nowhere.

    A preview answers 200 with :class:`RowDeletePreviewOut` instead of the delete's 204.
    """
    state = request.app.state
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="collection not found")
        # The default row is deletable like any other: rows are user-created,
        # `EngineConfig.rows_defined` means an empty list is "everything is off" rather than
        # "resurrect the default", and an un-deletable item with no visible reason is its own bug
        # report. Disabling it is still the reversible option; this is the permanent one.
        slug, build = collection.slug, collection.build
        # Captured while the row still exists and carried in the job payload: after the DB row is gone
        # there is nothing left to resolve the title its collections were built under, so a retry
        # (Plex down at this moment, container killed mid-write) would have nothing to address.
        template = reconcile.row_template(session, slug, state.secrets)
        if dry_run:
            # The same walk `delete_row_in_session` does, minus the write — one shared predicate, so the
            # warning and the delete that carries it out cannot disagree.
            anchors = rows_anchored_to(session, slug)
            has_schedule = bool((collection.schedule or "").strip())
            # Resolved, not read off the column: the DEFAULT row's `name` is stale seed data and its
            # real title is the global template — `serialize_row` substitutes it for exactly this
            # reason, so naming the raw column here would preview a title the UI shows nowhere.
            name = template if slug == DEFAULT_SLUG else collection.name

    if dry_run:
        removed, error = await reconcile.preview_row_removal(state, slug=slug, build=build, template=template)
        # Built through the model rather than as a bare dict: a hand-written `JSONResponse` is not
        # validated by FastAPI, so a renamed key or an inverted flag would ship silently while
        # `RowDeletePreviewOut` went on describing the old shape in the schema the SPA generates from.
        return JSONResponse(
            RowDeletePreviewOut(
                dry_run=True,
                collections=removed,
                anchors_cleared=anchors,
                privacy_sync=build == "shared",
                schedule_cleared=has_schedule,
                preview_incomplete=(
                    f"Plex could not be read all the way through, so this list may be incomplete ({error}). "
                    "Check the connection and preview again."
                    if error
                    else None
                ),
                message=f"Would remove {len(removed)} collection(s) from Plex for “{name}”.",
            ).model_dump()
        )

    with state.sessions() as session:
        deleted = delete_row_in_session(session, collection_id, template=template)
        steps = [reconcile_step(slug, build=build, scope="collection.delete", template=template)]
        if build == "shared":
            # The shared label is no longer declared, so every account's excludes must be recomputed
            # after the removal and before any later visibility-increasing work.
            steps.append(privacy_sync_step(f"row '{slug}' was deleted"))
        steps.append(schedule_rebuild_step())
        queue_convergence_in_session(session, steps, domain="rows", effect_key=f"row-{collection_id}-delete")
        session.commit()
    if deleted.anchors_cleared:
        logger.info(
            "row '{}' was deleted — {} row(s) positioned relative to it now follow the library default instead: {}",
            slug,
            len(deleted.anchors_cleared),
            ", ".join(deleted.anchors_cleared),
        )
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
            reject_season_name_without_seasons(
                new_template,
                [] if slug == DEFAULT_SLUG else list(collection.seasons or []),
                row_has_theme=collection.theme_id is not None,
            )
            reject_duplicate_name(
                session,
                request.app.state.secrets,
                new_template,
                exclude_slug=slug,
                build=build,
                media=collection.media,
                library_keys=collection.library_keys or [],
                theme=reconcile.theme_of(session, collection),
            )
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
        renames: list[dict] = []
        failures: list[str] = []
        error: str | None = None
        # Floor of safe mode: if the iterator never reaches its done event, assume the preview was forced.
        effective_dry_run = force_dry_run()
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
                if event.get("error"):
                    failures.append(f"{event.get('user', '?')}: {event['error']}")
                elif event.get("done"):
                    effective_dry_run = bool(event.get("dry_run", effective_dry_run))
                else:
                    renames.append(event)
                q.put(event)
        except Exception as e:
            # Redacted: this catches anything the generator raises BEFORE its own per-collection
            # handler — a `plex.sections()` failure carrying a tokened URL — and it goes straight to
            # the browser (rule 9).
            error = redact(f"{type(e).__name__}: {e}")
            q.put({"error": error})
        finally:
            # Rule 10: the stream does real editTitle writes, so it leaves the same audit row as
            # `run_row_rename_from_plex`. Before the sentinel, so the audit exists once the stream ends.
            try:
                write_audit(
                    state,
                    "collection.rename",
                    "info",
                    slug=slug,
                    renames=renames,
                    new_template=new_template,
                    dry_run=effective_dry_run,
                    error=error or ("; ".join(failures) or None),
                )
            except Exception as audit_error:
                logger.warning("rename audit not written: {}", redact(str(audit_error)))
            q.put(None)  # sentinel

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _run)

    async def generate():
        while True:
            # `Queue.get` is a BLOCKING stdlib call. A plain call with a
            # 0.1s timeout would freeze the event loop for that long on every empty tick — and a
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

    # A real cleanup is a Plex writer, so it takes the one-writer lock the job worker and every run take.
    # Without it, a cleanup overlapping a run that delivers this row could forget the ledger key the run had
    # just written, leaving that row's plays uncredited until a later delivery found it by label again.
    # Same policy as uninstall: 409 for a RUN, which holds the lock for many minutes; a bounded wait for a
    # writer JOB, which is seconds. A preview writes nothing and never takes it.
    writer = None if body.dry_run else jobs.plex_writer_lock()
    if writer is not None:
        if state.run_service.is_running():
            raise HTTPException(
                status_code=409,
                detail="A run is updating Plex right now, so nothing was removed. Try again once it finishes.",
            )
        try:
            await asyncio.wait_for(writer.acquire(), timeout=jobs.WRITER_LOCK_WAIT_S)
        except TimeoutError:
            raise HTTPException(
                status_code=409,
                detail="Shortlist is busy making other changes on Plex, so nothing was removed. Try again in a minute.",
            ) from None
    try:
        removed, error = await reconcile.run_reconcile(
            state, slug=slug, build=build, dry_run=body.dry_run, scope="collection.cleanup"
        )
    finally:
        if writer is not None:
            writer.release()
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


class AiPauseRequest(StrictRequestModel):
    paused: bool


@router.post("/{collection_id}/ai-pause", response_model=CollectionOut)
async def pause_ai(collection_id: int, body: AiPauseRequest, request: Request) -> dict:
    """Pause or resume an AI row's AI. A paused row keeps its theme and keeps building from it; it just never
    spends tokens writing or refining one (409 from the theme endpoints until resumed)."""
    with request.app.state.sessions() as session:
        collection = _require_collection(session, collection_id)
        from shortlist.server.services.row_mutations import set_ai_paused_in_session

        set_ai_paused_in_session(session, collection, body.paused)
        session.commit()
        return serialize_row(session, collection, catalogue=load_catalogue(session))


class ThemeRefOut(PassthroughModel):
    """A theme as one person's rotation holds it: which one, when it started, and when it hands over."""

    theme_id: int | None = Field(description="The stored theme; null once it has been deleted.")
    name: str
    emoji: str | None
    started_at: str
    due_at: str | None


class RotationTargetOut(PassthroughModel):
    user_id: int
    name: str
    current: ThemeRefOut | None
    next: ThemeRefOut | None = Field(description="The theme queued to start when the current one ends.")
    started_at: str | None = Field(description="When the current theme started.")
    next_due_at: str | None = Field(description="When the current theme ends and the next one starts.")
    history: list[ThemeRefOut] = Field(description="Their earlier themes on this row, newest first.")


class ThemeRotationOut(PassthroughModel):
    mode: Literal["fixed", "explore"]
    days: int = Field(description="How many days a theme lasts.")
    targets: list[RotationTargetOut]


class UpNextRequest(StrictRequestModel):
    user_id: int
    theme_id: int


class RegenerateRequest(StrictRequestModel):
    user_id: int


_HISTORY_SHOWN = 6


def _ai_row(session, collection_id: int) -> Collection:
    """The row, or 404 when it is not an AI row: only those have a theme to rotate."""
    collection = _require_collection(session, collection_id)
    if collection.theme_id is None:
        raise HTTPException(status_code=404, detail="That row isn't an AI row.")
    return collection


def _theme_ref(row, themes: dict[int, Theme]) -> dict:
    theme = themes.get(row.theme_id) if row.theme_id is not None else None
    return {
        "theme_id": row.theme_id,
        "name": theme.name if theme is not None else row.theme_name,
        "emoji": theme.emoji if theme is not None else None,
        "started_at": iso_utc(row.started_at),
        "due_at": iso_utc(row.due_at),
    }


def _person_name(user: User) -> str:
    return user.nickname or user.friendly_name or user.username


def _audience_person(session, collection: Collection, user_id: int) -> User:
    from shortlist.server.services.theme_rotation import audience_users

    person = next((u for u in audience_users(session, collection) if u.id == user_id), None)
    if person is None:
        raise HTTPException(status_code=404, detail="That person isn't in this row's audience.")
    return person


@router.get("/{collection_id}/theme-rotation", response_model=ThemeRotationOut)
async def get_theme_rotation(collection_id: int, request: Request) -> dict:
    """Where each person's Explore rotation stands: their current theme, the one queued next, and what came before."""
    from shortlist.server.services.theme_rotation import DEFAULT_THEME_DAYS, audience_users

    with request.app.state.sessions() as session:
        collection = _ai_row(session, collection_id)
        days = collection.theme_days or DEFAULT_THEME_DAYS
        targets = []
        for person in audience_users(session, collection):
            rows = (
                session.query(ThemeHistory)
                .filter(ThemeHistory.collection_id == collection.id, ThemeHistory.user_id == person.id)
                .order_by(ThemeHistory.started_at.desc(), ThemeHistory.id.desc())
                .all()
            )
            themes = {
                t.id: t for t in session.query(Theme).filter(Theme.id.in_([r.theme_id for r in rows if r.theme_id]))
            }
            current = next((r for r in rows if r.state == "current"), None)
            upcoming = next((r for r in rows if r.state == "next"), None)
            targets.append(
                {
                    "user_id": person.id,
                    "name": _person_name(person),
                    "current": None if current is None else _theme_ref(current, themes),
                    "next": None if upcoming is None else _theme_ref(upcoming, themes),
                    "started_at": None if current is None else iso_utc(current.started_at),
                    "next_due_at": None if current is None else iso_utc(current.started_at + timedelta(days=days)),
                    "history": [_theme_ref(r, themes) for r in rows if r.state == "past"][:_HISTORY_SHOWN],
                }
            )
        return {"mode": collection.theme_mode or "fixed", "days": days, "targets": targets}


@router.put("/{collection_id}/up-next", response_model=ThemeRefOut)
async def set_up_next(collection_id: int, body: UpNextRequest, request: Request) -> dict:
    """Point a person's "Up next" at a saved theme, replacing any theme already queued. Changes no Plex state."""
    from shortlist.server.api.seasons import off_loop
    from shortlist.server.services.person_up_next import apply_up_next_in_session, prepare_up_next_in_session
    from shortlist.server.services.theme_rotation import target_lock
    from shortlist.server.services.theme_store import TitleClash

    state = request.app.state

    def write() -> dict:
        # Under the person's rotation lock: a nightly pass may be mid-write for the same person.
        with target_lock(collection_id, body.user_id), state.sessions() as session:
            try:
                selection = prepare_up_next_in_session(
                    session, collection_id, body.user_id, body.theme_id, secrets=state.secrets
                )
                queued = apply_up_next_in_session(session, selection)
            except LookupError as error:
                raise HTTPException(status_code=404, detail=str(error)) from None
            except (TitleClash, ValueError) as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            collection, person, theme = selection.collection, selection.person, selection.theme
            add_audit(
                session,
                "collection.up_next",
                "info",
                slug=collection.slug,
                user=person.slug,
                theme=theme.slug,
            )
            session.commit()
            return _theme_ref(queued, {theme.id: theme})

    return await off_loop(write, "up-next")


@router.post("/{collection_id}/up-next/regenerate", response_model=ThemeRefOut)
async def regenerate_up_next(collection_id: int, body: RegenerateRequest, request: Request) -> dict:
    """Write a new "Up next" theme for one person now, with one AI call, replacing any theme queued.

    409 while the row's AI is paused, 422 without an AI provider or when the row isn't set to Explore.
    """
    from shortlist.server.api.seasons import off_loop
    from shortlist.server.api.themes import PAUSED, PREVIEW_MAX_DETAILS
    from shortlist.server.services import theme_rotation, theme_store
    from shortlist.server.services.theme_author import ThemeAuthorError

    state = request.app.state
    with state.sessions() as session:
        collection = _ai_row(session, collection_id)
        if collection.theme_mode != "explore":
            raise HTTPException(status_code=422, detail="Turn on Explore for this row first.")
        if collection.ai_paused:
            raise HTTPException(status_code=409, detail=PAUSED)
        _audience_person(session, collection, body.user_id)

    def write() -> dict:
        tools = theme_rotation.authoring_tools(state)
        if tools.unavailable:
            raise HTTPException(status_code=tools.status, detail=tools.unavailable)
        spent: list[int] = []
        try:
            with theme_rotation.target_lock(collection_id, body.user_id), state.sessions() as session:
                collection = session.get(Collection, collection_id)
                try:
                    theme = theme_rotation.author_for_person(
                        session,
                        sessions=state.sessions,
                        secrets=state.secrets,
                        collection=collection,
                        user_id=body.user_id,
                        author=theme_rotation.author_theme,
                        curator=tools.curator,
                        tmdb=tools.tmdb,
                        plex=tools.plex,
                        profile_for=state.run_service.profile_with_history,
                        spent=spent,
                        max_details=PREVIEW_MAX_DETAILS,
                    )
                except ThemeAuthorError as e:
                    raise HTTPException(status_code=422, detail=str(e)) from None
                except LookupError:
                    raise HTTPException(status_code=422, detail="That person is no longer on the server.") from None
                except RuntimeError:
                    raise HTTPException(
                        status_code=502,
                        detail="Shortlist couldn't read their watch history. Check the Plex connection.",
                    ) from None
                except theme_store.RowPaused:
                    raise HTTPException(status_code=409, detail=PAUSED) from None
                except theme_store.TitleClash as e:
                    raise HTTPException(status_code=422, detail=str(e)) from None
                queued = theme_rotation.queue_next(
                    session, collection, body.user_id, theme, datetime.now(UTC), checked=True
                )
                session.commit()
                return _theme_ref(queued, {theme.id: theme})
        except Exception:
            # The AI call ran, so its cost stands even though the save rolled back.
            if spent:
                with state.sessions() as session:
                    row = session.get(Collection, collection_id)
                    if row is not None:
                        theme_store.add_row_tokens(session, row, sum(spent))
                        session.commit()
            raise

    return await off_loop(write, "theme authoring")


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
    #: Of the watched, the ones they saw out. A TV library finishes far less of what it lands than a
    #: movie library does, for reasons that have nothing to do with the row — so the two numbers are
    #: shown side by side rather than one being presented as the row's score.
    finished: int
    rate: float | None  # None when nothing was delivered there


class RowMaturedCohort(PassthroughModel):
    """The picks old enough to be judged: delivered at least `matured_days` ago, so every one of them
    has had its full window to be watched."""

    delivered: int
    watched: int
    finished: int
    rate: float | None
    cohort_to: str


class RowEffectivenessOut(PassthroughModel):
    """Whether one row is working, for the panel beside its settings."""

    delivered: int  # all time, distinct person+title
    watched: int
    finished: int
    # Runs still on record that built this row — counted exactly as `/api/runs?collection=<slug>`
    # selects them, since the panel's Runs tile links there. Pruned runs are in neither.
    runs: int
    first_delivered_at: str | None  # None = this row has never delivered anything
    last_delivered_at: str | None  # None = same; otherwise the most recent delivery
    matured_days: int
    shared_titles: dict[str, int] | None  # titles the shared copy holds now, by media type; None if never delivered
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
