"""Themes (#138) as stored: the `themes` row read back as the engine's `ThemeSpec`, and its slug."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, RowLimits, slugify
from shortlist.engine.placeholders import uses_theme
from shortlist.engine.themes import ThemeCollection, ThemePick, ThemeSpec, theme_content_hash
from shortlist.server.db.models import Collection, Theme
from shortlist.server.services.audit import add_audit

if TYPE_CHECKING:
    from shortlist.server.api.themes import ThemeSaveIn
    from shortlist.server.services.theme_author import ThemeDiff

__all__ = [
    "RowPaused",
    "ThemeStoreError",
    "TitleClash",
    "audit_theme_build",
    "charge_tokens",
    "pick_titles",
    "reject_title_clashes",
    "save_theme",
    "spec_from_row",
    "unique_slug",
    "write_theme",
]

_STATS_KEYS = ("named", "resolved", "in_library", "after_rules")


class ThemeStoreError(Exception):
    """A theme save the store refused. The API maps each subclass to an HTTP status; the rotation job logs it."""


class RowPaused(ThemeStoreError):
    """The row's AI is paused and the save spent tokens."""


class TitleClash(ThemeStoreError):
    """Saving would title a row what another row in a shared library is already titled."""


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


def _unique[T](items: list[T], key) -> list[T]:
    kept: dict[object, T] = {}
    for item in items:
        kept.setdefault(key(item), item)
    return list(kept.values())


def write_theme(row: Theme, body: ThemeSaveIn) -> None:
    """Set every content column from the request and the hash from those columns — never from the request."""
    draft = body.draft
    row.name = draft.name
    row.origin = draft.origin
    row.emoji = draft.emoji or None
    row.brief = draft.brief
    row.media = list(dict.fromkeys(draft.media))
    row.tags = [{"id": t.id, "name": t.name} for t in _unique(draft.tags, lambda t: t.id)]
    row.genres = list(dict.fromkeys(g.strip() for g in draft.genres))
    row.excluded_genres = list(dict.fromkeys(g.strip() for g in draft.excluded_genres))
    row.collections = [
        {"section_key": c.section_key, "section_title": c.section_title, "title": c.title}
        for c in _unique(draft.collections, lambda c: (c.section_key, c.title))
    ]
    row.picks = [
        {
            "tmdb_id": p.tmdb_id,
            "media": p.media,
            "origin": p.origin,
            "reason": p.reason,
            "title": p.title,
            "year": p.year,
        }
        for p in _unique(draft.picks, lambda p: (p.tmdb_id, p.media))
    ]
    row.rules = {k: v for k, v in draft.rules.model_dump().items() if v is not None}
    row.content_hash = theme_content_hash(spec_from_row(row))
    if body.stats:
        row.stats = {k: int(body.stats[k]) for k in _STATS_KEYS if k in body.stats}


def audit_theme_build(session: Session, row: Theme, body: ThemeSaveIn, *, diff: ThemeDiff | None) -> None:
    add_audit(
        session,
        "theme.build",
        "info",
        theme=row.slug,
        name=row.name,
        origin=row.origin,
        collection_id=body.collection_id,
        tokens=body.tokens,
        picks=len(row.picks),
        diff=None if diff is None else dataclasses.asdict(diff),
    )


def reject_title_clashes(session: Session, secrets, theme: Theme) -> None:
    """Raise `TitleClash` when renaming a theme would title a row what another row is already titled, in a
    library both share.

    The theme is already flushed with its new name, so every row on it is checked as it would now be titled.
    """
    # Imported here: the collections API imports this package.
    from shortlist.server.api import collections as collections_api

    spec = spec_from_row(theme)
    for row in session.query(Collection).filter(Collection.theme_id == theme.id):
        template = row.name_template or row.name
        if not uses_theme(template):
            continue
        try:
            collections_api._reject_duplicate_name(
                session,
                secrets,
                template,
                exclude_slug=row.slug,
                build=row.build or "",
                fallback_name=row.fallback_name or "",
                media=row.media or "both",
                library_keys=row.library_keys or [],
                theme=spec,
            )
        except HTTPException as e:
            raise TitleClash(str(e.detail)) from None


def charge_tokens(session: Session, body: ThemeSaveIn) -> Collection | None:
    """The row a save's tokens are charged to; `RowPaused` when it is paused and the save spent any."""
    if body.collection_id is None:
        return None
    collection = session.get(Collection, body.collection_id)
    if collection is None:
        raise LookupError("collection not found")
    if collection.ai_paused and body.tokens > 0:
        raise RowPaused
    return collection


def save_theme(
    session: Session,
    secrets,
    body: ThemeSaveIn,
    *,
    existing: Theme | None = None,
    diff: ThemeDiff | None = None,
) -> Theme:
    """Store a theme: a new one (unique slug) or ``existing`` rewritten in place. The caller owns the commit.

    Charges the save's tokens to the row (`Collection.ai_tokens`) and to the theme, and writes the audit event.
    """
    collection = charge_tokens(session, body)
    before = spec_from_row(existing) if existing is not None else None
    before_titles = pick_titles(existing) if existing is not None else {}
    if existing is None:
        row = Theme(slug=unique_slug(session, body.draft.name), origin=body.draft.origin, ai_tokens=body.tokens)
        write_theme(row, body)
        session.add(row)
    else:
        row = existing
        write_theme(row, body)
        row.ai_tokens = (row.ai_tokens or 0) + body.tokens
    session.flush()
    reject_title_clashes(session, secrets, row)
    if collection is not None:
        collection.ai_tokens += body.tokens
    if before is not None and diff is None:
        # An in-place rewrite always says what it changed.
        from shortlist.server.services.theme_author import diff_themes

        diff = diff_themes(
            before, spec_from_row(row), {**before_titles, **pick_titles(row)}, {t["id"]: t["name"] for t in row.tags}
        )
    audit_theme_build(session, row, body, diff=diff)
    return row
