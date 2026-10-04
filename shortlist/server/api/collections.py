"""Collections API: define curated rows — how each is built (per-person | shared), who it's for
(audience), and its recipe (size, media, name). Owner-only."""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, func
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse, StreamingResponse

import shortlist.server.services.context_builder as context_builder
from shortlist.engine import seasons as seasons_mod
from shortlist.engine.candidates import KNOWN_SOURCES
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.delivery import target_sections
from shortlist.engine.models import (
    LANGUAGE_MODES,
    MAX_REFRESH_DAYS,
    MAX_ROW_SIZE,
    MIN_ROW_SIZE,
    SONARR_MONITOR_MODES,
    RowSpec,
    dedupe_slug,
    normalise_languages,
    row_language_mode_or_inherit,
    row_languages_or_inherit,
    row_monitor_or_inherit,
    slugify,
)
from shortlist.engine.placeholders import fill_theme, refusal, uses_season, uses_theme
from shortlist.engine.rows import row_shown_today
from shortlist.engine.themes import ThemeSpec
from shortlist.engine.web_guidance import INSTRUCTION_MODES, MAX_INSTRUCTIONS_CHARS, AiInstructions
from shortlist.server.api.row_changes import (
    POSTER_RESET,
    PRIVACY_SYNC,
    RECONCILE,
    RENAME,
    VISIBILITY,
    PlannedWork,
    RowChange,
    plan_row_changes,
)
from shortlist.server.api.schemas import PassthroughModel, StrictRequestModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    CollectionAudience,
    Delivery,
    Event,
    Job,
    PickRow,
    RequestCandidate,
    Run,
    RunSharedRow,
    SharedRowWatch,
    Theme,
    ThemeHistory,
    User,
)
from shortlist.server.scheduler import crontab_trigger, rebuild_schedule
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services import jobs, poster_service, report_service
from shortlist.server.services.audit import add_audit
from shortlist.server.services.poster_service import load_upload
from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.services.theme_store import spec_from_row
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
    ``top`` needs neither; otherwise exactly one of ``row``/``anchor`` must be set. ``enabled``
    false is a placement in its own right — "never position this row" — and needs neither.

    ``row`` is a slug rather than a title because a per-person row is one Plex collection PER PERSON:
    a title names one account's copy and is meaningless for everyone else, which is what made the
    picker offer forty identical-looking options and place none of them (issue #81)."""

    anchor: str = Field(default="", max_length=255)
    row: str = Field(default="", max_length=255)
    before: bool = False
    top: bool = False
    #: The owner's per-row switch for this library. False means Shortlist never positions this row,
    #: so it sits wherever Plex put it — a new row starts at the bottom of the shelf.
    enabled: bool = True


class PosterIn(BaseModel):
    """A row's custom-poster config. ``mode`` "" leaves Plex artwork alone; "upload" uses the image
    stored via the upload endpoint; "text" renders ``title``/``subtitle`` with the built-in Pillow
    engine (no AI); "ai" renders them with the curator provider's image model. The text fields share
    the row-name placeholders ({user}/{library_name}/{top_seed})."""

    mode: str = _closed_set(POSTER_MODES, "", 'Poster source; "" leaves Plex artwork alone.')
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=120)
    style: str = Field(default="", max_length=400)


class AiInstructionsIn(StrictRequestModel):
    """What AI web search should look for on this row (#138). ``default`` uses the built-in wording
    plus the server-wide instructions; ``add`` appends ``text`` to them; ``own`` replaces them."""

    mode: str = _closed_set(set(INSTRUCTION_MODES), "default", "AI instructions must be default, add or own")
    text: str = Field(default="", max_length=MAX_INSTRUCTIONS_CHARS)


class CollectionIn(StrictRequestModel):
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

    @field_validator("fallback_name")
    @classmethod
    def _a_fallback_cannot_need_a_seed(cls, value: str) -> str:
        """The fallback is what a row is called when `{top_seed}` CANNOT be filled — so one that also
        needs a seed is no fallback at all. `render_row_name` refuses it, which was the whole of the
        behaviour: the API accepted it, the "no name for newcomers" alert saw a non-empty value and
        went quiet, and the row still was not built. The operator does exactly what the alert asks and
        is told it worked. That is issue #84's symptom re-entering through the field built to fix it.
        """
        if why := refusal(value, "fallback"):
            raise ValueError(why)
        return value

    min_watchers: int = Field(default=2, ge=2)  # a public row must never be shaped by one person
    request_tag: str = Field(default="", max_length=64)  # tag added to titles requested via this row
    candidate_sources: list[str] = Field(default_factory=list)  # [] -> inherit global candidates.sources
    watched_pct: float | None = Field(default=None, ge=0.0, le=1.0)  # None -> inherit global watched cap
    # Set by a caller that is about to stream the rename itself (the rename page). The PATCH then
    # saves the template but leaves the Plex work alone, instead of renaming everything inline and
    # leaving the stream to report "renamed 0 collections" for a rename that did happen.
    defer_rename: bool = False
    # Preview only: validate this edit, work out what it would owe Plex, and write NOTHING — not the
    # row, not the audience, not `row.name_template`, not the job queue (plex-safety rule 8).
    # Implemented by PROJECTING the post-edit snapshot (`_projected_snapshot`), never by applying the
    # edit and rolling back: `SettingsStore.set` commits inside itself, so a rollback would not undo
    # a default-row rename and the "preview" would have permanently retitled every row on the server.
    # Composes with `defer_rename` rather than clashing with it — a deferred rename simply plans no
    # RENAME, in the preview exactly as in the save.
    dry_run: bool = False
    # Lead the row with already-finished titles (a rewatch shelf) rather than merely permitting them.
    rewatch: bool = False
    # Rewatch rows only: leave out titles finished within this many days. 0 = no cooldown.
    rewatch_cooldown_days: int = Field(default=30, ge=0, le=MAX_REFRESH_DAYS)
    # Shows only: exclude every series this person has started, not just the ones they finished.
    unstarted_only: bool = False
    refresh_days: int | None = Field(default=None, ge=0, le=MAX_REFRESH_DAYS)  # None -> inherit the global cadence
    # How long this row waits when its owner has watched nothing since it was built. 0 = never wait;
    # None -> inherit the global recommendations.idle_hold_days.
    idle_hold_days: int | None = Field(default=None, ge=0, le=MAX_REFRESH_DAYS)
    # How much this row weights a title's release date. None -> inherit recommendations.recency.
    recency: float | None = Field(default=None, ge=0.0, le=1.0)
    recent_count: int | None = Field(default=None, ge=1, le=25)  # None -> inherit global recent_count
    max_seeds: int | None = Field(default=None, ge=1, le=100)  # None -> inherit the engine default (30)
    # Per-row limits on what may be picked; None = no limit (#138). Year order is checked in `_validate`.
    max_runtime: int | None = Field(default=None, ge=1, le=600)  # minutes
    min_year: int | None = Field(default=None, ge=1870, le=2100)
    max_year: int | None = Field(default=None, ge=1870, le=2100)
    min_rating: float | None = Field(default=None, ge=0.0, le=10.0)  # TMDB vote_average
    # "popular" | "skip" | None -> inherit the global recommendations.cold_start. Enforced in
    # `_validate`, like every other closed set here.
    cold_start: str | None = Field(
        default=None,
        json_schema_extra={"enum": [*sorted(COLD_STARTS), None]},
        description="What this row does for someone with too little watch history; null inherits the global setting.",
    )
    # This row's own Sonarr/Radarr settings; null -> inherit the global `requests.*` setting. Only
    # the profile and root folder are per row — URL and API key stay global (one Radarr, different
    # folders). `max_per_run` and the rating source are deliberately NOT here: they are the run's
    # ceiling and its one MDBList account, and `req_max_per_row` may only restrict below the former.
    # Meaningless on a shared row, which surfaces nothing missing to request.
    req_min_rating: float | None = Field(default=None, ge=0.0, le=10.0)
    req_min_votes: int | None = Field(default=None, ge=0)
    req_min_demand: int | None = Field(default=None, ge=1)
    req_min_year: int | None = Field(default=None, ge=0, le=2999)
    req_max_year: int | None = Field(default=None, ge=0, le=2999)
    req_auto_send: bool | None = None
    req_auto_min_demand: int | None = Field(default=None, ge=1)
    req_auto_min_rating: float | None = Field(default=None, ge=0.0, le=10.0)
    req_max_per_row: int | None = Field(default=None, ge=0, le=100)
    req_radarr_quality_profile_id: int | None = Field(default=None, ge=1)
    req_radarr_root_folder: str | None = Field(default=None, max_length=512)
    req_sonarr_quality_profile_id: int | None = Field(default=None, ge=1)
    req_sonarr_root_folder: str | None = Field(default=None, max_length=512)
    # How much of a show Sonarr monitors for this row; null inherits requests.sonarr.monitor.
    # Enforced in `_validate` (a plain-English 422 naming the modes, which a Pydantic enum error is
    # not). The enum is ADVERTISED so the SPA's generated type is the union rather than a bare
    # string — the row editor picks from it, and a bare string there checks nothing.
    req_sonarr_monitor: str | None = Field(
        default=None, max_length=32, json_schema_extra={"enum": [*SONARR_MONITOR_MODES, None]}
    )
    # This row's language preference; null on any of the three inherits the matching global. Same
    # advertised-enum reasoning as `req_sonarr_monitor` above.
    req_language_mode: str | None = Field(
        default=None, max_length=16, json_schema_extra={"enum": [*LANGUAGE_MODES, None]}
    )
    # null inherits the owner's list; [] is a row that CLEARED its languages and means it (in "only"
    # mode it requests nothing). The two must stay distinct all the way to the column — see 0085.
    req_preferred_languages: list[str] | None = Field(default=None, max_length=50)
    # null means "follow this row's own req_min_rating + 1.5", not "unset" — so a row that raises its
    # base floor carries this bar up with it.
    req_min_rating_other: float | None = Field(default=None, ge=0.0, le=10.0)
    # Tag this row's requests with the wanting person's slug; null inherits requests.auto_user_tag.
    req_auto_user_tag: bool | None = None
    # How many recent watches the row cycles between, one per run. 1 = always the most recent.
    # Capped at 20: past that the "recent" the row's title claims stops being true, and the cycle takes
    # three weeks to come round — indistinguishable from the stuck row this exists to fix.
    seed_window: int = Field(default=1, ge=1, le=20)
    pick_order: str = _closed_set(ORDERS, "best", "How the delivered collection is ordered.")
    library_keys: list[str] = Field(default_factory=list)  # [] -> every library of the row's media type
    placement: str = _closed_set(PLACEMENTS, "both", "Where the OWNER's own collection appears.")
    placement_friends: str = _closed_set(PLACEMENTS, "both", "Where each FRIEND's own collection appears.")
    # WHICH DAYS the row appears, as ISO weekdays (1=Mon .. 7=Sun). [] -> every day. The pair with
    # `placement` is the whole of "When it appears" (issue #102): placement is WHERE, this is WHEN.
    show_days: list[int] = Field(
        default_factory=list,
        description="Days this row appears, as ISO weekdays (1=Monday .. 7=Sunday). Empty means every day.",
    )
    pin_top: bool = False  # pin to top of the library's Recommended shelf
    # Per-library Recommended-shelf override for this row, keyed by section key. {} -> inherit the
    # the default, which is the top of the shelf.
    hub_anchor: dict[str, HubAnchorIn] = Field(default_factory=dict)
    poster: PosterIn = Field(default_factory=PosterIn)
    ai_instructions: AiInstructionsIn = Field(default_factory=AiInstructionsIn)
    # The collection's Plex summary and sort title (issue #120). "" leaves that field on Plex alone.
    description: str = Field(
        default="",
        max_length=2000,
        description="The collection's Plex summary; takes {user}, {library_name} and {top_seed}. "
        "Empty leaves the summary on Plex alone.",
    )
    sort_title_prefix: str = Field(
        default="",
        max_length=64,
        description="Put before the row's name to make its Plex sort title, e.g. '!010_'. Orders the row "
        "in the library's Collections tab, not on Home. Empty leaves the sort title alone.",
    )

    @field_validator("description", "sort_title_prefix")
    @classmethod
    def _blank_is_empty(cls, value: str) -> str:
        """Whitespace alone is no value. Anything else is kept verbatim — a prefix's trailing space
        (`01 `) is part of how it sorts."""
        return value if value.strip() else ""

    # A "Your requests" row (issue #127): what each person asked for in Overseerr/Radarr/Sonarr, never
    # the candidate pool. Always per-person, never rewatch, never seasonal — `_validate` says so.
    requests_row: bool = False
    # Keep a request on the row this many days after it lands; 0 = until watched.
    requests_window_days: int = Field(default=90, ge=0, le=3650)
    # How the *arrs tag a person's requests, e.g. "req-{username}"; empty -> only a person's own tag.
    requests_tag_pattern: str = Field(default="", max_length=128)

    # The seasons this row follows (discussion #124); [] -> not seasonal. Out of season the row is hidden,
    # and it shows from `season_lead_days` before each season's day to `season_after_days` after it.
    seasons: list[str] = Field(
        default_factory=list,
        description="Seasons this row follows (see GET /api/seasons). Empty means it is not seasonal.",
    )
    season_lead_days: int = Field(
        default=30,
        ge=0,
        le=seasons_mod.MAX_LEAD_DAYS,
        description="How many days before each season's day the row starts showing.",
    )
    season_after_days: int = Field(
        default=0,
        ge=0,
        le=seasons_mod.MAX_AFTER_DAYS,
        description="How many days after each season's day the row stays up.",
    )

    # An AI row (#138): the theme it is filled from. Set it on create and the row is made disabled, so the
    # owner sees the theme before the first build. Null is an ordinary row.
    theme_id: int | None = Field(
        default=None, description="The theme this AI row follows (see POST /api/themes). Null for an ordinary row."
    )

    # Explore (#138): an AI row that gives each person a new theme every ``theme_days`` days. These and the
    # three controls below exist only on an AI row; every default is today's behaviour.
    theme_mode: Literal["fixed", "explore"] = Field(
        default="fixed", description="fixed keeps one theme; explore picks a new one for each person on a schedule."
    )
    explore_brief: str = Field(
        default="", max_length=500, description="What kind of themes Explore should look for; blank lets the AI choose."
    )
    theme_days: int | None = Field(
        default=None, ge=1, le=90, description="How many days a theme lasts in Explore; null is 7."
    )
    refresh_share: float | None = Field(
        default=None, gt=0, le=1, description="The share of picks swapped on a refresh night; null keeps two thirds."
    )
    repeat_cooldown_days: int | None = Field(
        default=None, ge=1, le=365, description="Don't pick a title again within this many days; null is off."
    )
    avoid_rows: list[str] | None = Field(
        default=None, description="Slugs of other per-person rows whose titles this row keeps out; null is none."
    )

    @field_validator("avoid_rows")
    @classmethod
    def _check_avoid_rows(cls, slugs: list[str] | None) -> list[str] | None:
        return list(dict.fromkeys(slugs)) if slugs else None

    @field_validator("show_days")
    @classmethod
    def _check_show_days(cls, days: list[int]) -> list[int]:
        return _normalise_show_days(days)


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


class PlanEntryOut(PassthroughModel):
    """One unit of Plex work an edit would cause."""

    kind: str = _closed_set_out({RECONCILE, PRIVACY_SYNC, RENAME, POSTER_RESET, VISIBILITY}, "What this step would do.")
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

    mode: str = _closed_set_out(set(INSTRUCTION_MODES), "default, add or own")
    text: str


class CollectionOut(PassthroughModel):
    """A curated-row definition — the response shape of :func:`_serialize`."""

    id: int
    slug: str
    # The DEFAULT row's title is the global template, not its own stale `name` column — see `_serialize`.
    name: str
    last_run_id: int | None  # None until the row has ever built
    #: Up to four titles from the row's most recent delivery, best ranked first. Empty until it has built.
    preview_titles: list[PreviewTitleOut]
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
    # Required, not defaulted (see `_closed_set_out`): every one of these comes from `_serialize`,
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
    pick_order: str = _closed_set_out(ORDERS, "How the delivered collection is ordered.")
    placement: str = _closed_set_out(PLACEMENTS, "Where the OWNER's own collection appears.")
    placement_friends: str = _closed_set_out(PLACEMENTS, "Where each FRIEND's own collection appears.")
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
    ai_paused: bool = Field(description="Whether the row's AI is paused: it keeps its theme but spends no tokens.")
    ai_tokens: int = Field(description="Tokens the AI has spent writing this row's themes.")
    theme_mode: Literal["fixed", "explore"] = Field(description="Whether an AI row keeps one theme or explores.")
    explore_brief: str = Field(description="What Explore is asked to look for; blank lets the AI choose.")
    theme_days: int | None = Field(description="Days a theme lasts in Explore; null is 7.")
    refresh_share: float | None = Field(description="Share of picks swapped on a refresh night; null keeps two thirds.")
    repeat_cooldown_days: int | None = Field(description="No repeats within this many days; null is off.")
    avoid_rows: list[str] | None = Field(description="Slugs of rows whose titles this row keeps out; null is none.")
    # The three keys below exist ONLY on a dry-run PATCH, where the row comes back unchanged and the
    # preview rides alongside it. Optional-with-None is a deliberate exception to `_closed_set_out`'s
    # "declare responses required so a dropped field fails loudly": these are genuinely absent on a
    # live edit, and making them required would force every real save to invent them.
    #: True on a preview. Absent on a live edit, which HAS written by the time it answers.
    dry_run: bool | None = None
    #: What this edit would owe Plex, in the order it would happen. Empty means it owes Plex nothing.
    plan: list[PlanEntryOut] | None = None
    #: Why this preview may UNDER-report, in plain English, or null when it is complete.
    #:
    #: The third state, and the reason it is a field rather than an inference. `_stranded_sections`
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
    #: this one. `_forget_anchor_row` clears these; nothing warned about it before the fact.
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


def _normalise_show_days(days: list[int]) -> list[int]:
    """Sorted, de-duplicated ISO weekdays. Raises ValueError for anything outside 1..7.

    0 is the one to care about: JavaScript's `Date.getDay()` calls Sunday 0, so an untyped client
    would send it and the row would silently never appear on a Sunday — a bug with no error message
    and no visible cause.
    """
    bad = sorted({d for d in days if d < 1 or d > 7})
    if bad:
        raise ValueError(f"show_days must be ISO weekdays 1 (Monday) to 7 (Sunday); got {bad}")
    chosen = sorted(set(days))
    # ALL SEVEN collapses to "every day", so there is ONE stored form for one meaning. Otherwise the
    # midnight job treats the row as scheduled and converges the whole server nightly for a row that is
    # never hidden, and the Rows page badges it as an override of the default it actually is.
    return [] if len(chosen) == 7 else chosen


def _stored_instructions(body: AiInstructionsIn) -> dict[str, str]:
    """What `Collection.prompt` holds: {} for the default, so an untouched row stores exactly what it did."""
    if body.mode == "default":
        return {}
    return {"mode": body.mode, "text": body.text.strip()}


def _ai_instructions_view(stored: object) -> dict[str, str]:
    parsed = AiInstructions.from_stored(stored)
    return {"mode": parsed.mode, "text": parsed.text} if parsed else {"mode": "default", "text": ""}


def _validate(body: CollectionIn) -> None:
    if body.ai_instructions.mode not in INSTRUCTION_MODES:
        raise HTTPException(status_code=422, detail="AI instructions must be default, add or own")
    # A row that names its sources without AI web search has no instructions field on screen to fill in.
    web_search_off = bool(body.candidate_sources) and "llm_web" not in body.candidate_sources
    if body.ai_instructions.mode != "default" and not body.ai_instructions.text.strip() and not web_search_off:
        raise HTTPException(status_code=422, detail="Write the AI instructions, or choose Use the default.")
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
    if body.min_year is not None and body.max_year is not None and body.min_year > body.max_year:
        raise HTTPException(status_code=422, detail="min_year cannot be later than max_year")
    if body.pick_order not in ORDERS:
        raise HTTPException(status_code=422, detail=f"pick_order must be one of {sorted(ORDERS)}")
    if body.cold_start is not None and body.cold_start not in COLD_STARTS:
        raise HTTPException(status_code=422, detail=f"cold_start must be null or one of {sorted(COLD_STARTS)}")
    if body.req_sonarr_monitor is not None and body.req_sonarr_monitor not in SONARR_MONITOR_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"req_sonarr_monitor must be null or one of {sorted(SONARR_MONITOR_MODES)}",
        )
    if body.req_language_mode is not None and body.req_language_mode not in LANGUAGE_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"req_language_mode must be null or one of {sorted(LANGUAGE_MODES)}",
        )
    if body.req_preferred_languages is not None:
        # `[a-z]{2}`, not `.isalpha()` — see `_language_codes` in api/settings.py for why a
        # Unicode-aware check lets a homoglyph through that matches no TMDB language.
        bad = [c for c in body.req_preferred_languages if not re.fullmatch(r"[a-z]{2}", str(c).strip().lower())]
        if bad:
            more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
            raise HTTPException(
                status_code=422,
                # Only the first few, but the rest are COUNTED — otherwise an owner fixes what they
                # were shown and is rejected again for values the message implied were fine.
                detail=(
                    f"req_preferred_languages must be ISO 639-1 codes (two letters, e.g. 'en'); got {bad[:5]}{more}"
                ),
            )
        # Normalised on the way IN as well as on the way out. This is defence in depth, not a guard
        # anything currently depends on: `_serialize` normalises this column on the way out, so the
        # editor never sees a raw value and the run reads through `row_languages_or_inherit` anyway.
        # It is here so the stored value matches what every reader assumes, for anything that reaches
        # the column without going through those — a SQL query, a support bundle, a future consumer.
        body.req_preferred_languages = list(normalise_languages(body.req_preferred_languages))
    if body.placement not in PLACEMENTS:
        raise HTTPException(status_code=422, detail=f"placement must be one of {sorted(PLACEMENTS)}")
    if body.placement_friends not in PLACEMENTS:
        raise HTTPException(status_code=422, detail=f"placement_friends must be one of {sorted(PLACEMENTS)}")
    if body.poster.mode not in POSTER_MODES:
        raise HTTPException(status_code=422, detail=f"poster mode must be one of {sorted(POSTER_MODES)}")
    if body.schedule.strip():
        try:
            crontab_trigger(body.schedule.strip())
        except ValueError as e:
            raise HTTPException(
                status_code=422, detail=f"invalid schedule — needs a 5-field cron (e.g. '30 3 * * *'): {e}"
            ) from e
    for lib, anchor in body.hub_anchor.items():
        if not anchor.enabled:
            continue  # "never position this row" needs no anchor of any kind
        if not anchor.top and not anchor.anchor.strip() and not anchor.row.strip():
            raise HTTPException(
                status_code=422, detail=f"hub_anchor[{lib}]: needs 'top', a 'row' slug, or a non-empty 'anchor'"
            )
        if anchor.row.strip() and anchor.anchor.strip():
            # The engine resolves `row` first, so accepting both would silently ignore one of them and
            # leave the editor showing a placement that is not the one in force.
            raise HTTPException(status_code=422, detail=f"hub_anchor[{lib}]: set either 'row' or 'anchor', not both")
    _validate_pairing(rewatch=body.rewatch, unstarted_only=body.unstarted_only, media=body.media)
    pattern = body.requests_tag_pattern.strip()
    if pattern and "{username}" not in pattern and "{name}" not in pattern:
        raise HTTPException(status_code=422, detail="Tag pattern needs {username} or {name} in it")
    _validate_requests_row(
        requests_row=body.requests_row,
        build=body.build,
        rewatch=body.rewatch,
        seasons=body.seasons,
        has_theme=body.theme_id is not None,
    )


def _validate_requests_row(
    *, requests_row: bool, build: str, rewatch: bool, seasons: list[str], has_theme: bool = False
) -> None:
    """The shapes a "Your requests" row cannot take. A person's requests are theirs alone, so the row is
    always per-person; it holds titles they have NOT seen, so it cannot lead with finished ones; and a
    request lands when it lands, so no season decides whether the row shows.

    Keyword-only like `_validate_pairing`, and for the same reason: a PATCH judges the MERGED row, so
    flipping `rewatch` on a requests row is refused as surely as flipping `requests_row` on a rewatch row.
    """
    if not requests_row:
        return
    if build != "per_person":
        raise HTTPException(status_code=422, detail="A requests row is always one row per person")
    if rewatch:
        raise HTTPException(status_code=422, detail="A requests row can't also be a rewatch row")
    if seasons:
        raise HTTPException(
            status_code=422,
            detail="A requests row can't be seasonal — it shows what they asked for whenever it lands",
        )
    if has_theme:
        raise HTTPException(status_code=422, detail="A requests row can't also be an AI row")


def _validate_anchor_rows(session: Session, body: CollectionIn, editing_slug: str) -> None:
    """Refuse a row anchor that names a row which doesn't exist, itself, or a cycle.

    Checked HERE and not only in the engine because the engine's only sane response to a cycle is to
    drop the placement and fall back to the library default — so the row lands somewhere the owner
    did not choose. It is recorded (`placed: False` in the run's shelf audit) rather than silent, but
    the moment to say "these two rows point at each other" is still while someone is looking at the
    screen that created it.

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


def _anchored_to(entry: object, gone: str) -> bool:
    """Whether one ``hub_anchor`` entry positions its row relative to row ``gone``.

    The single definition of that match, so the DELETE preview's warning and the delete that carries
    it out cannot disagree about which rows lose their placement. Only ``row`` is matched: a
    placement anchored to a foreign collection TITLE names no row and survives the delete.
    """
    return isinstance(entry, dict) and str(entry.get("row") or "").strip() == gone


def _rows_anchored_to(session: Session, gone: str) -> list[str]:
    """Slugs of the rows whose shelf placement is positioned relative to row ``gone`` — read only."""
    return [
        row.slug
        for row in session.query(Collection).all()
        if any(_anchored_to(entry, gone) for entry in (row.hub_anchor or {}).values())
    ]


def _forget_anchor_row(session: Session, gone: str) -> list[str]:
    """Drop every placement that positioned a row relative to ``gone``. Returns the slugs changed.

    Called when a row is deleted. Without it those rows keep pointing at a row that no longer exists,
    and the engine — which skips an anchor it cannot resolve, correctly, because a row may simply not
    have delivered into that library yet — would leave them unplaced every night from then on. That
    is the right response to a TRANSIENT miss and the wrong one to a permanent one, and the
    difference is invisible from inside the run. Clearing the reference falls them back to the
    library default, which is where a row with no placement of its own belongs.
    """
    changed = _rows_anchored_to(session, gone)
    for row in session.query(Collection).filter(Collection.slug.in_(changed)).all():
        row.hub_anchor = {lib: entry for lib, entry in (row.hub_anchor or {}).items() if not _anchored_to(entry, gone)}
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


def _season_window_view(window: seasons_mod.SeasonWindow | None) -> dict | None:
    if window is None:
        return None
    return {
        "slug": window.season.slug,
        "name": window.season.name,
        "emoji": window.season.emoji,
        "starts": window.starts.isoformat(),
        "ends": window.ends.isoformat(),
    }


def _season_status(collection: Collection, now: datetime, *, catalogue: seasons_mod.Catalogue) -> dict | None:
    """Which season a seasonal row shows today and which comes next, on the server's clock; None if not seasonal."""
    if not collection.seasons:
        return None
    args = (list(collection.seasons), collection.season_lead_days, collection.season_after_days, now.date())
    showing = seasons_mod.shown_on(*args, catalogue=catalogue)
    if showing is not None:
        # Its last day on screen, which is not its window's end when a following season takes over first.
        showing = replace(showing, ends=seasons_mod.last_shown_day(*args, catalogue=catalogue))
    upcoming = seasons_mod.next_after(*args, catalogue=catalogue)
    return {"showing": _season_window_view(showing), "next": _season_window_view(upcoming)}


def row_display_name(session: Session, collection: Collection) -> str:
    """What the Rows page calls a row.

    The default row's real title is the global template (Settings → Defaults), which the engine renders per
    library — not its stale seeded `name` column. Surfacing the template shows the actual default
    ("✨ {library_name} Picked for You"), consistent with what delivers.
    """
    if collection.slug == DEFAULT_SLUG:
        return SettingsStore(session).get("row.name_template") or collection.name
    return collection.name


#: How many titles the Rows list's collage shows for a row.
PREVIEW_TITLE_COUNT = 4


def _preview_titles(session: Session, slugs: list[str]) -> dict[str, list[dict]]:
    """Up to four titles from each row's most recent delivery, keyed by slug, for the Rows list.

    Built for every row at once — the list renders them all, so this is two queries, not two per row.
    A per-person row's picks are `picks` rows, written only by real runs; its newest run holds every
    person's picks, so the same title arrives once per person and is kept once, at its best rank. A
    shared row's picks live only in `run_shared_rows`, which dry runs write too, so that read skips dry
    runs, and skips a run that delivered it nothing (Plex still holds the earlier titles then). A pick
    never matched to a library item (`rating_key` 0) has no artwork to show and is left out.

    Args:
        session: An open database session.
        slugs: The rows to read.

    Returns:
        slug -> `{"rating_key", "title"}` dicts, best ranked first. A row that never built is absent.
    """
    latest_per_person = (
        session.query(PickRow.collection_slug, func.max(PickRow.run_id).label("run_id"))
        .filter(PickRow.collection_slug.in_(slugs))
        .group_by(PickRow.collection_slug)
        .subquery()
    )
    best_rank = func.min(PickRow.rank)
    per_person = (
        session.query(PickRow.collection_slug, PickRow.rating_key, func.min(PickRow.title), best_rank)
        .join(
            latest_per_person,
            and_(
                PickRow.collection_slug == latest_per_person.c.collection_slug,
                PickRow.run_id == latest_per_person.c.run_id,
            ),
        )
        .filter(PickRow.rating_key > 0)
        .group_by(PickRow.collection_slug, PickRow.rating_key)
        .order_by(PickRow.collection_slug, best_rank, PickRow.rating_key)
        .all()
    )
    previews: dict[str, list[dict]] = {}
    for slug, rating_key, title, _rank in per_person:
        titles = previews.setdefault(slug, [])
        if len(titles) < PREVIEW_TITLE_COUNT:
            titles.append({"rating_key": rating_key, "title": title})

    latest_shared = (
        session.query(RunSharedRow.collection_slug, func.max(RunSharedRow.run_id).label("run_id"))
        .join(Run, Run.id == RunSharedRow.run_id)
        .filter(
            RunSharedRow.collection_slug.in_(slugs),
            Run.dry_run.is_(False),
            func.json_array_length(RunSharedRow.picks) > 0,
        )
        .group_by(RunSharedRow.collection_slug)
        .subquery()
    )
    shared = (
        session.query(RunSharedRow.collection_slug, RunSharedRow.picks)
        .join(
            latest_shared,
            and_(
                RunSharedRow.collection_slug == latest_shared.c.collection_slug,
                RunSharedRow.run_id == latest_shared.c.run_id,
            ),
        )
        .all()
    )
    for slug, picks in shared:
        titles, seen = [], set()
        for pick in sorted(picks, key=lambda p: p.get("rank") or 0):
            rating_key = pick.get("rating_key") or 0
            if rating_key <= 0 or rating_key in seen:
                continue
            seen.add(rating_key)
            titles.append({"rating_key": rating_key, "title": pick.get("title") or ""})
            if len(titles) == PREVIEW_TITLE_COUNT:
                break
        previews[slug] = titles
    return previews


def _serialize(
    session,
    collection: Collection,
    now: datetime | None = None,
    *,
    catalogue: seasons_mod.Catalogue,
    previews: dict[str, list[dict]] | None = None,
) -> dict:
    """One row as the API renders it.

    Args:
        session: An open database session.
        collection: The row.
        now: The one clock read for the whole response; read here when omitted.
        catalogue: The season catalogue, for the season status.
        previews: `_preview_titles` for a whole list, read once by the caller; read here for this one
            row when omitted.

    Returns:
        The `CollectionOut` payload.
    """
    # One clock read for everything this row reports about today: the badge and the season status must
    # describe the same day, even for a response built across midnight.
    now = now or context_builder.local_now()
    if previews is None:
        previews = _preview_titles(session, [collection.slug])
    audience_ids = [
        row.user_id for row in session.query(CollectionAudience).filter_by(collection_id=collection.id).all()
    ]
    name = row_display_name(session, collection)
    # The most recent run that delivered picks for THIS row — so the Rows UI can link straight to what
    # happened (the run detail groups its results by row). None until the row has ever built.
    last_run_id = session.query(func.max(PickRow.run_id)).filter(PickRow.collection_slug == collection.slug).scalar()
    return {
        "id": collection.id,
        "slug": collection.slug,
        "name": name,
        "last_run_id": last_run_id,
        "preview_titles": previews.get(collection.slug, []),
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
        "description": collection.description or "",
        "sort_title_prefix": collection.sort_title_prefix or "",
        "min_watchers": collection.min_watchers,
        "request_tag": collection.request_tag or "",
        "candidate_sources": list(collection.candidate_sources or []),
        "watched_pct": collection.watched_pct,
        "rewatch": bool(collection.rewatch),
        "rewatch_cooldown_days": collection.rewatch_cooldown_days,
        "requests_row": bool(collection.requests_row),
        "requests_window_days": collection.requests_window_days,
        "requests_tag_pattern": collection.requests_tag_pattern or "",
        "unstarted_only": bool(collection.unstarted_only),
        "refresh_days": collection.refresh_days,
        "idle_hold_days": collection.idle_hold_days,
        "recency": collection.recency,
        "recent_count": collection.recent_count,
        "max_seeds": collection.max_seeds,
        "max_runtime": collection.max_runtime,
        "min_year": collection.min_year,
        "max_year": collection.max_year,
        "min_rating": collection.min_rating,
        "cold_start": collection.cold_start,
        "seed_window": int(collection.seed_window or 1),
        "req_min_rating": collection.req_min_rating,
        "req_min_votes": collection.req_min_votes,
        "req_min_demand": collection.req_min_demand,
        "req_min_year": collection.req_min_year,
        "req_max_year": collection.req_max_year,
        "req_auto_send": collection.req_auto_send,
        "req_auto_min_demand": collection.req_auto_min_demand,
        "req_auto_min_rating": collection.req_auto_min_rating,
        "req_max_per_row": collection.req_max_per_row,
        "req_radarr_quality_profile_id": collection.req_radarr_quality_profile_id,
        "req_radarr_root_folder": collection.req_radarr_root_folder,
        "req_sonarr_quality_profile_id": collection.req_sonarr_quality_profile_id,
        "req_sonarr_root_folder": collection.req_sonarr_root_folder,
        # Not the raw column: a mode this build no longer offers is served as null ("inherits"),
        # which is also what the run does with it. Serving it raw let the editor PATCH it straight
        # back and be refused by the closed-set check, so a row holding a retired mode could not be
        # saved at all — not even renamed.
        "req_sonarr_monitor": row_monitor_or_inherit(collection.req_sonarr_monitor),
        "req_language_mode": row_language_mode_or_inherit(collection.req_language_mode),
        "req_preferred_languages": row_languages_or_inherit(collection.req_preferred_languages),
        "req_min_rating_other": collection.req_min_rating_other,
        "req_auto_user_tag": collection.req_auto_user_tag,
        "pick_order": collection.pick_order or "best",
        "placement": collection.placement or "both",
        "show_days": list(collection.show_days or []),
        # Resolved HERE, on the server's clock — the same one the midnight job and Plex follow. A
        # badge computed in the browser reads the admin's timezone, which can disagree with what
        # Plex is actually showing for as long as the offset lasts.
        "shown_today": row_shown_today(
            collection.show_days,
            collection.seasons,
            collection.season_lead_days,
            collection.season_after_days,
            now,
            catalogue=catalogue,
        ),
        "seasons": list(collection.seasons or []),
        "season_lead_days": collection.season_lead_days,
        "season_after_days": collection.season_after_days,
        "season_status": _season_status(collection, now, catalogue=catalogue),
        "placement_friends": collection.placement_friends or "both",
        "pin_top": bool(collection.pin_top),
        "hub_anchor": collection.hub_anchor or {},
        "library_keys": [str(k) for k in (collection.library_keys or [])],
        "poster": _poster_view(session, collection),
        "ai_instructions": _ai_instructions_view(collection.prompt),
        "theme_id": collection.theme_id,
        "ai_paused": bool(collection.ai_paused),
        "ai_tokens": collection.ai_tokens or 0,
        "theme_mode": collection.theme_mode or "fixed",
        "explore_brief": collection.explore_brief or "",
        "theme_days": collection.theme_days,
        "refresh_share": collection.refresh_share,
        "repeat_cooldown_days": collection.repeat_cooldown_days,
        "avoid_rows": list(collection.avoid_rows) if collection.avoid_rows else None,
    }


def _reject_season_name_without_seasons(template: str, seasons: list[str], *, row_has_theme: bool = False) -> None:
    """Refuse a name that uses the season on a row that follows none, or the theme on a row that is not an AI
    row: it could never be filled in, so the row would never be built for anyone (discussion #124, #138)."""
    if why := refusal(template or "", "row_name", row_has_seasons=bool(seasons), row_has_theme=row_has_theme):
        raise HTTPException(status_code=422, detail=why)


def _validate_theme(
    session: Session,
    theme_id: int | None,
    *,
    build: str,
    seasons: list[str],
    rewatch: bool = False,
    requests_row: bool = False,
) -> Theme | None:
    """The theme an AI row follows, or None for an ordinary row; 422 for a row a theme cannot drive.

    Keyword-only on what the row will be, like `_validate_requests_row`: a PATCH judges the MERGED row.
    AI rows are per-person in v1, and a theme has no calendar, so a season is refused too.
    """
    if theme_id is None:
        return None
    theme = session.get(Theme, theme_id)
    if theme is None:
        raise HTTPException(status_code=422, detail="That theme doesn't exist. Write or pick one first.")
    if build != "per_person":
        raise HTTPException(status_code=422, detail="An AI row is always one row per person, never a shared row.")
    if seasons:
        raise HTTPException(
            status_code=422, detail="An AI row can't also follow seasons — a theme has no calendar of its own."
        )
    if rewatch:
        raise HTTPException(status_code=422, detail="An AI row can't also be a rewatch row")
    if requests_row:
        raise HTTPException(status_code=422, detail="An AI row can't also be a requests row")
    return theme


_EXPLORE_COLUMNS = ("theme_mode", "explore_brief", "theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows")
_EXPLORE_DEFAULTS = {
    "theme_mode": "fixed",
    "explore_brief": "",
    "theme_days": None,
    "refresh_share": None,
    "repeat_cooldown_days": None,
    "avoid_rows": None,
}


def _validate_explore(
    session: Session, values: dict, *, theme_id: int | None, own_slug: str, only: set[str] | None = None
) -> None:
    """422 for Explore settings or over-time controls a row cannot use (#138).

    ``values`` is the merged row. They exist only on an AI row, so each needs a theme; ``only`` limits the
    "needs a theme" check to the fields a PATCH actually sent. ``avoid_rows`` must name other, existing
    per-person rows.
    """
    if theme_id is None:
        stray = [c for c in (only if only is not None else _EXPLORE_COLUMNS) if values[c] != _EXPLORE_DEFAULTS[c]]
        if stray:
            raise HTTPException(
                status_code=422,
                detail="Explore and the over-time controls only apply to an AI row. Give the row a theme.",
            )
    for slug in values["avoid_rows"] or []:
        if slug == own_slug:
            raise HTTPException(status_code=422, detail=f"A row can't keep out its own titles (“{slug}”).")
        other = session.query(Collection).filter(Collection.slug == slug).first()
        if other is None:
            raise HTTPException(status_code=422, detail=f"There is no row “{slug}” to keep out.")
        if other.build != "per_person":
            raise HTTPException(
                status_code=422, detail=f"“{slug}” is a shared row. Only per-person rows can be kept out."
            )


def _unattributed_theme_tokens(session: Session, theme: Theme | None, *, exclude_id: int | None) -> int:
    """The tokens a theme cost that no row has been charged for yet: all of them while no other row follows it.

    A new row's list is saved before the row exists, so those tokens had no row to land on. Computed, not
    stored: a theme another row already follows has had its tokens counted by that row, and counting them
    again would double them.
    """
    if theme is None or not theme.ai_tokens:
        return 0
    others = session.query(Collection.id).filter(Collection.theme_id == theme.id)
    if exclude_id is not None:
        others = others.filter(Collection.id != exclude_id)
    return 0 if others.first() else int(theme.ai_tokens)


def _reject_duplicate_name(
    session,
    secrets,
    template: str,
    *,
    exclude_slug: str = "",
    build: str = "",
    fallback_name: str = "",
    media: str = "both",
    library_keys=(),
    already_clashing: frozenset[str] = frozenset(),
    theme: ThemeSpec | None = None,
) -> None:
    """Refuse a row title another row is already titled from — see `reconcile.row_titled_from` for
    what "already titled from" means and why the `name` column is the wrong thing to compare.

    ``template`` is the EFFECTIVE template being proposed (`name_template or name`), not the raw name:
    a row that carries its own template is titled from that, so changing only its `name` cannot clash
    with anything, and changing only its `name_template` very much can.

    ``media``/``library_keys`` are where the row builds: a title only has to be unique among rows that
    could build in one library with it (issue #121). ``already_clashing`` holds the slugs this row
    clashed with BEFORE the edit, which are not refused again — the rule is "no NEW clashes".
    """
    clash = next(
        (
            row
            for row in reconcile.rows_titled_from(
                session,
                template,
                secrets=secrets,
                exclude_slug=exclude_slug,
                build=build,
                fallback_name=fallback_name,
                media=media,
                library_keys=library_keys,
                theme=theme,
            )
            if row.slug not in already_clashing
        ),
        None,
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
    # Name the FIELD that collided, not just the row. A fallback clash used to be reported as
    # "'More like {top_seed}' is already the title of …" — quoting the row name, which is fine, and
    # sending the operator to the box that isn't the problem.
    culprit = template
    where = "name"
    if fallback_name and reconcile.title_key(fallback_name) in reconcile._title_keys(
        session, clash, secrets, catalogue=load_catalogue(session)
    ):
        culprit = fallback_name
        where = "\u201cName for people with nothing watched yet\u201d"
    raise HTTPException(
        status_code=422,
        detail=f"{culprit!r} is already the title of {whose}, which can build in the same library — two rows "
        f"with the same title in one library become a single collection on Plex, so pick a different {where} "
        "or build the two rows in different libraries",
    )


def _unique_slug(session, base: str) -> str:
    """A slug no row has now AND no history still names.

    The slug is a row's identity in every history table, and deleting a row frees it in `collections`
    alone. A new row that took it over inherited the deleted row's last picks (redelivered as "not due
    to rebuild"), its delivery ledger, its shared-row picks and watch credits, and its queued requests —
    seen live on 2026-09-13. A delivered row's history is kept (run pruning leaves picks, deliveries and
    watch credits alone), so in practice its slug stays reserved for good.

    A pending `row.reconcile` for the slug counts too. DELETE queues it before dropping the row, and it
    cannot start while a run is in flight — a run that still holds the old row, and persists its picks
    under the slug as each person finishes. When the job does start it removes by the slug's ledger
    keys, which would by then be the NEW row's collections.

    The default row's slug is never handed out: `picked` makes a row the default one everywhere,
    titled from the global template and credited with legacy picks stored under a blank slug.
    """
    base = base if base not in RESERVED_SLUGS | {DEFAULT_SLUG} else f"{base}_row"
    columns = (
        Collection.slug,
        PickRow.collection_slug,
        Delivery.collection_slug,
        RunSharedRow.collection_slug,
        SharedRowWatch.collection_slug,
        RequestCandidate.row_slug,
    )

    def is_taken(slug: str) -> bool:
        if any(session.query(column).filter(column == slug).first() is not None for column in columns):
            return True
        pending_removal = session.query(Job.id).filter(
            Job.kind == "row.reconcile",
            Job.status.in_(("queued", "running")),
            func.json_extract(Job.payload, "$.slug") == slug,
        )
        return pending_removal.first() is not None

    return dedupe_slug(base, is_taken)


def _set_audience(session, collection: Collection, body: CollectionIn) -> None:
    """Replace this row's audience with the requested user ids, refusing any that don't exist.

    The ids are RESOLVED first, deliberately. `CollectionAudience.user_id` is a foreign key and the
    connection runs with `PRAGMA foreign_keys=ON`, so an unknown id used to surface as an
    `IntegrityError` at commit — an unhandled 500 carrying a SQL string, where every other bad input
    on this router is a 422. And on a SHARED row this list is what decides who is excluded from the
    share filter, so silently dropping an id nobody recognises is the wrong direction to fail in.
    """
    session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
    _validate_audience_ids(session, body)
    for user_id in _audience_after_set(body):
        session.add(CollectionAudience(collection_id=collection.id, user_id=user_id))


def _audience_after_set(body: CollectionIn) -> list[int]:
    """The membership :func:`_set_audience` leaves behind, deduped and in request order.

    Gated on the RAW ``body.audience``, NOT the row's merged value — and that distinction is the
    whole reason this is a function. `CollectionIn.audience` defaults to "everyone", so a PATCH that
    sends ``audience_user_ids`` ALONE clears the membership of a row that stays "subset": everyone
    is dropped, not just the ids left out. A preview that reasoned from the merged audience instead
    reported one person's collection going while the save removed all forty.
    """
    if body.audience != "subset":
        return []
    return list(dict.fromkeys(body.audience_user_ids))  # dedupe, keep order


def _validate_audience_ids(session, body: CollectionIn) -> None:
    """Refuse an audience naming a user who does not exist.

    Hoisted out of `_set_audience` so the PATCH handler can run it BEFORE its first write. It used to
    fire from inside the apply half, after `SettingsStore.set` had already committed — so a
    default-row rename refused for an unknown id answered 422 while every row on the server had been
    permanently retitled.

    Raises:
        HTTPException: 422 naming the unknown ids. `CollectionAudience.user_id` is a foreign key and
            the connection runs with ``PRAGMA foreign_keys=ON``, so an unknown id otherwise surfaced
            as an `IntegrityError` at commit — an unhandled 500 carrying a SQL string, where every
            other bad input on this router is a 422. And on a SHARED row this list decides who is
            excluded from the share filter, so silently dropping an unrecognised id is the wrong
            direction to fail in.
    """
    wanted = _audience_after_set(body)
    if not wanted:
        return
    known = {user_id for (user_id,) in session.query(User.id).filter(User.id.in_(wanted)).all()}
    if unknown := [user_id for user_id in wanted if user_id not in known]:
        raise HTTPException(status_code=422, detail=f"no such user(s): {unknown} — the audience must be existing users")


@router.get("", response_model=list[CollectionOut])
async def list_collections(request: Request) -> list[dict]:
    with request.app.state.sessions() as session:
        collections = session.query(Collection).order_by(Collection.sort_order, Collection.id).all()
        # ONE clock read for the whole response, the same rule `_build_rows` follows: served a
        # millisecond either side of midnight, two rows in one list would otherwise report different
        # days.
        now = context_builder.local_now()
        catalogue = load_catalogue(session)
        previews = _preview_titles(session, [c.slug for c in collections])
        return [_serialize(session, c, now, catalogue=catalogue, previews=previews) for c in collections]


def _known_seasons(slugs: list[str], *, catalogue: seasons_mod.Catalogue) -> list[str]:
    """Known seasons only, de-duplicated and in calendar order, so equal choices compare equal.

    In the handlers rather than a field validator on ``CollectionIn``: the catalogue holds the owner's own
    seasons (issue #137), which live in the database, and a validator has no session to read them with.
    """
    try:
        return seasons_mod.normalise_slugs(slugs, catalogue=catalogue)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


@router.post("", status_code=201, response_model=CollectionOut)
async def create_collection(body: CollectionIn, request: Request) -> dict:
    # `CollectionIn` is the body model for PATCH as well, which is where `dry_run` belongs — but that
    # makes it part of the POST schema too, and creation has nothing to preview. Silently ignoring it
    # would mean `POST {"dry_run": true}` answers 201 having created the row: a documented preview
    # flag that writes, which is the exact shape plex-safety rule 8 exists to prevent.
    if body.dry_run:
        raise HTTPException(status_code=422, detail="dry_run is only supported on PATCH and DELETE")
    _validate(body)
    _reject_season_name_without_seasons(
        body.name_template or body.name, body.seasons, row_has_theme=body.theme_id is not None
    )
    with request.app.state.sessions() as session:
        catalogue = load_catalogue(session)
        body.seasons = _known_seasons(body.seasons, catalogue=catalogue)
        theme = _validate_theme(
            session,
            body.theme_id,
            build=body.build,
            seasons=body.seasons,
            rewatch=body.rewatch,
            requests_row=body.requests_row,
        )
        _validate_explore(
            session,
            {c: getattr(body, c) for c in _EXPLORE_COLUMNS},
            theme_id=None if theme is None else theme.id,
            own_slug="",
        )
        # The template this row will actually be titled from, not the bare name — a POST may set both. An AI
        # row's `{theme}` is filled from its theme by the check itself.
        template = body.name_template or body.name
        _reject_duplicate_name(
            session,
            request.app.state.secrets,
            template,
            build=body.build,
            fallback_name=body.fallback_name,
            media=body.media,
            library_keys=body.library_keys,
            theme=None if theme is None else spec_from_row(theme),
        )
        _validate_anchor_rows(session, body, editing_slug="")
        slug = _unique_slug(session, slugify(body.name))
        collection = Collection(
            slug=slug,
            name=body.name,
            build=body.build,
            audience=body.audience,
            # An AI row starts disabled whatever was asked: nothing is built, or spent, until the owner has seen it.
            enabled=body.enabled and theme is None,
            theme_id=None if theme is None else theme.id,
            **{column: getattr(body, column) for column in _EXPLORE_COLUMNS},
            schedule=body.schedule.strip(),
            size=body.size,
            media=body.media,
            sort_order=body.sort_order,
            name_template=body.name_template,
            # Verbatim, NOT `or None`: "" is a real answer ("no fallback — skip those people"), and
            # storing it as NULL makes it indistinguishable from "never asked", which migration 0070
            # then overwrites on a replay.
            fallback_name=body.fallback_name,
            min_watchers=body.min_watchers,
            request_tag=body.request_tag.strip(),
            candidate_sources=body.candidate_sources,
            watched_pct=body.watched_pct,
            rewatch=body.rewatch,
            rewatch_cooldown_days=body.rewatch_cooldown_days,
            requests_row=body.requests_row,
            requests_window_days=body.requests_window_days,
            requests_tag_pattern=body.requests_tag_pattern.strip(),
            unstarted_only=body.unstarted_only,
            refresh_days=body.refresh_days,
            idle_hold_days=body.idle_hold_days,
            recency=body.recency,
            recent_count=body.recent_count,
            max_seeds=body.max_seeds,
            max_runtime=body.max_runtime,
            min_year=body.min_year,
            max_year=body.max_year,
            min_rating=body.min_rating,
            cold_start=body.cold_start,
            seed_window=body.seed_window,
            pick_order=body.pick_order,
            placement=body.placement,
            show_days=body.show_days,
            seasons=body.seasons,
            season_lead_days=body.season_lead_days,
            season_after_days=body.season_after_days,
            placement_friends=body.placement_friends,
            pin_top=body.pin_top,
            hub_anchor={k: v.model_dump() for k, v in body.hub_anchor.items()},
            library_keys=body.library_keys,
            poster=body.poster.model_dump(),
            prompt=_stored_instructions(body.ai_instructions),
            description=body.description,
            sort_title_prefix=body.sort_title_prefix,
            **{column: getattr(body, column) for column in _REQUEST_COLUMNS},
        )
        collection.ai_tokens = _unattributed_theme_tokens(session, theme, exclude_id=None)
        session.add(collection)
        session.flush()
        _set_audience(session, collection, body)
        session.commit()
        result = _serialize(session, collection, catalogue=catalogue)
    rebuild_schedule(request.app)  # a new row may carry a schedule — register its cron job now
    return result


#: This row's own request floors and Arr target; null on any of them means inherit the global. Named once
#: for create and edit alike: the create constructor listed columns by hand and missed every one of these.
_REQUEST_COLUMNS = (
    "req_min_rating",
    "req_min_votes",
    "req_min_demand",
    "req_min_year",
    "req_max_year",
    "req_auto_send",
    "req_auto_min_demand",
    "req_auto_min_rating",
    "req_max_per_row",
    "req_radarr_quality_profile_id",
    "req_radarr_root_folder",
    "req_sonarr_quality_profile_id",
    "req_sonarr_root_folder",
    "req_sonarr_monitor",
    "req_language_mode",
    "req_preferred_languages",
    "req_min_rating_other",
    "req_auto_user_tag",
)

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
    # Reach Plex on the row's next run, like its poster — they owe Plex nothing at save time.
    "description",
    "sort_title_prefix",
    "min_watchers",
    "request_tag",
    "candidate_sources",
    "watched_pct",
    "rewatch",
    "rewatch_cooldown_days",
    "requests_row",
    "requests_window_days",
    "requests_tag_pattern",
    "unstarted_only",
    "refresh_days",
    "idle_hold_days",
    "recency",
    "recent_count",
    "max_seeds",
    "max_runtime",
    "min_year",
    "max_year",
    "min_rating",
    "cold_start",
    "seed_window",
    *_REQUEST_COLUMNS,
    "pick_order",
    "placement",
    "placement_friends",
    "show_days",
    "seasons",
    "season_lead_days",
    "season_after_days",
    "pin_top",
    "library_keys",
    "theme_id",
    *_EXPLORE_COLUMNS,
)


def _stranded_sections(
    state,
    *,
    old_media: str,
    old_keys: list[str],
    new_media: str,
    new_keys: list[str],
    unreadable: list[str] | None = None,
) -> set[str]:
    """Section keys this row USED to deliver into and no longer does.

    Narrowing a row is not the same as removing it: the collections in the libraries it still targets
    are live. So the set is a difference, computed with the engine's own `target_sections` so it can
    never drift from where delivery actually writes.

    Empty when the row widened, when nothing moved, or when Plex cannot be reached — the last of those
    deliberately: not knowing which libraries exist must mean "delete nothing", never "delete
    everything". The next edit or a sync check picks it up.

    Args:
        unreadable: Out-param for the THIRD state, and the only reason it exists. An empty result
            means "nothing was stranded" to a live edit and "I could not find out" to a preview, and
            the two are indistinguishable from the return value alone — so a caller that needs to
            tell them apart passes a list here and gets the plain-English reason appended. Live
            callers pass nothing and behave exactly as before. Same out-param idiom as
            `_reconcile_row_removal`'s ``removed``.
    """
    if (old_media, sorted(old_keys)) == (new_media, sorted(new_keys)):
        return set()
    try:
        sections = state.run_service.build_context(dry_run=True).plex.sections()
    except Exception as e:
        logger.warning("could not read libraries to narrow row scope ({}) — nothing removed", type(e).__name__)
        if unreadable is not None:
            unreadable.append(
                "Plex could not be reached, so which libraries this row would leave is unknown — "
                "it may remove collections this preview does not list. Check the connection and preview again."
            )
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


def _apply_patch(
    session,
    secrets,
    collection: Collection,
    body: CollectionIn,
    sent: set[str],
    *,
    is_default: bool,
    default_rename_to: str,
) -> None:
    """Write a validated PATCH onto the row. Every write the handler makes, and nothing else.

    Split out so the handler reads validate → (preview and stop) → apply, and so the one write that a
    transaction cannot take back — `SettingsStore.set`, which commits inside itself — sits on this
    side of that line rather than up among the checks.

    Args:
        session: Open session; the caller commits.
        secrets: Secret store, for `SettingsStore`.
        collection: The row to write onto.
        body: The validated edit.
        sent: ``body.model_fields_set`` — only these fields move.
        is_default: Whether this is the DEFAULT row, whose title is a global setting.
        default_rename_to: The new global ``row.name_template`` a default-row rename resolved to,
            or "" for every other edit. Resolved by the caller before the preview branch.
    """
    if default_rename_to:
        SettingsStore(session, secrets).set("row.name_template", default_rename_to)
    elif "name" in sent and not is_default:
        collection.name = body.name
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
    if "theme_id" in sent and body.theme_id is None:
        # Without a theme the row is no longer an AI row, and these exist only on one.
        for column, default in _EXPLORE_DEFAULTS.items():
            setattr(collection, column, default)
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
    if "ai_instructions" in sent:
        collection.prompt = _stored_instructions(body.ai_instructions)
        session.add(
            Event(
                scope="collection.ai_instructions",
                level="info",
                message={
                    "slug": collection.slug,
                    "mode": body.ai_instructions.mode,
                    "chars": len(body.ai_instructions.text.strip()),
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        )
    if "hub_anchor" in sent:
        collection.hub_anchor = {k: v.model_dump() for k, v in body.hub_anchor.items()}
    if sent & {"audience", "audience_user_ids"}:
        _set_audience(session, collection, body)


def _merged_template(collection: Collection, body: CollectionIn, sent: set[str]) -> str:
    """This row's effective title template once the PATCH lands.

    Delivery renders from ``name_template or name``, and a PATCH may send either half — so both are
    taken from the request when the request sent them and off the row when it did not. One
    definition because the duplicate-title check and the rename plan must agree on the title: they
    used to compute it separately, and a check that disagrees with the rename is a check of nothing.
    """
    return (body.name_template if "name_template" in sent else collection.name_template) or (
        body.name if "name" in sent else collection.name
    )


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
        catalogue = load_catalogue(session)
        if "seasons" in sent:
            body.seasons = _known_seasons(body.seasons, catalogue=catalogue)
        _validate_anchor_rows(session, body, editing_slug=collection.slug)
        before = _snapshot(session, collection)
        is_default = collection.slug == DEFAULT_SLUG
        merged_theme_id = body.theme_id if "theme_id" in sent else collection.theme_id
        merged_seasons = body.seasons if "seasons" in sent else list(collection.seasons or [])
        theme = None
        if is_default and body.theme_id is not None:
            raise HTTPException(
                status_code=422, detail="The default row can't be an AI row — add a new row from the AI template."
            )
        if sent & {"theme_id", "build", "seasons", "rewatch", "requests_row"}:
            theme = _validate_theme(
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
        if sent & (set(_EXPLORE_COLUMNS) | {"theme_id"}):
            # Judged on the merged row. Clearing the theme resets these (`_apply_patch`), so only what the
            # request itself sent counts against a row that has none.
            _validate_explore(
                session,
                {c: getattr(body, c) if c in sent else getattr(collection, c) for c in _EXPLORE_COLUMNS},
                theme_id=merged_theme_id,
                own_slug=collection.slug,
                only=sent & set(_EXPLORE_COLUMNS),
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
                _reject_season_name_without_seasons(body.name, [])
        elif sent & {"name", "name_template", "seasons", "theme_id"}:
            # Merged, like the title checks below: a PATCH that sends only the seasons, or only the name,
            # is judged against what the row will be once it lands.
            _reject_season_name_without_seasons(
                _merged_template(collection, body, sent), merged_seasons, row_has_theme=merged_theme_id is not None
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
                    theme=reconcile._theme_of(session, collection),
                )
            )
            _reject_duplicate_name(
                session,
                state.secrets,
                title_now if is_default else _merged_template(collection, body, sent),
                exclude_slug=collection.slug,
                build=merged_build,
                fallback_name=(body.fallback_name if "fallback_name" in sent else fallback_now) or "",
                media=merged_media,
                library_keys=merged_keys,
                already_clashing=before_clashes,
                theme=merged_spec,
            )
        # The clash check runs on the MERGED effective template, for the same reason `_validate_pairing`
        # does: a PATCH may send either half. Sending `name_template` ALONE changes the title and used
        # to be checked by nothing at all, while sending `name` alone on a row that carries its own
        # template changes no title and was checked as though it did.
        # `fallback_name` is checked for the DEFAULT row too. Its `name`/`name_template` are exempt
        # because its title is the global setting (handled below), but its fallback IS a per-row
        # column, and the new "no name for newcomers" alert points operators straight at that field —
        # so it was the one write on the row editor with no duplicate-title check behind it. Two rows
        # rendering one title for one person in one library share a single Plex collection.
        if (sent & {"fallback_name"}) or (not is_default and sent & {"name", "name_template"}):
            merged = _merged_template(collection, body, sent)
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
                collection.name_template or collection.name, reconcile._theme_of(session, collection)
            )
            moved = reconcile.title_key(fill_theme(merged, merged_spec)) != reconcile.title_key(title_before)
            fallback_moved = reconcile.title_key(merged_fallback) != reconcile.title_key(collection.fallback_name or "")
            # Only when the TITLE actually moves. The editor re-sends `name` on every save, so
            # checking on "was the field present" refused a size-only edit on a row that already
            # clashes — with a message about names, for a change that was not about names. A row in
            # that state (created before this guard, or restored from a backup) must stay editable;
            # the rule is "no NEW clashes", not "no clashing row may be touched".
            if moved or fallback_moved:
                _reject_duplicate_name(
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
            seasonal_template = _merged_template(collection, body, sent)
            for slug in ticked if uses_season(seasonal_template) else []:
                _reject_duplicate_name(
                    session,
                    state.secrets,
                    reconcile.season_title(seasonal_template, catalogue[slug]),
                    exclude_slug=collection.slug,
                    build=merged_build,
                    media=merged_media,
                    library_keys=merged_keys,
                    theme=merged_spec,
                )
        if theme is not None and "theme_id" in sent and theme.id != collection.theme_id:
            collection.ai_tokens = (collection.ai_tokens or 0) + _unattributed_theme_tokens(
                session, theme, exclude_id=collection.id
            )
        # A theme newly set gives a `{theme}` row a title it never wore, as a ticked season does.
        if theme is not None and "theme_id" in sent and theme.id != collection.theme_id and not is_default:
            themed_template = _merged_template(collection, body, sent)
            if uses_theme(themed_template):
                _reject_duplicate_name(
                    session,
                    state.secrets,
                    themed_template,
                    exclude_slug=collection.slug,
                    build=merged_build,
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
        default_rename_to = ""
        if "name" in sent and is_default:
            new_template = body.name.strip()
            previous = SettingsStore(session, state.secrets).get("row.name_template") or ""
            if new_template and new_template != previous:
                # Renaming the default row retitles it on Plex just as surely as renaming any other,
                # so it owes the same clash check — onto the title EVERY other row already renders.
                _reject_duplicate_name(
                    session,
                    state.secrets,
                    new_template,
                    exclude_slug=DEFAULT_SLUG,
                    build="per_person",
                    media=merged_media,
                    library_keys=merged_keys,
                )
                default_rename_to = new_template
                template_before, template_after = previous, new_template
        merged_min_year = body.min_year if "min_year" in sent else collection.min_year
        merged_max_year = body.max_year if "max_year" in sent else collection.max_year
        if merged_min_year is not None and merged_max_year is not None and merged_min_year > merged_max_year:
            raise HTTPException(status_code=422, detail="The earliest year can't be later than the latest year.")
        # Checked against the MERGED row, never the request body — see `_validate_pairing`.
        _validate_pairing(
            rewatch=body.rewatch if "rewatch" in sent else bool(collection.rewatch),
            unstarted_only=body.unstarted_only if "unstarted_only" in sent else bool(collection.unstarted_only),
            media=body.media if "media" in sent else collection.media,
        )
        _validate_requests_row(
            requests_row=body.requests_row if "requests_row" in sent else bool(collection.requests_row),
            build=body.build if "build" in sent else collection.build,
            rewatch=body.rewatch if "rewatch" in sent else bool(collection.rewatch),
            seasons=body.seasons if "seasons" in sent else list(collection.seasons or []),
            has_theme=merged_theme_id is not None,
        )
        # Hoisted above the writes: `_set_audience` raises this from inside the apply half, which on a
        # default-row rename meant answering 422 after `SettingsStore.set` had already committed.
        if sent & {"audience", "audience_user_ids"}:
            _validate_audience_ids(session, body)

        # Everything above VALIDATES; everything below WRITES. A preview leaves between the two, so it
        # is refused by exactly what would refuse the save and has still written nothing.
        if body.dry_run:
            if touching_name:
                template_after = _merged_template(collection, body, sent)
            preview_change = _row_change(
                before,
                _projected_snapshot(session, collection, body, sent),
                template_before=template_before,
                template_after=template_after,
                defer_rename=body.defer_rename,
            )
            preview_row = _serialize(session, collection, catalogue=catalogue)
        else:
            _apply_patch(
                session,
                state.secrets,
                collection,
                body,
                sent,
                is_default=is_default,
                default_rename_to=default_rename_to,
            )
            session.commit()
            after = _snapshot(session, collection)
            if touching_name:
                template_after = collection.name_template or collection.name
            result = _serialize(session, collection, catalogue=catalogue)

    if body.dry_run:
        warnings: list[str] = []

        def preview_stranded() -> set[str]:
            return _stranded_sections(
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

    # A schedule or enable/disable change alters which cron jobs should exist — re-derive them.
    if sent & {"schedule", "enabled"}:
        rebuild_schedule(request.app)

    change = _row_change(
        before,
        after,
        template_before=template_before,
        template_after=template_after,
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
        "show_days": tuple(collection.show_days or []),
        "calendar": (
            tuple(collection.seasons or []),
            collection.season_lead_days,
            collection.season_after_days,
        ),
    }


def _projected_snapshot(session, collection: Collection, body: CollectionIn, sent: set[str]) -> dict:
    """What :func:`_snapshot` WOULD return after this PATCH, computed without touching the row.

    The dry-run counterpart to `_snapshot`, and deliberately NOT "apply it and roll back": the
    handler's default-row rename goes through `SettingsStore.set`, which commits inside itself, so a
    rollback would not undo it — a preview of one row's rename would permanently retitle every row on
    the server.

    Every field here is one `_snapshot` reads, resolved the way the apply path resolves it. A
    projection that drifts is a preview that lies about a deletion, which is worse than no preview at
    all, so two tests pin it: `test_a_dry_run_projects_exactly_what_the_real_patch_produces` runs both
    over the matrix `plan_row_changes` branches on, and
    `test_a_dry_run_reports_the_wipe_that_ids_without_an_audience_actually_perform` covers the
    audience branch that matrix CANNOT reach — every row in it is `audience="everyone"`, which
    resolves to the whole roster on both sides and so agrees trivially.

    Args:
        session: Open session, for resolving the audience against the roster.
        collection: The row as it stands, unmodified.
        body: The requested edit.
        sent: ``body.model_fields_set`` — a PATCH only moves the fields it actually sent.

    Returns:
        The same shape `_snapshot` returns.

    The caller must have run `_validate_audience_ids` first; this projects a VALID edit and does not
    re-check one.
    """
    before = _snapshot(session, collection)
    # The row's `audience` column takes the request's value only when the request sent it...
    audience_kind = body.audience if "audience" in sent else collection.audience
    if audience_kind == "everyone":
        # ...and "everyone" ignores the membership table entirely, resolving to whoever is on the
        # roster right now — which is what makes a row switched to everyone drop nobody.
        audience = frozenset(user_id for (user_id,) in session.query(User.id).all())
    elif sent & {"audience", "audience_user_ids"}:
        # ...but the MEMBERSHIP is rewritten by `_set_audience`, which gates on the RAW body value.
        # Deferring to `_audience_after_set` is what keeps the two from disagreeing: read from the
        # merged audience instead and a PATCH sending only `audience_user_ids` previews one removal
        # where the save performs one per person on the server.
        audience = frozenset(_audience_after_set(body))
    else:
        audience = before["audience"]
    return {
        "slug": collection.slug,
        "build": body.build if "build" in sent else before["build"],
        "enabled": bool(body.enabled) if "enabled" in sent else before["enabled"],
        "media": body.media if "media" in sent else before["media"],
        "libraries": tuple(str(k) for k in body.library_keys) if "library_keys" in sent else before["libraries"],
        "audience": audience,
        "poster_mode": (body.poster.mode or "") if "poster" in sent else before["poster_mode"],
        "show_days": tuple(body.show_days) if "show_days" in sent else before["show_days"],
        "calendar": (
            tuple(body.seasons) if "seasons" in sent else before["calendar"][0],
            body.season_lead_days if "season_lead_days" in sent else before["calendar"][1],
            body.season_after_days if "season_after_days" in sent else before["calendar"][2],
        ),
    }


def _row_change(
    before: dict, after: dict, *, template_before: str, template_after: str, defer_rename: bool
) -> RowChange:
    """The before/after pair the planner reads, built the same way for a save and for a preview.

    One construction site on purpose: the two paths differ only in how ``after`` was obtained, and a
    second copy of this mapping is a place for a preview to describe a different edit from the one
    the save performs.
    """
    return RowChange(
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
        days_before=before["show_days"],
        days_after=after["show_days"],
        calendar_before=before["calendar"],
        calendar_after=after["calendar"],
        defer_rename=defer_rename,
    )


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
    the old titles and removes the new ones. Both find the same collections — `_reconcile_row_removal`
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
        elif work.kind == VISIBILITY:
            # The handler recomputes today's answer and keeps no state; queued with the row, it promotes that
            # row alone (after the server-wide filter merge). A row whose day turned over while Plex was
            # unreachable is the midnight job's, which is durable and retried. So is this one: an outage
            # right now is retried rather than lost.
            # Names the row: when its days are CLEARED and no other row on the server carries a
            # schedule, the job's gate would otherwise see nothing to do and skip the very pass that
            # puts this row back.
            jobs.enqueue(state.sessions, "rows.visibility", {"row": slug})
    # Anything queued above happens NOW when Plex is reachable; when it isn't, the worker retries it.
    # AWAITED, unlike the delete below. An edit's Plex work IS the request: narrowing a row's
    # libraries means "take it off those libraries", so returning 200 before that happened would
    # report a change that has not been made. A delete is different — the row is gone from the DB,
    # which is what the page is waiting to hear, and the cleanup is bookkeeping after the fact.
    await jobs.drain_now(state, f"row '{slug}' was edited")


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
        if dry_run:
            # The same walk `_forget_anchor_row` does, minus the write — one shared predicate, so the
            # warning and the delete that carries it out cannot disagree.
            anchors = _rows_anchored_to(session, slug)
            has_schedule = bool((collection.schedule or "").strip())
            # Resolved, not read off the column: the DEFAULT row's `name` is stale seed data and its
            # real title is the global template — `_serialize` substitutes it for exactly this
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

    # Queue the Plex removal FIRST, then drop the DB row. Draining after the delete is deliberate:
    # `is_default` aside, the removal no longer needs the row to exist, and queueing means a Plex outage
    # right now leaves a retried job rather than orphaned collections nothing will ever revisit.
    _queue_reconcile(state, slug=slug, build=build, scope="collection.delete", template=template)
    with state.sessions() as session:
        collection = session.get(Collection, collection_id)
        if collection is not None:
            session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
            # Keyed by id, and SQLite hands a freed highest id to the next row — which would otherwise
            # serve, and could push to Plex, this row's artwork.
            poster_service.clear_assets(session, collection.id)
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
            _reject_season_name_without_seasons(
                new_template,
                [] if slug == DEFAULT_SLUG else list(collection.seasons or []),
                row_has_theme=collection.theme_id is not None,
            )
            _reject_duplicate_name(
                session,
                request.app.state.secrets,
                new_template,
                exclude_slug=slug,
                build=build,
                media=collection.media,
                library_keys=collection.library_keys or [],
                theme=reconcile._theme_of(session, collection),
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
        if collection.theme_id is None:
            raise HTTPException(status_code=422, detail="Only an AI row has AI to pause.")
        if collection.ai_paused != body.paused:
            collection.ai_paused = body.paused
            add_audit(
                session,
                "collection.ai_pause",
                "info",
                slug=collection.slug,
                paused=body.paused,
            )
        session.commit()
        return _serialize(session, collection, catalogue=load_catalogue(session))


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
        "started_at": row.started_at.isoformat(),
        "due_at": None if row.due_at is None else row.due_at.isoformat(),
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
                    "started_at": None if current is None else current.started_at.isoformat(),
                    "next_due_at": None if current is None else (current.started_at + timedelta(days=days)).isoformat(),
                    "history": [_theme_ref(r, themes) for r in rows if r.state == "past"][:_HISTORY_SHOWN],
                }
            )
        return {"mode": collection.theme_mode or "fixed", "days": days, "targets": targets}


@router.put("/{collection_id}/up-next", response_model=ThemeRefOut)
async def set_up_next(collection_id: int, body: UpNextRequest, request: Request) -> dict:
    """Point a person's "Up next" at a saved theme, replacing any theme already queued. Changes no Plex state."""
    from shortlist.server.services.theme_rotation import queue_next
    from shortlist.server.services.theme_store import TitleClash

    with request.app.state.sessions() as session:
        collection = _ai_row(session, collection_id)
        if collection.theme_mode != "explore":
            raise HTTPException(status_code=422, detail="Turn on Explore for this row first.")
        person = _audience_person(session, collection, body.user_id)
        theme = session.get(Theme, body.theme_id)
        if theme is None:
            raise HTTPException(status_code=404, detail="theme not found")
        try:
            queued = queue_next(
                session, collection, person.id, theme, datetime.now(UTC), secrets=request.app.state.secrets
            )
        except TitleClash as e:
            raise HTTPException(status_code=422, detail=str(e)) from None
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


@router.post("/{collection_id}/up-next/regenerate", response_model=ThemeRefOut)
async def regenerate_up_next(collection_id: int, body: RegenerateRequest, request: Request) -> dict:
    """Write a new "Up next" theme for one person now, with one AI call, replacing any theme queued.

    409 while the row's AI is paused, 422 without an AI provider or when the row isn't set to Explore.
    """
    from shortlist.server.api.seasons import _off_loop
    from shortlist.server.api.themes import _PAUSED
    from shortlist.server.services import theme_rotation, theme_store
    from shortlist.server.services.theme_author import ThemeAuthorError

    state = request.app.state
    with state.sessions() as session:
        collection = _ai_row(session, collection_id)
        if collection.theme_mode != "explore":
            raise HTTPException(status_code=422, detail="Turn on Explore for this row first.")
        if collection.ai_paused:
            raise HTTPException(status_code=409, detail=_PAUSED)
        _audience_person(session, collection, body.user_id)

    def write() -> dict:
        tools = theme_rotation.authoring_tools(state)
        if tools.unavailable:
            raise HTTPException(status_code=tools.status, detail=tools.unavailable)
        with state.sessions() as session:
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
                )
            except ThemeAuthorError as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            except LookupError:
                raise HTTPException(status_code=422, detail="That person is no longer on the server.") from None
            except RuntimeError:
                raise HTTPException(
                    status_code=502, detail="Shortlist couldn't read their watch history. Check the Plex connection."
                ) from None
            except theme_store.RowPaused:
                raise HTTPException(status_code=409, detail=_PAUSED) from None
            except theme_store.TitleClash as e:
                raise HTTPException(status_code=422, detail=str(e)) from None
            queued = theme_rotation.queue_next(
                session, collection, body.user_id, theme, datetime.now(UTC), checked=True
            )
            session.commit()
            return _theme_ref(queued, {theme.id: theme})

    return await _off_loop(write, "theme authoring")


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
