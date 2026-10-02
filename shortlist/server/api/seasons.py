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
from shortlist.engine.models import MediaType
from shortlist.engine.placeholders import uses_season
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
from shortlist.server.services.library_index import library_index
from shortlist.server.services.season_catalogue import load_catalogue, make_slug, season_from_row

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

    kind: Literal["fixed", "nth", "easter"]
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
    #: None for a built-in, which follows its row's "Built-in seasons show from…" timing.
    lead_days: int | None
    after_days: int | None
    preset: str | None
    used_by: list[UsedByOut]


class PresetOut(SeasonIn):
    """A ready-made season: a `SeasonIn` the editor opens pre-filled. ``preset`` is ``key``."""

    model_config = ConfigDict(extra="allow")

    key: str
    #: What the editor calls the preset, region included: "Mother's Day (US, CA, AU, NZ)". ``name`` has none,
    #: because a row's title renders it.
    label: str
    #: What to add when TMDB's tags fall short, e.g. "TMDB tags very few films as Father's Day — add a…".
    note: str


class SeasonPreviewIn(SeasonSourcesIO):
    """A draft season to count, for the row the editor was opened from. Undeclared fields are let through, not
    refused: the editor posts its whole draft, and a preview stores nothing that a misspelt field could
    silently fail to set."""

    rule: DateRuleIO
    #: The row's ``media``: only titles of its type count, because a films row never draws a show.
    media: Literal["movie", "show", "both"] = "both"
    #: The row's ``library_keys``: only titles in those libraries count. Empty is every library of its type.
    library_keys: list[str] = Field(default_factory=list)


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
        used_by = _used_by(session)
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
    if body.preset is not None and body.preset not in {preset.key for preset in PRESETS}:
        raise HTTPException(status_code=422, detail=f"There's no ready-made season “{body.preset}”.")
    state = request.app.state
    today = context_builder.local_now().date()
    with state.sessions() as session:
        rule = _checked(session, body, editing=None)
        # Every stored slug, not the catalogue's: a stored season the catalogue skips still owns its slug.
        taken = set(session.scalars(select(SeasonDef.slug)))
        row = SeasonDef(slug=make_slug(body.name, taken), preset=body.preset, **_columns(body, rule))
        session.add(row)
        session.flush()
        _reject_row_title_clashes(session, state, season_from_row(row))
        session.commit()
        return _season_view(season_from_row(row), row, {}, today)


@router.put("/{slug}", response_model=SeasonOut)
async def update_season(slug: str, body: SeasonIn, request: Request) -> dict:
    """Replace a custom season, keeping its slug.

    A change of date or timing changes which days its rows are shown on, so it is applied to Plex now, as a
    change to a row's own seasons is. A source change rebuilds its rows on their next build (D11).
    """
    if slug in BUILTIN_SEASONS:
        raise HTTPException(status_code=403, detail="Built-in seasons can't be edited.")
    state = request.app.state
    now = context_builder.local_now()
    with state.sessions() as session:
        row = _stored(session, slug)
        rule = _checked(session, body, editing=slug)
        before = _calendar(row)
        titled_before = (row.name, row.emoji)
        catalogue_before = load_catalogue(session)
        for column, value in _columns(body, rule).items():
            setattr(row, column, value)
        moved = _calendar(row) != before
        if (row.name, row.emoji) != titled_before:
            # Only a new name or emoji retitles a row: re-checking an unchanged one would refuse a date or
            # source edit over a clash this edit did not make.
            session.flush()
            _reject_row_title_clashes(session, state, season_from_row(row))
        session.commit()
        used_by = _used_by(session)
        catalogue_after = {**catalogue_before, slug: season_from_row(row)}
        following = _enabled_followers(session, slug) if moved else []
        changed = [
            c.slug
            for c in following
            if _today(c, c.seasons, now, catalogue_before) != _today(c, c.seasons, now, catalogue_after)
        ]
        view = _season_view(season_from_row(row), row, used_by, now.date())
    if changed:
        _apply_visibility(state, changed, f"season '{slug}' moved")
    return view


@router.delete("/{slug}", status_code=204)
async def delete_season(slug: str, request: Request) -> Response:
    """Delete a custom season and untick it in every row, in one transaction (D12).

    Refused, naming the rows, while it is any row's only season: that row would be left following nothing.
    """
    if slug in BUILTIN_SEASONS:
        raise HTTPException(status_code=403, detail="Built-in seasons can't be deleted.")
    now = context_builder.local_now()
    with request.app.state.sessions() as session:
        row = _stored(session, slug)
        catalogue_before = load_catalogue(session)
        catalogue_after = {key: season for key, season in catalogue_before.items() if key != slug}
        following = [
            c
            for c in session.scalars(select(Collection).order_by(Collection.sort_order, Collection.id))
            if slug in (c.seasons or [])
        ]
        alone = [row_display_name(session, c) for c in following if set(c.seasons) == {slug}]
        if alone:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"“{row.name}” is the only season in {_and_list(alone)}. "
                    "Give those rows another season, or delete them, first."
                ),
            )
        changed = [
            c.slug
            for c in following
            if c.enabled
            and _today(c, c.seasons, now, catalogue_before)
            != _today(c, [s for s in c.seasons if s != slug], now, catalogue_after)
        ]
        for collection in following:
            collection.seasons = [s for s in collection.seasons if s != slug]
        session.delete(row)
        session.commit()
    if changed:
        _apply_visibility(request.app.state, changed, f"season '{slug}' was deleted")
    return Response(status_code=204)


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

    def count() -> seasons_mod.SeasonPreview:
        plex = _plex(state)
        index = library_index(plex, state.sessions, media=body.media, library_keys=body.library_keys)
        return seasons_mod.preview(tmdb, plex, draft, index, today=today, workers=_PREVIEW_PAGE_WORKERS)

    result = await _off_loop(count, "season preview")
    return {
        **dataclasses.asdict(result),
        "next_date": result.next_date.isoformat() if result.next_date else None,
        "per_collection": [dataclasses.asdict(c) for c in result.per_collection],
        "sample": list(result.sample),
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
        raise HTTPException(status_code=422, detail="Add at least one tag, collection or film.")
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
        "lead_days": body.lead_days,
        "after_days": body.after_days,
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
    return rule.normalised(), row.lead_days, row.after_days


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


def _used_by(session: Session) -> dict[str, list[dict]]:
    """Slug -> the rows that follow it, as ``{id, name}``, in the Rows page's order."""
    used: dict[str, list[dict]] = {}
    for row in session.scalars(select(Collection).order_by(Collection.sort_order, Collection.id)):
        for slug in dict.fromkeys(row.seasons or []):
            used.setdefault(slug, []).append({"id": row.id, "name": row_display_name(session, row)})
    return used


def _enabled_followers(session: Session, slug: str) -> list[Collection]:
    """The enabled rows that follow the season, in the Rows page's order."""
    rows = session.scalars(
        select(Collection).where(Collection.enabled.is_(True)).order_by(Collection.sort_order, Collection.id)
    )
    return [row for row in rows if slug in (row.seasons or [])]


def _today(row: Collection, seasons: list[str], now: datetime, catalogue: seasons_mod.Catalogue) -> tuple:
    """What a `rows.visibility` pass applies to a row today: whether it is shown, and which season it is built
    for (a collection built for another is kept hidden). A season edit that changes neither needs no pass."""
    shown = row_shown_today(
        row.show_days, seasons, row.season_lead_days, row.season_after_days, now, catalogue=catalogue
    )
    season = seasons_mod.row_season_on(
        list(seasons), row.season_lead_days, row.season_after_days, now.date(), catalogue=catalogue
    )
    return shown, season.built_for if season else None


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
        "picks": [],
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


def _apply_visibility(state: State, rows: list[str], reason: str) -> None:
    """Re-apply today's shown-or-hidden to these rows now, as the row editor does when a row's seasons change.

    One `rows.visibility` pass per row, named, because the pass's own gate looks only at rows whose answer
    changed in the past week — and it recomputes with today's catalogue, in which these rows' past days have
    changed too. The handler promotes one named row at a time (`promote_user_rows(only_row=…)`), so the rows
    cannot share a pass. Each pass merges every account's excludes before it promotes anything (plex-safety
    rule 1).

    Not awaited: each pass is a whole privacy sync, and the season is saved, which is what the editor is
    waiting to hear. The jobs are durable and retried, and show in the header's activity popover.
    """
    for slug in rows:
        jobs.enqueue(state.sessions, "rows.visibility", {"row": slug})
    jobs.drain_in_background(state, reason)
