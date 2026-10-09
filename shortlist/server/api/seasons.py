"""Seasons API (issue #137): every season a Seasonal row can follow — the built-ins and the owner's own — and
what the season editor reads while the owner makes one. Owner-only.

A custom season reads TMDB and the owner's Plex libraries, and writes neither: a Plex collection it names is
only ever listed and read (plex-safety rule 4).
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from loguru import logger
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import State

import shortlist.server.services.context_builder as context_builder
from shortlist.engine import seasons as seasons_mod
from shortlist.engine.clients.http_retry import redact
from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.delivery import section_kind
from shortlist.engine.models import MediaType, RowSeason
from shortlist.engine.placeholders import fill_season, naming_season, uses_season
from shortlist.engine.rows import row_shown_today
from shortlist.engine.seasons import (
    BUILTIN_SEASONS,
    MAX_AFTER_DAYS,
    MAX_LEAD_DAYS,
    PRESET_TAG_NAMES,
    PRESETS,
    CollectionRef,
    DateRule,
    Preset,
    Season,
)
from shortlist.server.api.collections import row_display_name
from shortlist.server.api.schemas import PassthroughModel
from shortlist.server.auth import require_owner
from shortlist.server.db.models import Collection, SeasonDef
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services import jobs
from shortlist.server.services.library_index import library_index, row_sections
from shortlist.server.services.season_catalogue import load_catalogue

router = APIRouter(prefix="/seasons", tags=["seasons"], dependencies=[Depends(require_owner)])

#: A search shorter than this answers nothing rather than everything.
_MIN_QUERY = 2
#: Titles one library search returns.
_LIBRARY_SEARCH_LIMIT = 10
#: Pages of one TMDB list the editor reads at once. A run reads with the client's default: only an owner
#: waiting on a page is worth the burst.
_PREVIEW_PAGE_WORKERS = 6

_NO_TMDB = "Add a TMDB API key in Settings first."
_NO_PLEX = "Plex isn't connected yet — finish setup first."


# Request and response share these, so they pass undeclared keys through (`schemas.PassthroughModel`); the
# bodies that SAVE a season refuse an unknown top-level field instead (`SeasonIn`).
class DateRuleIO(PassthroughModel):
    """When a season falls: see `seasons.DateRule`. ``weekday`` is Monday=0; ``nth`` is 1-4, or -1 for the last."""

    kind: Literal["fixed", "nth", "easter", "month"]
    month: int = 1
    day: int = 1
    nth: int = 1
    weekday: int = 0
    offset: int = 0


class TagIO(PassthroughModel):
    """A TMDB tag (keyword), with its name so the editor can show it without asking TMDB."""

    id: int
    name: str


class CollectionIO(PassthroughModel):
    """A Plex collection, by library and title — never ratingKey: Kometa recreates its seasonal ones each year."""

    section_key: str
    section_title: str
    title: str


class PickIO(PassthroughModel):
    """A title the owner picked by hand."""

    tmdb_id: int
    media_type: Literal["movie", "show"]
    title: str
    year: int | None = None


class SeasonSourcesIO(PassthroughModel):
    """Where a season's films come from. Only titles in the libraries are ever used."""

    tags: list[TagIO] = Field(default_factory=list, max_length=20)
    #: A whole TMDB film genre, read at the discover vote floor.
    genre: int | None = None
    #: Genres a tag or collection film may not carry (a hand pick is never left out).
    excluded_genres: list[int] = Field(default_factory=list, max_length=5)
    collections: list[CollectionIO] = Field(default_factory=list, max_length=10)
    picks: list[PickIO] = Field(default_factory=list, max_length=200)


class SeasonIn(SeasonSourcesIO):
    """A custom season, to create or replace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=40)
    emoji: str = Field(min_length=1, max_length=8)
    rule: DateRuleIO
    lead_days: int = Field(7, ge=0, le=MAX_LEAD_DAYS)
    after_days: int = Field(0, ge=0, le=MAX_AFTER_DAYS)
    #: The ready-made season this was added from (`GET /presets`), so it stops being offered. Set on create
    #: only: an edit keeps where the season came from.
    preset: str | None = None


class UsedByOut(PassthroughModel):
    """A row that follows the season."""

    id: int
    name: str


class SeasonWindowOut(PassthroughModel):
    """Inclusive calendar dates for a month-long season."""

    start: str
    end: str


class SeasonOut(SeasonSourcesIO):
    """A season a row can follow. A built-in's sources live in code and are not listed."""

    slug: str
    name: str
    emoji: str
    description: str
    builtin: bool
    rule: DateRuleIO
    #: The rule in plain English: "4th Thursday of November".
    rule_label: str
    #: ISO dates: the season's next two days on or after today, on the server's clock.
    next_dates: list[str]
    #: Exact month windows; empty for day rules, whose timing comes from the season or its row.
    next_windows: list[SeasonWindowOut] = Field(default_factory=list)
    #: None for a built-in, which follows its row's "Built-in seasons show from…" timing.
    lead_days: int | None
    after_days: int | None
    preset: str | None
    used_by: list[UsedByOut]


class PresetOut(SeasonIn):
    """A ready-made season's save fields plus catalogue metadata. ``preset`` is ``key``."""

    model_config = ConfigDict(extra="allow")

    key: str
    #: What the editor calls the preset, region included: "Mother's Day (US, CA, AU, NZ)". ``name`` has none,
    #: because a row's title renders it.
    label: str
    #: What to add when TMDB's tags fall short, e.g. "TMDB tags very few films as Father's Day — add a…".
    note: str
    category: Literal["holidays", "film_days", "spotlights"] = "holidays"
    description: str = ""


class SeasonPreviewIn(SeasonSourcesIO):
    """A draft season to count, for the row the editor was opened from. Undeclared fields are let through, not
    refused: the editor posts its whole draft, and a preview stores nothing that a misspelt field could
    silently fail to set."""

    rule: DateRuleIO
    #: The row's ``media``: only titles of its type count, because a films row never draws a show.
    media: Literal["movie", "show", "both"] = "both"
    #: The row's ``library_keys``: only titles in those libraries count. Empty is every library of its type.
    library_keys: list[str] = Field(default_factory=list)


class SeasonDateOut(PassthroughModel):
    """When a date rule next falls (ISO), or, with ``next_date`` None, why the rule can't be used."""

    next_date: str | None
    rule_error: str | None
    next_windows: list[SeasonWindowOut] = Field(default_factory=list)


class CollectionCountOut(PassthroughModel):
    title: str
    section_key: str
    #: False when the library has no collection of that title right now.
    found: bool
    #: Its titles the season would use: in a library, known to TMDB, and not of a left-out genre.
    in_library: int


class SeasonPreviewOut(PassthroughModel):
    """What the editor's summary shows. Every count is of titles in the libraries."""

    #: ISO date; None while the date rule is invalid.
    next_date: str | None
    #: Why the date rule is invalid, worded for the owner.
    rule_error: str | None
    total: int
    #: ``total``'s films and shows. A row fills each library from its own type, so a row of both needs each
    #: half to be enough. None for a type the row builds in no library of.
    movies: int | None
    shows: int | None
    #: The total split by the first of these sources, in this order, to give each title.
    from_tags: int
    from_genre: int
    from_collections: int
    from_picks: int
    #: Tag id -> titles that tag alone gives.
    per_tag: dict[int, int]
    per_collection: list[CollectionCountOut]
    #: Up to 10 titles, the most voted on TMDB first.
    sample: list[str]


class TagOut(PassthroughModel):
    id: int
    name: str
    #: Films on TMDB with the tag, in or out of the libraries.
    movies: int


class PlexCollectionOut(PassthroughModel):
    section_key: str
    section_title: str
    title: str
    count: int
    smart: bool
    #: Its library's type, so the editor counts a TV collection in shows.
    media_type: Literal["movie", "show"]


class LibraryTitleOut(PassthroughModel):
    tmdb_id: int
    media_type: Literal["movie", "show"]
    title: str
    year: int | None


@router.get("", response_model=list[SeasonOut])
async def list_seasons(request: Request) -> list[dict]:
    """Every season a row can follow, in calendar order: the built-ins and the owner's own."""
    today = context_builder.local_now().date()
    with request.app.state.sessions() as session:
        catalogue = load_catalogue(session)
        stored = {row.slug: row for row in session.scalars(select(SeasonDef))}
        used_by = _used_by(session, catalogue)
        return [
            _season_view(catalogue[slug], None if catalogue[slug].builtin else stored[slug], used_by, today)
            for slug in seasons_mod.normalise_slugs(list(catalogue), catalogue=catalogue)
        ]


@router.get("/presets", response_model=list[PresetOut])
async def list_presets(request: Request) -> list[dict]:
    """The ready-made seasons not added yet (#137 D9)."""
    with request.app.state.sessions() as session:
        added = set(session.scalars(select(SeasonDef.preset).where(SeasonDef.preset.is_not(None))))
    return [_preset_view(preset) for preset in PRESETS if preset.key not in added]


@router.post("", status_code=201, response_model=SeasonOut)
async def create_season(body: SeasonIn, request: Request) -> dict:
    """Save a new season. Its slug is made from its name now and never changes (D14)."""
    return _save_season(request.app.state, "create", body=body)


@router.put("/{slug}", response_model=SeasonOut)
async def update_season(slug: str, body: SeasonIn, request: Request) -> dict:
    """Replace a custom season and atomically record owed visibility work."""
    return _save_season(request.app.state, "update", slug=slug, body=body)


@router.delete("/{slug}", status_code=204)
async def delete_season(slug: str, request: Request) -> Response:
    """Untick a custom season and save its required visibility work in one transaction."""
    _save_season(request.app.state, "delete", slug=slug)
    return Response(status_code=204)


def _save_season(state, action: str, *, slug: str | None = None, body=None) -> dict | None:
    from shortlist.server.assistant.row_effects import queue_convergence_in_session
    from shortlist.server.services.season_changes import (
        apply_season_in_session,
        prepare_season_in_session,
        season_view_in_session,
    )

    with state.sessions() as session:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        mutation = prepare_season_in_session(session, action, slug=slug, body=body)
        row = apply_season_in_session(session, state, mutation)
        if mutation.steps:
            queue_convergence_in_session(session, mutation.steps, domain="seasons")
        session.flush()
        result = season_view_in_session(session, row) if row is not None else None
        session.commit()
    if mutation.steps:
        jobs.drain_in_background(
            state, f"season '{mutation.slug}' was deleted" if action == "delete" else f"season '{mutation.slug}' moved"
        )
    return result


@router.post("/preview", response_model=SeasonPreviewOut)
async def preview_season(body: SeasonPreviewIn, request: Request) -> dict:
    """Count a draft season's titles in one row's libraries, as a run would, without saving anything.

    Only the row's own media type and libraries count: that is all the row draws from (#137 I-1). An invalid
    date rule still counts: the editor shows what is wrong with the date beside the count.
    """
    state = request.app.state
    tmdb = state.run_service.build_tmdb_only()
    if tmdb is None:
        raise HTTPException(status_code=503, detail=_NO_TMDB)
    draft = _draft(body)
    today = context_builder.local_now().date()

    def count() -> tuple[seasons_mod.SeasonPreview, set[MediaType]]:
        plex = _plex(state)
        index = library_index(plex, state.sessions, media=body.media, library_keys=body.library_keys)
        kinds = {section_kind(s) for s in row_sections(plex, media=body.media, library_keys=body.library_keys)}
        return seasons_mod.preview(tmdb, plex, draft, index, today=today, workers=_PREVIEW_PAGE_WORKERS), kinds

    result, kinds = await _off_loop(count, "season preview")
    return {
        **dataclasses.asdict(result),
        "movies": result.movies if MediaType.MOVIE in kinds else None,
        "shows": result.shows if MediaType.SHOW in kinds else None,
        "next_date": result.next_date.isoformat() if result.next_date else None,
        "per_collection": [dataclasses.asdict(c) for c in result.per_collection],
        "sample": list(result.sample),
    }


@router.post("/next-date", response_model=SeasonDateOut)
async def next_date(body: DateRuleIO) -> dict:
    """When a draft date rule next falls, or why it can't be used — from the rule alone.

    The editor's "Next: …" line asks this rather than the count, so a count that fails (no TMDB key, Plex
    down) never takes the date with it. Reads no clock but the server's, and nothing else.
    """
    rule = DateRule(
        kind=body.kind, month=body.month, day=body.day, nth=body.nth, weekday=body.weekday, offset=body.offset
    )
    try:
        rule.validate()
    except ValueError as e:
        return {"next_date": None, "rule_error": str(e)}
    today = context_builder.local_now().date()
    season = Season(slug="draft", name="Draft", emoji="", rule=rule, description="")
    return {
        "next_date": seasons_mod.next_anchors(season, today, 1)[0].isoformat(),
        "rule_error": None,
        "next_windows": _month_windows(season, today),
    }


@router.get("/tmdb-tags", response_model=list[TagOut])
async def tmdb_tags(request: Request, q: str = "") -> list[dict]:
    """TMDB tags whose name matches ``q``, each with how many films TMDB tags with it."""
    query = q.strip()
    if len(query) < _MIN_QUERY:
        return []
    tmdb = request.app.state.run_service.build_tmdb_only()
    if tmdb is None:
        raise HTTPException(status_code=503, detail=_NO_TMDB)
    return await _off_loop(lambda: tmdb.search_keywords(query), "TMDB tag search")


@router.get("/plex-collections", response_model=list[PlexCollectionOut])
async def plex_collections(request: Request, q: str = "") -> list[dict]:
    """The libraries' collections whose title contains ``q``, in any case, by title. Never one of Shortlist's."""
    query = q.strip().casefold()
    if len(query) < _MIN_QUERY:
        return []
    state = request.app.state
    found = await _off_loop(lambda: _plex(state).list_collections(), "Plex collection list")
    matches = sorted(
        (c for c in found if query in c.title.casefold()), key=lambda c: (c.title.casefold(), c.section_title)
    )
    return [
        {
            "section_key": c.section_key,
            "section_title": c.section_title,
            "title": c.title,
            "count": c.count,
            "smart": c.smart,
            "media_type": c.media_type.value,
        }
        for c in matches
    ]


@router.get("/library-search", response_model=list[LibraryTitleOut])
async def library_search(request: Request, q: str = "") -> list[dict]:
    """Films and shows in the libraries whose title contains ``q``, for picking by hand."""
    query = q.strip()
    if len(query) < _MIN_QUERY:
        return []
    state = request.app.state
    titles = await _off_loop(lambda: _plex(state).search_titles(query, _LIBRARY_SEARCH_LIMIT), "library search")
    return [{"tmdb_id": t.tmdb_id, "media_type": t.media_type.value, "title": t.title, "year": t.year} for t in titles]


def _checked(session: Session, body: SeasonIn, *, editing: str | None) -> DateRule:
    """The season's date rule, once everything POST and PUT require of a season holds; 422 naming what doesn't.

    Args:
        session: For the names already taken.
        body: The season as sent.
        editing: The slug being replaced, whose own name is not a clash; None for a new season.
    """
    rule = DateRule(
        kind=body.rule.kind,
        month=body.rule.month,
        day=body.rule.day,
        nth=body.rule.nth,
        weekday=body.rule.weekday,
        offset=body.rule.offset,
    )
    try:
        rule.validate()
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    # Stored normalised, so an edit to a field the kind ignores changes nothing and moves no row (`_calendar`).
    rule = rule.normalised()
    if not (body.tags or body.genre is not None or body.collections or body.picks):
        raise HTTPException(status_code=422, detail="Add at least one tag, genre, collection or film.")
    # Every stored name, not just the catalogue's, and the built-ins': a row's title renders `{season}`, so two
    # seasons with one name would give two rows one title (D13).
    names = [(season.slug, season.name) for season in BUILTIN_SEASONS.values()]
    names += [(slug, name) for slug, name in session.execute(select(SeasonDef.slug, SeasonDef.name))]
    wanted = body.name.casefold()
    clash = next((name for slug, name in names if slug != editing and name.casefold() == wanted), None)
    if clash is not None:
        raise HTTPException(status_code=422, detail=f"There's already a season called “{clash}”.")
    return rule


def _columns(body: SeasonIn, rule: DateRule) -> dict:
    """Every `SeasonDef` column a save sets, sources de-duplicated. The slug and preset are the caller's."""
    return {
        "name": body.name,
        "emoji": body.emoji,
        "rule_kind": rule.kind,
        "month": rule.month,
        "day": rule.day,
        "nth": rule.nth,
        "weekday": rule.weekday,
        "easter_offset": rule.offset,
        "lead_days": 0 if rule.kind == "month" else body.lead_days,
        "after_days": 0 if rule.kind == "month" else body.after_days,
        "tags": [{"id": t.id, "name": t.name} for t in _unique(body.tags, lambda t: t.id)],
        "genre": body.genre,
        "excluded_genres": list(dict.fromkeys(body.excluded_genres)),
        "collections": [
            {"section_key": c.section_key, "section_title": c.section_title, "title": c.title}
            for c in _unique(body.collections, lambda c: (c.section_key, c.title))
        ],
        "picks": [
            {"tmdb_id": p.tmdb_id, "media_type": p.media_type, "title": p.title, "year": p.year}
            for p in _unique(body.picks, lambda p: (p.tmdb_id, p.media_type))
        ],
    }


def _unique[T](items: list[T], key: Callable[[T], object]) -> list[T]:
    """``items`` without repeats by ``key``, the first of each kept, in order."""
    kept: dict[object, T] = {}
    for item in items:
        kept.setdefault(key(item), item)
    return list(kept.values())


def _calendar(row: SeasonDef) -> tuple[DateRule, int, int]:
    """Everything that decides which days a season's rows are shown on. Normalised, so a field the rule's kind
    ignores never reads as a move."""
    rule = DateRule(row.rule_kind, row.month, row.day, row.nth, row.weekday, row.easter_offset)
    return (
        rule.normalised(),
        0 if rule.kind == "month" else row.lead_days,
        0 if rule.kind == "month" else row.after_days,
    )


def _draft(body: SeasonPreviewIn) -> Season:
    """The engine's view of a draft. Built field by field, never from an unsaved `SeasonDef`, whose int columns
    are None until the database fills its defaults."""
    return Season(
        slug="draft",
        name="Draft",
        emoji="",
        rule=DateRule(
            kind=body.rule.kind,
            month=body.rule.month,
            day=body.rule.day,
            nth=body.rule.nth,
            weekday=body.rule.weekday,
            offset=body.rule.offset,
        ),
        description="",
        keywords=tuple(dict.fromkeys(tag.id for tag in body.tags)),
        movie_genres=(body.genre,) if body.genre is not None else (),
        keyword_excluded_genres=tuple(dict.fromkeys(body.excluded_genres)),
        collections=tuple(dict.fromkeys(CollectionRef(c.section_key, c.title) for c in body.collections)),
        picks=tuple(dict.fromkeys((p.tmdb_id, MediaType(p.media_type)) for p in body.picks)),
    )


def _stored(session: Session, slug: str) -> SeasonDef:
    row = session.scalar(select(SeasonDef).where(SeasonDef.slug == slug))
    if row is None:
        raise HTTPException(status_code=404, detail="season not found")
    return row


def _row_name_in(session: Session, row: Collection, season: Season | None) -> str:
    """What the Rows page calls a row, with ``season`` filled into it.

    Anything said about one season names its rows as they read in that season: the Seasonal template's own
    name, ``{season_emoji} {season} picks``, reads "🦃 Thanksgiving picks" beside Thanksgiving, not as its
    placeholders.
    """
    name = row_display_name(session, row)
    return name if season is None else fill_season(name, naming_season(season))


def _used_by(session: Session, catalogue: seasons_mod.Catalogue) -> dict[str, list[dict]]:
    """Slug -> the rows that follow it, as ``{id, name}`` with that season filled into each name, in the Rows
    page's order."""
    used: dict[str, list[dict]] = {}
    for row in session.scalars(select(Collection).order_by(Collection.sort_order, Collection.id)):
        for slug in dict.fromkeys(row.seasons or []):
            used.setdefault(slug, []).append({"id": row.id, "name": _row_name_in(session, row, catalogue.get(slug))})
    return used


def _today(
    row: Collection, seasons: list[str], now: datetime, catalogue: seasons_mod.Catalogue
) -> tuple[bool, RowSeason | None]:
    """What a `rows.visibility` pass works from for a row today: whether it is shown, and the season (with its
    window) it builds for."""
    shown = row_shown_today(
        row.show_days, seasons, row.season_lead_days, row.season_after_days, now, catalogue=catalogue
    )
    season = seasons_mod.row_season_on(
        list(seasons), row.season_lead_days, row.season_after_days, now.date(), catalogue=catalogue
    )
    return shown, season


def _pass_owed(before: tuple[bool, RowSeason | None], after: tuple[bool, RowSeason | None]) -> bool:
    """Whether a season edit owes a row a `rows.visibility` pass now.

    Owed for a row shown before or after the edit whenever its shown-today answer, or the day or window of the
    season it shows, changed. The pass decides from the delivery ledger's own record of what the row's collection
    was built for (`RowSeason.holds`); this gate cannot, and guessing from the season's day before the edit
    missed a second edit made before the next run (10 to 17 to 24 June was judged against the 17th, a day the
    row was never built for). A rename or an edit to a field the rule's kind ignores moves no day, and a row
    hidden before and after needs no pass.
    """
    (shown_before, season_before), (shown_after, season_after) = before, after
    if not (shown_before or shown_after):
        return False
    return shown_before != shown_after or _when(season_before) != _when(season_after)


def _when(season: RowSeason | None) -> tuple | None:
    """The season a row shows, its day and its window: everything a season edit can move for the row. Spelled
    out because `RowSeason` equality leaves the window out."""
    return None if season is None else (season.slug, season.anchor, season.starts, season.ends)


def _reject_row_title_clashes(session: Session, state: State, season: Season) -> None:
    """422 when ``season`` would title a row what another row is already titled, in a library both can build in.

    A row named ``{season}`` is titled after whichever season it shows, so a new or renamed season can give
    it the title of a plain row beside it, and delivery would then write both rows into one Plex collection
    (#137 I-2). The row editor refuses such a name (`collections._reject_duplicate_name`); this is the same
    check, `collection_reconcile.rows_titled_from`, on each seasonal row's title in this season. Every row
    named after its season, not only those that tick this one: any of them may tick it later. The season is
    already in ``session``, so the rows checked against see its name too.
    """
    clashes: list[tuple[str, str, str]] = []
    for row in session.scalars(select(Collection).order_by(Collection.sort_order, Collection.id)):
        template = reconcile.row_template(session, row.slug, state.secrets)
        if not uses_season(template):
            continue
        title = reconcile.season_title(template, season)
        for other in reconcile.rows_titled_from(
            session,
            title,
            secrets=state.secrets,
            exclude_slug=row.slug,
            build=row.build or "",
            media=row.media or "both",
            library_keys=row.library_keys or [],
        ):
            clashes.append((row_display_name(session, row), title, row_display_name(session, other)))
    if not clashes:
        return
    which = "; and ".join(f"“{row}” “{title}”, the title “{other}” already has" for row, title, other in clashes)
    raise HTTPException(
        status_code=422,
        detail=(
            f"This season would title {which}, in a library they can share. Two rows with one title in one "
            "library become a single collection on Plex: choose another name or emoji for the season, or "
            "rename one of those rows."
        ),
    )


def _month_windows(season: Season, today: date) -> list[dict[str, str]]:
    """Keep leap-year and month-boundary arithmetic on the server, alongside the run's calendar."""
    if season.rule.kind != "month":
        return []
    return [
        {"start": anchor.replace(day=1).isoformat(), "end": anchor.isoformat()}
        for anchor in seasons_mod.next_anchors(season, today)
    ]


def _season_view(season: Season, stored: SeasonDef | None, used_by: dict[str, list[dict]], today: date) -> dict:
    """A `SeasonOut`. ``stored`` is the custom season's row, for its sources; None for a built-in."""
    view = {
        "slug": season.slug,
        "name": season.name,
        "emoji": season.emoji,
        "description": season.description,
        "builtin": season.builtin,
        "rule": dataclasses.asdict(season.rule),
        # Safe: every season in the catalogue has a rule that validated (`season_from_row`).
        "rule_label": season.rule.label(),
        "next_dates": [day.isoformat() for day in seasons_mod.next_anchors(season, today)],
        "next_windows": _month_windows(season, today),
        "lead_days": season.lead_days,
        "after_days": season.after_days,
        "preset": None,
        "tags": [],
        "genre": None,
        "excluded_genres": [],
        "collections": [],
        "picks": [],
        "used_by": used_by.get(season.slug, []),
    }
    if stored is not None:
        view.update(
            preset=stored.preset,
            tags=stored.tags,
            genre=stored.genre,
            excluded_genres=stored.excluded_genres,
            collections=stored.collections,
            picks=stored.picks,
        )
    return view


def _preset_view(preset: Preset) -> dict:
    season = preset.season
    return {
        "key": preset.key,
        "label": preset.label,
        "note": preset.note,
        "category": preset.category,
        "description": preset.description,
        "preset": preset.key,
        "name": season.name,
        "emoji": season.emoji,
        "rule": dataclasses.asdict(season.rule),
        "lead_days": season.lead_days,
        "after_days": season.after_days,
        "tags": [{"id": tag, "name": PRESET_TAG_NAMES[tag]} for tag in season.keywords],
        "genre": season.movie_genres[0] if season.movie_genres else None,
        "excluded_genres": list(season.keyword_excluded_genres),
        "collections": [],
        "picks": [
            {"tmdb_id": pick.tmdb_id, "media_type": "movie", "title": pick.title, "year": pick.year}
            for pick in preset.picks
        ],
    }


def _and_list(names: list[str]) -> str:
    """“A”, “A” and “B”, “A”, “B” and “C”."""
    quoted = [f"“{name}”" for name in names]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"


def _plex(state: State) -> PlexClient:
    """The owner's PMS (this connects: call it off the event loop), or 503 before setup."""
    plex = state.run_service.build_plex_reader()
    if plex is None:
        raise HTTPException(status_code=503, detail=_NO_PLEX)
    return plex


async def _off_loop[T](read: Callable[[], T], what: str) -> T:
    """``read()`` in a worker thread. A TMDB or Plex failure is a 502 whose message carries no credential."""
    try:
        return await asyncio.get_running_loop().run_in_executor(None, read)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("{} failed ({})", what, type(e).__name__)
        raise HTTPException(status_code=502, detail=redact(f"{type(e).__name__}: {e}")) from e
