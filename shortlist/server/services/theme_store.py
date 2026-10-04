"""Themes (#138) as stored: the `themes` row read back as the engine's `ThemeSpec`, and its slug."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, RowLimits, slugify
from shortlist.engine.themes import ThemeCollection, ThemePick, ThemeSpec
from shortlist.server.db.models import Theme

__all__ = ["pick_titles", "spec_from_row", "unique_slug"]


def spec_from_row(row: Theme) -> ThemeSpec:
    """The theme as the engine reads it. Collections keep the real library section key they were saved with."""
    rules = row.rules or {}
    min_rating = rules.get("min_rating")
    return ThemeSpec(
        slug=row.slug,
        name=row.name,
        emoji=row.emoji,
        media=tuple(MediaType(m) for m in row.media or []),
        tags=tuple(int(t["id"]) for t in row.tags or []),
        genres=tuple(row.genres or []),
        excluded_genres=tuple(row.excluded_genres or []),
        collections=tuple(
            ThemeCollection(section_key=str(c["section_key"]), title=str(c["title"])) for c in row.collections or []
        ),
        picks=tuple(
            ThemePick(
                tmdb_id=int(p["tmdb_id"]),
                media=MediaType(p["media"]),
                origin=str(p.get("origin") or "owner"),
                reason=p.get("reason") or None,
            )
            for p in row.picks or []
        ),
        rules=RowLimits(
            max_runtime=rules.get("max_runtime"),
            min_year=rules.get("min_year"),
            max_year=rules.get("max_year"),
            # float(): RowLimits.fingerprint formats it as given, and 7 vs 7.0 must not change the hash.
            min_rating=None if min_rating is None else float(min_rating),
        ),
        min_votes=rules.get("min_votes"),
    )


def pick_titles(row: Theme) -> dict[tuple[MediaType, int], str]:
    """The titles a theme stored beside its picks, by (media, id). A pick saved without one is left out."""
    return {(MediaType(p["media"]), int(p["tmdb_id"])): p["title"] for p in row.picks or [] if p.get("title")}


def unique_slug(session: Session, name: str) -> str:
    """A slug made from ``name``, or ``…-2``, ``…-3`` — the first no stored theme has."""
    base = slugify(name).replace("_", "-") or "theme"
    taken = set(session.scalars(select(Theme.slug)))
    slug, n = base, 2
    while slug in taken:
        slug = f"{base}-{n}"
        n += 1
    return slug
