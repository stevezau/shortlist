"""What saving a season checks and derives, shared by the seasons API and the assistant.

Lives below both transports so neither imports the other; the request bodies are passed in as the models the
API defines, read only through their fields.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import State

from shortlist.engine import seasons as seasons_mod
from shortlist.engine.models import RowSeason
from shortlist.engine.placeholders import fill_season, naming_season, uses_season
from shortlist.engine.rows import row_shown_today
from shortlist.engine.seasons import BUILTIN_SEASONS, DateRule, Season
from shortlist.server.db.models import Collection, SeasonDef
from shortlist.server.services import collection_reconcile as reconcile
from shortlist.server.services.row_views import row_display_name


def checked_rule(session: Session, body: BaseModel, *, editing: str | None) -> DateRule:
    """The season's date rule, once everything POST and PUT require of a season holds; 422 naming what doesn't.

    Args:
        session: For the names already taken.
        body: The season as sent (a `SeasonIn`).
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
    # Stored normalised, so an edit to a field the kind ignores changes nothing and moves no row (`calendar_of`).
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


def season_columns(body: BaseModel, rule: DateRule) -> dict:
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
        "tags": [{"id": t.id, "name": t.name} for t in unique_by(body.tags, lambda t: t.id)],
        "genre": body.genre,
        "excluded_genres": list(dict.fromkeys(body.excluded_genres)),
        "collections": [
            {"section_key": c.section_key, "section_title": c.section_title, "title": c.title}
            for c in unique_by(body.collections, lambda c: (c.section_key, c.title))
        ],
        "picks": [
            {"tmdb_id": p.tmdb_id, "media_type": p.media_type, "title": p.title, "year": p.year}
            for p in unique_by(body.picks, lambda p: (p.tmdb_id, p.media_type))
        ],
    }


def unique_by[T](items: list[T], key: Callable[[T], object]) -> list[T]:
    """``items`` without repeats by ``key``, the first of each kept, in order."""
    kept: dict[object, T] = {}
    for item in items:
        kept.setdefault(key(item), item)
    return list(kept.values())


def calendar_of(row: SeasonDef) -> tuple[DateRule, int, int]:
    """Everything that decides which days a season's rows are shown on. Normalised, so a field the rule's kind
    ignores never reads as a move."""
    rule = DateRule(row.rule_kind, row.month, row.day, row.nth, row.weekday, row.easter_offset)
    return (
        rule.normalised(),
        0 if rule.kind == "month" else row.lead_days,
        0 if rule.kind == "month" else row.after_days,
    )


def stored_season(session: Session, slug: str) -> SeasonDef:
    row = session.scalar(select(SeasonDef).where(SeasonDef.slug == slug))
    if row is None:
        raise HTTPException(status_code=404, detail="season not found")
    return row


def row_name_in(session: Session, row: Collection, season: Season | None) -> str:
    """What the Rows page calls a row, with ``season`` filled into it.

    Anything said about one season names its rows as they read in that season: the Seasonal template's own
    name, ``{season_emoji} {season} picks``, reads "🦃 Thanksgiving picks" beside Thanksgiving, not as its
    placeholders.
    """
    name = row_display_name(session, row)
    return name if season is None else fill_season(name, naming_season(season))


def rows_by_season(session: Session, catalogue: seasons_mod.Catalogue) -> dict[str, list[dict]]:
    """Slug -> the rows that follow it, as ``{id, name}`` with that season filled into each name, in the Rows
    page's order."""
    used: dict[str, list[dict]] = {}
    for row in session.scalars(select(Collection).order_by(Collection.sort_order, Collection.id)):
        for slug in dict.fromkeys(row.seasons or []):
            used.setdefault(slug, []).append({"id": row.id, "name": row_name_in(session, row, catalogue.get(slug))})
    return used


def shown_today(
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


def pass_owed(before: tuple[bool, RowSeason | None], after: tuple[bool, RowSeason | None]) -> bool:
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
    return shown_before != shown_after or season_when(season_before) != season_when(season_after)


def season_when(season: RowSeason | None) -> tuple | None:
    """The season a row shows, its day and its window: everything a season edit can move for the row. Spelled
    out because `RowSeason` equality leaves the window out."""
    return None if season is None else (season.slug, season.anchor, season.starts, season.ends)


def reject_row_title_clashes(session: Session, state: State, season: Season) -> None:
    """422 when ``season`` would title a row what another row is already titled, in a library both can build in.

    A row named ``{season}`` is titled after whichever season it shows, so a new or renamed season can give
    it the title of a plain row beside it, and delivery would then write both rows into one Plex collection
    (#137 I-2). The row editor refuses such a name (`row_editing.reject_duplicate_name`); this is the same
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


def month_windows(season: Season, today: date) -> list[dict[str, str]]:
    """Keep leap-year and month-boundary arithmetic on the server, alongside the run's calendar."""
    if season.rule.kind != "month":
        return []
    return [
        {"start": anchor.replace(day=1).isoformat(), "end": anchor.isoformat()}
        for anchor in seasons_mod.next_anchors(season, today)
    ]


def season_view(season: Season, stored: SeasonDef | None, used_by: dict[str, list[dict]], today: date) -> dict:
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
        "next_windows": month_windows(season, today),
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


def and_list(names: list[str]) -> str:
    """“A”, “A” and “B”, “A”, “B” and “C”."""
    quoted = [f"“{name}”" for name in names]
    return quoted[0] if len(quoted) == 1 else f"{', '.join(quoted[:-1])} and {quoted[-1]}"
