"""Row validation, serialisation and patching, shared by the collections API and the assistant.

Below both transports so neither imports the other. The request models (`CollectionIn`) live here because the
checks read them; the HTTP routes and their response models stay in `shortlist.server.api.collections`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import HTTPException
from loguru import logger
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, text
from sqlalchemy.orm import Session

import shortlist.server.services.context_builder as context_builder
from shortlist.engine import seasons as seasons_mod
from shortlist.engine.candidates import KNOWN_SOURCES
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
)
from shortlist.engine.placeholders import refusal
from shortlist.engine.rows import row_shown_today
from shortlist.engine.themes import ThemeSpec
from shortlist.engine.web_guidance import INSTRUCTION_MODES, MAX_INSTRUCTIONS_CHARS
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    CollectionAudience,
    Delivery,
    Event,
    Job,
    PickRow,
    RequestCandidate,
    RowDeliverySnapshot,
    RunSharedRow,
    SharedRowWatch,
    Theme,
    User,
)
from shortlist.server.scheduler import crontab_trigger
from shortlist.server.schema_base import StrictRequestModel
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services import poster_service
from shortlist.server.services.poster_service import load_upload
from shortlist.server.services.row_changes import (
    RowChange,
)
from shortlist.server.services.row_views import ai_instructions_view, live_avoid_rows, row_display_name
from shortlist.server.services.season_catalogue import load_catalogue
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import SettingsStore

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


def closed_set(values: set[str], default: str, description: str) -> Field:
    """A string field whose accepted values are ADVERTISED in the OpenAPI schema.

    `validate_row` below is what actually rejects a bad value, and it stays the enforcement point — it
    raises one plain-English 422 naming the valid options, which a Pydantic enum error does not.
    But a schema that says only `str` is a schema that lies by omission: the SPA's types are
    generated from it, so every one of these closed sets arrived in TypeScript as a bare `string`
    and the UI had to re-declare the union by hand to get any checking at all.
    Sorted so the emitted schema is stable — `tests/unit/test_openapi_snapshot.py` compares it
    byte-for-byte, and a set's iteration order is not something to hang that on.
    """
    return Field(default=default, description=description, json_schema_extra={"enum": sorted(values)})


def closed_set_out(values: set[str], description: str) -> Field:
    """`closed_set` for a RESPONSE field: same advertised set, no default.

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

    mode: str = closed_set(POSTER_MODES, "", 'Poster source; "" leaves Plex artwork alone.')
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=120)
    style: str = Field(default="", max_length=400)


class AiInstructionsIn(StrictRequestModel):
    """What AI web search should look for on this row (#138). ``default`` uses the built-in wording
    plus the server-wide instructions; ``add`` appends ``text`` to them; ``own`` replaces them."""

    mode: str = closed_set(set(INSTRUCTION_MODES), "default", "AI instructions must be default, add or own")
    text: str = Field(default="", max_length=MAX_INSTRUCTIONS_CHARS)


class CollectionIn(StrictRequestModel):
    name: str = Field(min_length=1, max_length=255)
    build: str = closed_set(BUILDS, "per_person", "Who the row is built for: one per person, or one shared row.")
    audience: str = closed_set(AUDIENCES, "everyone", "Everyone, or the subset named by audience_user_ids.")
    audience_user_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    # This row's own run schedule (5-field cron); "" = never runs on a schedule. New rows default to
    # a nightly 03:30 so they work out of the box; there is no global schedule.
    schedule: str = Field(default="30 3 * * *", max_length=64)
    size: int = Field(default=15, ge=MIN_ROW_SIZE, le=MAX_ROW_SIZE)
    media: str = closed_set(MEDIA, "both", "Which library types this row builds in.")
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
    # Implemented by PROJECTING the post-edit snapshot (`projected_snapshot`), never by applying the
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
    # Per-row limits on what may be picked; None = no limit (#138). Year order is checked in `validate_row`.
    max_runtime: int | None = Field(default=None, ge=1, le=600)  # minutes
    min_year: int | None = Field(default=None, ge=1870, le=2100)
    max_year: int | None = Field(default=None, ge=1870, le=2100)
    min_rating: float | None = Field(default=None, ge=0.0, le=10.0)  # TMDB vote_average
    # "popular" | "skip" | None -> inherit the global recommendations.cold_start. Enforced in
    # `validate_row`, like every other closed set here.
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
    # Enforced in `validate_row` (a plain-English 422 naming the modes, which a Pydantic enum error is
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
    pick_order: str = closed_set(ORDERS, "best", "How the delivered collection is ordered.")
    library_keys: list[str] = Field(default_factory=list)  # [] -> every library of the row's media type
    placement: str = closed_set(PLACEMENTS, "both", "Where the OWNER's own collection appears.")
    placement_friends: str = closed_set(PLACEMENTS, "both", "Where each FRIEND's own collection appears.")
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
    # the candidate pool. Always per-person, never rewatch, never seasonal — `validate_row` says so.
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
        return normalise_show_days(days)


def normalise_show_days(days: list[int]) -> list[int]:
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


def stored_instructions(body: AiInstructionsIn) -> dict[str, str]:
    """What `Collection.prompt` holds: {} for the default, so an untouched row stores exactly what it did."""
    if body.mode == "default":
        return {}
    return {"mode": body.mode, "text": body.text.strip()}


def validate_row(body: CollectionIn) -> None:
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
        # anything currently depends on: `serialize_row` normalises this column on the way out, so the
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
    validate_pairing(rewatch=body.rewatch, unstarted_only=body.unstarted_only, media=body.media)
    pattern = body.requests_tag_pattern.strip()
    if pattern and "{username}" not in pattern and "{name}" not in pattern:
        raise HTTPException(status_code=422, detail="Tag pattern needs {username} or {name} in it")
    validate_requests_row(
        requests_row=body.requests_row,
        build=body.build,
        rewatch=body.rewatch,
        seasons=body.seasons,
        has_theme=body.theme_id is not None,
    )


def validate_requests_row(
    *, requests_row: bool, build: str, rewatch: bool, seasons: list[str], has_theme: bool = False
) -> None:
    """The shapes a "Your requests" row cannot take. A person's requests are theirs alone, so the row is
    always per-person; it holds titles they have NOT seen, so it cannot lead with finished ones; and a
    request lands when it lands, so no season decides whether the row shows.

    Keyword-only like `validate_pairing`, and for the same reason: a PATCH judges the MERGED row, so
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


def validate_anchor_rows(session: Session, body: CollectionIn, editing_slug: str) -> None:
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


def anchored_to(entry: object, gone: str) -> bool:
    """Whether one ``hub_anchor`` entry positions its row relative to row ``gone``.

    The single definition of that match, so the DELETE preview's warning and the delete that carries
    it out cannot disagree about which rows lose their placement. Only ``row`` is matched: a
    placement anchored to a foreign collection TITLE names no row and survives the delete.
    """
    return isinstance(entry, dict) and str(entry.get("row") or "").strip() == gone


def rows_anchored_to(session: Session, gone: str) -> list[str]:
    """Slugs of the rows whose shelf placement is positioned relative to row ``gone`` — read only."""
    return [
        row.slug
        for row in session.query(Collection).all()
        if any(anchored_to(entry, gone) for entry in (row.hub_anchor or {}).values())
    ]


def validate_pairing(*, rewatch: bool, unstarted_only: bool, media: str) -> None:
    """Refuse the two combinations of these three fields that a row cannot honour.

    Its own function because a PATCH must check the MERGED row, not the request body — `CollectionIn`
    hands `validate_row` its DEFAULTS for anything the request omitted, so a row already set to rewatch
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
    # "Shows only" in the field's own docs, and structurally: `started_shows` yields only show keys, so
    # on a movies row the flag is inert. Storing an inert setting the editor won't even show is how a
    # row ends up behaving unlike what its settings say.
    if unstarted_only and media == "movie":
        raise HTTPException(
            status_code=422,
            detail="unstarted_only applies to shows — a movie is finished the moment it is watched, "
            "so there is no 'started' state to exclude",
        )


def poster_view(session: Session, collection: Collection) -> dict:
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


def season_window_view(window: seasons_mod.SeasonWindow | None) -> dict | None:
    if window is None:
        return None
    return {
        "slug": window.season.slug,
        "name": window.season.name,
        "emoji": window.season.emoji,
        "starts": window.starts.isoformat(),
        "ends": window.ends.isoformat(),
    }


def season_status(collection: Collection, now: datetime, *, catalogue: seasons_mod.Catalogue) -> dict | None:
    """Which season a seasonal row shows today and which comes next, on the server's clock; None if not seasonal."""
    if not collection.seasons:
        return None
    args = (list(collection.seasons), collection.season_lead_days, collection.season_after_days, now.date())
    showing = seasons_mod.shown_on(*args, catalogue=catalogue)
    if showing is not None:
        # Its last day on screen, which is not its window's end when a following season takes over first.
        showing = replace(showing, ends=seasons_mod.last_shown_day(*args, catalogue=catalogue))
    upcoming = seasons_mod.next_after(*args, catalogue=catalogue)
    return {"showing": season_window_view(showing), "next": season_window_view(upcoming)}


#: How many titles the Rows list's collage shows for a row.
PREVIEW_TITLE_COUNT = 4


def preview_titles(session: Session, slugs: list[str]) -> dict[str, list[dict]]:
    """Up to four titles from each row's most recent delivery, keyed by slug, for the Rows list.

    Read confirmed current delivery snapshots, independently of diagnostic run history. Personal
    titles are deduplicated at their best rank; shared titles retain their delivered order. Picks
    without a matched Plex rating key have no artwork to show and are left out.

    Args:
        session: An open database session.
        slugs: The rows to read.

    Returns:
        slug -> `{"rating_key", "title"}` dicts, best ranked first. A row that never built is absent.
    """
    from shortlist.server.services.delivery_snapshots import current_pick_ids, current_snapshots

    ids = {pick_id for picks in current_pick_ids(session).values() for pick_id in picks}
    best_rank = func.min(PickRow.rank)
    per_person = (
        session.query(PickRow.collection_slug, PickRow.rating_key, func.min(PickRow.title), best_rank)
        .filter(PickRow.id.in_(ids), PickRow.collection_slug.in_(slugs), PickRow.rating_key > 0)
        .group_by(PickRow.collection_slug, PickRow.rating_key)
        .order_by(PickRow.collection_slug, best_rank, PickRow.rating_key)
        .all()
    )
    previews: dict[str, list[dict]] = {}
    for slug, rating_key, title, _rank in per_person:
        titles = previews.setdefault(slug, [])
        if len(titles) < PREVIEW_TITLE_COUNT:
            titles.append({"rating_key": rating_key, "title": title})

    for snapshot in current_snapshots(session):
        if not snapshot.shared or snapshot.collection_slug not in slugs:
            continue
        titles = previews.setdefault(snapshot.collection_slug, [])
        seen = {pick["rating_key"] for pick in titles}
        for pick in snapshot.picks:
            rating_key = pick.get("rating_key") or 0
            if rating_key <= 0 or rating_key in seen or len(titles) >= PREVIEW_TITLE_COUNT:
                continue
            seen.add(rating_key)
            titles.append({"rating_key": rating_key, "title": pick.get("title") or ""})

    return previews


def serialize_row(
    session: Session,
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
        previews: `preview_titles` for a whole list, read once by the caller; read here for this one
            row when omitted.

    Returns:
        The `CollectionOut` payload.
    """
    # One clock read for everything this row reports about today: the badge and the season status must
    # describe the same day, even for a response built across midnight.
    now = now or context_builder.local_now()
    if previews is None:
        previews = preview_titles(session, [collection.slug])
    audience_ids = [
        row.user_id for row in session.query(CollectionAudience).filter_by(collection_id=collection.id).all()
    ]
    name = row_display_name(session, collection)
    # An Explore row wears a different theme each period, so only a fixed row has one name to show.
    fixed_theme = (
        session.get(Theme, collection.theme_id)
        if collection.theme_id is not None and (collection.theme_mode or "fixed") == "fixed"
        else None
    )
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
        "season_status": season_status(collection, now, catalogue=catalogue),
        "placement_friends": collection.placement_friends or "both",
        "pin_top": bool(collection.pin_top),
        "hub_anchor": collection.hub_anchor or {},
        "library_keys": [str(k) for k in (collection.library_keys or [])],
        "poster": poster_view(session, collection),
        "ai_instructions": ai_instructions_view(collection.prompt),
        "theme_id": collection.theme_id,
        "theme_name": None if fixed_theme is None else fixed_theme.name,
        "theme_emoji": None if fixed_theme is None else fixed_theme.emoji or None,
        "ai_paused": bool(collection.ai_paused),
        "ai_tokens": collection.ai_tokens or 0,
        "theme_mode": collection.theme_mode or "fixed",
        "explore_brief": collection.explore_brief or "",
        "theme_days": collection.theme_days,
        "refresh_share": collection.refresh_share,
        "repeat_cooldown_days": collection.repeat_cooldown_days,
        "avoid_rows": live_avoid_rows(session, collection),
    }


def reject_season_name_without_seasons(template: str, seasons: list[str], *, row_has_theme: bool = False) -> None:
    """Refuse a name that uses the season on a row that follows none, or the theme on a row that is not an AI
    row: it could never be filled in, so the row would never be built for anyone (discussion #124, #138)."""
    if why := refusal(template or "", "row_name", row_has_seasons=bool(seasons), row_has_theme=row_has_theme):
        raise HTTPException(status_code=422, detail=why)


def validate_theme(
    session: Session,
    theme_id: int | None,
    *,
    build: str,
    seasons: list[str],
    rewatch: bool = False,
    requests_row: bool = False,
    trusted_theme: Theme | None = None,
) -> Theme | None:
    """The theme an AI row follows, or None for an ordinary row; 422 for a row a theme cannot drive.

    Keyword-only on what the row will be, like `validate_requests_row`: a PATCH judges the MERGED row.
    AI rows are per-person in v1, and a theme has no calendar, so a season is refused too.
    """
    if trusted_theme is not None:
        from sqlalchemy import inspect

        if theme_id is not None or not inspect(trusted_theme).transient:
            raise ValueError("A trusted theme projection must be transient and cannot replace a saved theme ID.")
    if theme_id is None and trusted_theme is None:
        return None
    theme = trusted_theme if trusted_theme is not None else session.get(Theme, theme_id)
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


EXPLORE_COLUMNS = ("theme_mode", "explore_brief", "theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows")


EXPLORE_DEFAULTS = {
    "theme_mode": "fixed",
    "explore_brief": "",
    "theme_days": None,
    "refresh_share": None,
    "repeat_cooldown_days": None,
    "avoid_rows": None,
}


def validate_explore(
    session: Session,
    values: dict,
    *,
    theme_id: int | None,
    own_slug: str,
    only: set[str] | None = None,
    already_avoided: tuple[str, ...] = (),
    has_theme: bool | None = None,
) -> None:
    """422 for Explore settings or over-time controls a row cannot use (#138).

    ``values`` is the merged row. They exist only on an AI row, so each needs a theme; ``only`` limits the
    "needs a theme" check to the fields a PATCH actually sent. ``avoid_rows`` must name other, existing
    per-person rows, but only a slug the request newly ADDS is checked: ``already_avoided`` are the row's stored
    ones, which a row deleted since then must not make unsavable.
    """
    if not (theme_id is not None if has_theme is None else has_theme):
        stray = [c for c in (only if only is not None else EXPLORE_COLUMNS) if values[c] != EXPLORE_DEFAULTS[c]]
        if stray:
            raise HTTPException(
                status_code=422,
                detail="Explore and the over-time controls only apply to an AI row. Give the row a theme.",
            )
    for slug in values["avoid_rows"] or []:
        if slug in already_avoided:
            continue
        if slug == own_slug:
            raise HTTPException(status_code=422, detail=f"A row can't keep out its own titles (“{slug}”).")
        other = session.query(Collection).filter(Collection.slug == slug).first()
        if other is None:
            raise HTTPException(status_code=422, detail=f"There is no row “{slug}” to keep out.")
        if other.build != "per_person":
            raise HTTPException(
                status_code=422, detail=f"“{slug}” is a shared row. Only per-person rows can be kept out."
            )


def unattributed_theme_tokens(session: Session, theme: Theme | None, *, exclude_id: int | None) -> int:
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


def reject_duplicate_name(
    session: Session,
    secrets: SecretBox | None,
    template: str,
    *,
    exclude_slug: str = "",
    build: str = "",
    fallback_name: str = "",
    media: str = "both",
    library_keys: Iterable[str | int] = (),
    already_clashing: frozenset[str] = frozenset(),
    theme: ThemeSpec | None = None,
) -> None:
    """Refuse a row title another row is already titled from — see `reconcile.rows_titled_from` for
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
    # Name the FIELD that collided, not just the row. Quoting only the row name ("'More like {top_seed}' is already
    # the title of …") on a fallback clash sends the operator to the box that isn't the problem.
    culprit = template
    where = "name"
    if fallback_name and reconcile.title_key(fallback_name) in reconcile.row_title_keys(
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


#: PATCH fields that can move a row's title for a person, or the libraries and people it shares with another row.
TITLE_MOVING_FIELDS = {
    "name",
    "name_template",
    "media",
    "library_keys",
    "audience",
    "audience_user_ids",
    "theme_mode",
    "theme_id",
    "enabled",
    "fallback_name",
}


def reject_new_person_title_clash(
    session: Session,
    secrets: SecretBox | None,
    collection: Collection,
    body: CollectionIn,
    sent: set[str],
    *,
    media: str,
    library_keys: list[str],
    theme: ThemeSpec | None,
) -> None:
    """422 when this edit would give one person two rows of one title in a library both build in.

    An explore row wears each person's own theme, which the server-wide title check never sees (#121). So the
    edit is judged per person: the clashes that exist now against those that would exist after it. Only a clash
    the edit ADDS is refused, so a row that already clashes stays editable for anything unrelated.
    """
    account_by_user, audience_by_collection = context_builder.ContextBuilder.audience_maps(session)
    audience_sent = bool(sent & {"audience", "audience_user_ids"})
    if audience_sent:
        subset = (body.audience if "audience" in sent else collection.audience) == "subset"
        members = audience_after_set(body)
        accounts = frozenset(account_by_user[uid] for uid in members if uid in account_by_user) if subset else None
    else:
        accounts = reconcile.frozenset_or_none(
            context_builder.ContextBuilder.subset_audience(collection, account_by_user, audience_by_collection)
        )
    theme_mode = body.theme_mode if "theme_mode" in sent else collection.theme_mode
    after = reconcile.RowView(
        slug=collection.slug,
        name=collection.name,
        template=merged_template(collection, body, sent),
        fallback_name=(body.fallback_name if "fallback_name" in sent else collection.fallback_name) or "",
        media=media or "both",
        library_keys=tuple(str(k) for k in library_keys),
        audience=accounts,
        base_theme=theme,
        explore=theme_mode == "explore",
        row_id=collection.id,
    )
    clash = reconcile.new_person_clash(session, secrets, collection, after)
    if clash is None:
        return
    other, person, title = clash
    who = person.nickname or person.friendly_name or person.username
    raise HTTPException(
        status_code=422,
        detail=f"{title!r} would be the title of this row for {who}"
        f" and of the row {other.name!r} ({other.slug}) too, in a library both build in — two rows with the same "
        "title in one library become a single collection on Plex, so pick a different name, theme or library.",
    )


def unique_slug(session: Session, base: str) -> str:
    """A slug no row has now AND no history still names.

    The slug is a row's identity in every history table, and deleting a row frees it in `collections`
    alone. A new row that took it over inherited the deleted row's last picks (redelivered as "not due
    to rebuild"), its delivery ledger, its shared-row picks and watch credits, and its queued requests —
    seen live on 2026-09-13. A delivered row's history is kept (run pruning leaves picks, deliveries and
    watch credits alone), so in practice its slug stays reserved for good.

    Skipped/pending run outcomes and retained delivery snapshots also reserve the identity even
    when no pick or current delivery remains. Otherwise a replacement inherits another row's reports.

    A pending `row.reconcile` for the slug counts too, including one inside `assistant.converge`.
    DELETE queues it before dropping the row, and it
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
        RowDeliverySnapshot.collection_slug,
    )

    def is_taken(slug: str) -> bool:
        if any(session.query(column).filter(column == slug).first() is not None for column in columns):
            return True
        # These are structured JSON identities, not free text. Bind the exact slug so
        # punctuation and prefix matches cannot attach one row's history to another.
        histories = (
            "SELECT 1 FROM run_users, json_each(run_users.rows_considered) AS outcome "
            "WHERE outcome.key = :slug LIMIT 1",
            "SELECT 1 FROM runs, json_each(runs.stats, '$.expected_rows') AS expected "
            "WHERE expected.type = 'object' AND json_extract(expected.value, '$.slug') = :slug LIMIT 1",
            "SELECT 1 FROM jobs, json_each(jobs.payload, '$.steps') AS step "
            "WHERE jobs.kind = 'assistant.converge' AND jobs.status IN ('queued', 'running') "
            "AND step.type = 'object' AND json_extract(step.value, '$.kind') = 'row.reconcile' "
            "AND json_extract(step.value, '$.payload.slug') = :slug LIMIT 1",
        )
        if any(session.execute(text(query), {"slug": slug}).first() is not None for query in histories):
            return True
        pending_removal = session.query(Job.id).filter(
            Job.kind == "row.reconcile",
            Job.status.in_(("queued", "running")),
            func.json_extract(Job.payload, "$.slug") == slug,
        )
        return pending_removal.first() is not None

    return dedupe_slug(base, is_taken)


def set_audience(session: Session, collection: Collection, body: CollectionIn) -> None:
    """Replace this row's audience with the requested user ids, refusing any that don't exist.

    The ids are RESOLVED first, deliberately. `CollectionAudience.user_id` is a foreign key and the
    connection runs with `PRAGMA foreign_keys=ON`, so an unknown id used to surface as an
    `IntegrityError` at commit — an unhandled 500 carrying a SQL string, where every other bad input
    on this router is a 422. And on a SHARED row this list is what decides who is excluded from the
    share filter, so silently dropping an id nobody recognises is the wrong direction to fail in.
    """
    session.query(CollectionAudience).filter_by(collection_id=collection.id).delete()
    validate_audience_ids(session, body)
    for user_id in audience_after_set(body):
        session.add(CollectionAudience(collection_id=collection.id, user_id=user_id))


def audience_after_set(body: CollectionIn) -> list[int]:
    """The membership :func:`set_audience` leaves behind, deduped and in request order.

    Gated on the RAW ``body.audience``, NOT the row's merged value — and that distinction is the
    whole reason this is a function. `CollectionIn.audience` defaults to "everyone", so a PATCH that
    sends ``audience_user_ids`` ALONE clears the membership of a row that stays "subset": everyone
    is dropped, not just the ids left out. A preview that reasoned from the merged audience instead
    reported one person's collection going while the save removed all forty.
    """
    if body.audience != "subset":
        return []
    return list(dict.fromkeys(body.audience_user_ids))  # dedupe, keep order


def validate_audience_ids(session: Session, body: CollectionIn) -> None:
    """Refuse an audience naming a user who does not exist.

    Hoisted out of `set_audience` so the PATCH handler can run it BEFORE its first write. It used to
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
    wanted = audience_after_set(body)
    if not wanted:
        return
    known = {user_id for (user_id,) in session.query(User.id).filter(User.id.in_(wanted)).all()}
    if unknown := [user_id for user_id in wanted if user_id not in known]:
        raise HTTPException(status_code=422, detail=f"no such user(s): {unknown} — the audience must be existing users")


def known_seasons(slugs: list[str], *, catalogue: seasons_mod.Catalogue) -> list[str]:
    """Known seasons only, de-duplicated and in calendar order, so equal choices compare equal.

    In the handlers rather than a field validator on ``CollectionIn``: the catalogue holds the owner's own
    seasons (issue #137), which live in the database, and a validator has no session to read them with.
    """
    try:
        return seasons_mod.normalise_slugs(slugs, catalogue=catalogue)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None


#: This row's own request floors and Arr target; null on any of them means inherit the global. Named once
#: for create and edit alike: the create constructor listed columns by hand and missed every one of these.
REQUEST_COLUMNS = (
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
PATCHABLE_COLUMNS = (
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
    *REQUEST_COLUMNS,
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
    *EXPLORE_COLUMNS,
)


def stranded_sections(
    state: Any,
    *,
    old_media: str,
    old_keys: list[str],
    new_media: str,
    new_keys: list[str],
    unreadable: list[str] | None = None,
    sections: list | None = None,
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
            `reconcile_row_removal`'s ``removed``.
    """
    if (old_media, sorted(old_keys)) == (new_media, sorted(new_keys)):
        return set()
    if sections is None:
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


def apply_row_patch(
    session: Session,
    secrets: SecretBox | None,
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
        SettingsStore(session, secrets).set_in_transaction("row.name_template", default_rename_to)
    elif "name" in sent and not is_default:
        collection.name = body.name
    for column in PATCHABLE_COLUMNS:
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
        for column, default in EXPLORE_DEFAULTS.items():
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
        collection.prompt = stored_instructions(body.ai_instructions)
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
        set_audience(session, collection, body)


def merged_template(collection: Collection, body: CollectionIn, sent: set[str]) -> str:
    """This row's effective title template once the PATCH lands.

    Delivery renders from ``name_template or name``, and a PATCH may send either half — so both are
    taken from the request when the request sent them and off the row when it did not. One
    definition because the duplicate-title check and the rename plan must agree on the title: they
    used to compute it separately, and a check that disagrees with the rename is a check of nothing.
    """
    return (body.name_template if "name_template" in sent else collection.name_template) or (
        body.name if "name" in sent else collection.name
    )


def row_snapshot(session: Session, collection: Collection) -> dict:
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


def projected_snapshot(session: Session, collection: Collection, body: CollectionIn, sent: set[str]) -> dict:
    """What :func:`row_snapshot` WOULD return after this PATCH, computed without touching the row.

    The dry-run counterpart to `row_snapshot`, and deliberately NOT "apply it and roll back": the
    handler's default-row rename goes through `SettingsStore.set`, which commits inside itself, so a
    rollback would not undo it — a preview of one row's rename would permanently retitle every row on
    the server.

    Every field here is one `row_snapshot` reads, resolved the way the apply path resolves it. A
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
        The same shape `row_snapshot` returns.

    The caller must have run `validate_audience_ids` first; this projects a VALID edit and does not
    re-check one.
    """
    before = row_snapshot(session, collection)
    # The row's `audience` column takes the request's value only when the request sent it...
    audience_kind = body.audience if "audience" in sent else collection.audience
    if audience_kind == "everyone":
        # ...and "everyone" ignores the membership table entirely, resolving to whoever is on the
        # roster right now — which is what makes a row switched to everyone drop nobody.
        audience = frozenset(user_id for (user_id,) in session.query(User.id).all())
    elif sent & {"audience", "audience_user_ids"}:
        # ...but the MEMBERSHIP is rewritten by `set_audience`, which gates on the RAW body value.
        # Deferring to `audience_after_set` is what keeps the two from disagreeing: read from the
        # merged audience instead and a PATCH sending only `audience_user_ids` previews one removal
        # where the save performs one per person on the server.
        audience = frozenset(audience_after_set(body))
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


def row_change(
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
