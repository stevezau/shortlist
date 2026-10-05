"""The season catalogue a request, job or run sees: the built-ins, then the owner's own (issue #137)."""

from __future__ import annotations

import dataclasses
import re
import unicodedata

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType
from shortlist.engine.seasons import BUILTIN_SEASONS, CollectionRef, DateRule, Season, season_content_hash
from shortlist.server.db.models import SeasonDef

#: `seasons.slug` is String(64); this leaves room for a "-N" suffix of up to seven digits.
_MAX_SLUG_BASE = 56


def load_catalogue(session: Session) -> dict[str, Season]:
    """Every season a row may follow. Built once per request, job or run and passed down explicitly.

    The built-ins come first, then the owner's seasons in the order they were added. A stored season
    that no longer makes sense is left out with a warning rather than failing every run; rows that follow
    it go dormant, as for any slug the catalogue does not know.
    """
    catalogue = dict(BUILTIN_SEASONS)
    for row in session.scalars(select(SeasonDef).order_by(SeasonDef.id)):
        if row.slug in BUILTIN_SEASONS:
            # Built-ins are not editable (D2); a stored row under a built-in's slug was written by hand.
            logger.warning("season {!r} is left out of the catalogue: a built-in season has that slug", row.slug)
            continue
        try:
            catalogue[row.slug] = season_from_row(row)
        except (ValueError, KeyError, TypeError) as e:
            logger.warning(
                "season {!r} is left out of the catalogue: its stored definition is invalid ({})", row.slug, e
            )
    return catalogue


def season_from_row(row: SeasonDef) -> Season:
    """The engine's view of an owner-defined season.

    Args:
        row: A stored season.

    Returns:
        The season, with its own timing and a content hash over its sources.

    Raises:
        ValueError: The date rule cannot name a day every year, or a pick's media type is unknown.
        KeyError: A JSON source entry is missing a field.
        TypeError: A JSON source entry is not the shape it should be.
    """
    rule = DateRule(
        kind=row.rule_kind,  # checked by validate() below
        month=row.month,
        day=row.day,
        nth=row.nth,
        weekday=row.weekday,
        offset=row.easter_offset,
    )
    rule.validate()
    season = Season(
        slug=row.slug,
        name=row.name,
        emoji=row.emoji,
        rule=rule,
        description="",
        keywords=tuple(int(tag["id"]) for tag in row.tags),
        movie_genres=(row.genre,) if row.genre is not None else (),
        keyword_excluded_genres=tuple(int(genre) for genre in row.excluded_genres),
        collections=tuple(CollectionRef(section_key=str(c["section_key"]), title=c["title"]) for c in row.collections),
        picks=tuple((int(p["tmdb_id"]), MediaType(p["media_type"])) for p in row.picks),
        lead_days=row.lead_days,
        after_days=row.after_days,
        builtin=False,
    )
    return dataclasses.replace(season, content_hash=season_content_hash(season))


def make_slug(name: str, taken: set[str]) -> str:
    """A season's permanent slug, made from its name: "St Patrick's Day" -> "st-patricks-day".

    Args:
        name: The season's name as the owner typed it.
        taken: Every slug already stored. The built-ins' are reserved here whatever the caller passes: a
            custom "Hallowe'en" stored as `halloween` would be read as the built-in by every row.

    Returns:
        The slug, with "-2", "-3"... appended while it is taken; "season" when the name has no letters
        or digits at all (an emoji alone). At most 64 characters, the column's width: one character can
        decompose into several letters ("℡" is "tel"), so a 40-character name can make a longer slug.
    """
    taken = taken | BUILTIN_SEASONS.keys()
    text = name.replace("'", "").replace("\N{RIGHT SINGLE QUOTATION MARK}", "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:_MAX_SLUG_BASE].rstrip("-") or "season"
    slug, n = base, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug
