"""Themes (#138) as stored: the `themes` row read back as the engine's `ThemeSpec`, and its slug."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, RowLimits, slugify
from shortlist.engine.placeholders import uses_theme
from shortlist.engine.themes import ThemeCollection, ThemePick, ThemeSpec, theme_content_hash
from shortlist.server.db.models import Collection, Theme, ThemeHistory, User
from shortlist.server.services.audit import add_audit

if TYPE_CHECKING:
    from shortlist.server.api.themes import ThemeSaveIn
    from shortlist.server.services.theme_author import ThemeDiff

__all__ = [
    "RowPaused",
    "ThemeStoreError",
    "TitleClash",
    "add_row_tokens",
    "audit_theme_build",
    "charge_tokens",
    "pick_titles",
    "reject_person_title_clash",
    "reject_title_clashes",
    "save_theme",
    "spec_from_row",
    "unique_slug",
    "write_theme",
]

_STATS_KEYS = ("named", "resolved", "in_library", "after_rules", "ai_kept")


class ThemeStoreError(Exception):
    """A theme save the store refused. The API maps each subclass to an HTTP status; the rotation job logs it."""


class RowPaused(ThemeStoreError):
    """The row's AI is paused and the save spent tokens."""


class TitleClash(ThemeStoreError):
    """Saving would title a row what another row in a shared library is already titled.

    ``theme_name`` is the theme that caused it when a person's theme did, so a retry can steer clear of it.
    """

    def __init__(self, message: str = "", *, theme_name: str = "") -> None:
        super().__init__(message)
        self.theme_name = theme_name


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
    # A person's explore row is titled from their own theme and no row follows it as its base theme.
    held = session.query(ThemeHistory).filter(
        ThemeHistory.theme_id == theme.id, ThemeHistory.state.in_(("current", "next"))
    )
    for entry in held:
        collection = session.get(Collection, entry.collection_id)
        if collection is not None and collection.theme_mode == "explore":
            reject_person_title_clash(session, secrets, collection, entry.user_id, spec)


def reject_person_title_clash(session: Session, secrets, collection: Collection, user_id: int, theme) -> None:
    """Raise `TitleClash` when ``theme`` would title ``collection`` for this person what another of their rows
    already wears in a library the two share. ``theme`` is a `ThemeSpec` or a stored `Theme`."""
    # Imported here: the reconcile service imports this module.
    from shortlist.server.services import collection_reconcile

    person = session.get(User, user_id)
    if person is None:
        return
    spec = theme if isinstance(theme, ThemeSpec) else spec_from_row(theme)
    clash = collection_reconcile.person_title_clash(session, secrets, collection, person, spec)
    if clash is not None:
        raise TitleClash(
            f"The theme {spec.name!r} would title this row the same as the row {clash.name!r} ({clash.slug}) "
            "for this person, in a library both build in. Two rows with one title become one collection on Plex.",
            theme_name=spec.name,
        )


def charge_tokens(session: Session, body: ThemeSaveIn) -> Collection | None:
    """The row a save's tokens are charged to; `RowPaused` when it is paused and the save spent any."""
    if body.collection_id is None:
        return None
    # populate_existing: the AI call that came before this can take a minute, and the owner may have paused since.
    collection = session.get(Collection, body.collection_id, populate_existing=True)
    if collection is None:
        raise LookupError("collection not found")
    if collection.ai_paused and body.tokens > 0:
        raise RowPaused
    return collection


def add_row_tokens(session: Session, collection: Collection, tokens: int) -> None:
    """Charge ``tokens`` to a row in SQL, not as `ai_tokens += n` on a possibly stale read: a concurrent charge
    (a rotation pass and a regenerate) would otherwise be lost."""
    session.execute(
        update(Collection).where(Collection.id == collection.id).values(ai_tokens=Collection.ai_tokens + tokens)
    )
    session.refresh(collection)


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
    if collection is not None and body.tokens:
        add_row_tokens(session, collection, body.tokens)
    if before is not None and diff is None:
        # An in-place rewrite always says what it changed.
        from shortlist.server.services.theme_author import diff_themes

        diff = diff_themes(
            before, spec_from_row(row), {**before_titles, **pick_titles(row)}, {t["id"]: t["name"] for t in row.tags}
        )
    audit_theme_build(session, row, body, diff=diff)
    return row
