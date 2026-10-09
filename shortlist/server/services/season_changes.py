"""Calendar mutations and their visibility obligations within the caller's transaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from shortlist.engine.seasons import BUILTIN_SEASONS, PRESETS
from shortlist.server.db.models import Collection, SeasonDef
from shortlist.server.services import context_builder
from shortlist.server.services.season_catalogue import load_catalogue, make_slug, season_from_row
from shortlist.server.services.season_rules import (
    and_list,
    calendar_of,
    checked_rule,
    pass_owed,
    reject_row_title_clashes,
    row_name_in,
    rows_by_season,
    season_columns,
    season_view,
    shown_today,
    stored_season,
)


@dataclass(frozen=True)
class SeasonMutation:
    action: Literal["create", "update", "delete"]
    slug: str
    columns: dict
    preset: str | None
    following_ids: tuple[int, ...]
    steps: tuple[dict, ...]
    retitled: bool = False


def prepare_season_in_session(session: Session, action: str, *, slug: str | None = None, body=None) -> SeasonMutation:
    """Project the calendar before and after without dirtying ORM state or doing I/O."""
    from shortlist.server.assistant.row_effects import visibility_step

    if action not in {"create", "update", "delete"}:
        raise HTTPException(status_code=422, detail="unknown season action")
    if action != "create" and slug in BUILTIN_SEASONS:
        raise HTTPException(
            status_code=403, detail=f"Built-in seasons can't be {'deleted' if action == 'delete' else 'edited'}."
        )
    before = load_catalogue(session)
    now = context_builder.local_now()
    row = None if action == "create" else stored_season(session, slug)
    following = (
        [c for c in session.scalars(select(Collection).order_by(Collection.id)) if slug in (c.seasons or [])]
        if row
        else []
    )
    columns = {}
    preset = row.preset if row else None
    retitled = False
    if action == "delete":
        alone = [row_name_in(session, c, before[slug]) for c in following if set(c.seasons) == {slug}]
        if alone:
            advice = (
                "Give that row another season, or delete it, first."
                if len(alone) == 1
                else "Give those rows another season, or delete them, first."
            )
            raise HTTPException(
                status_code=409, detail=f"“{row.name}” is the only season in {and_list(alone)}. {advice}"
            )
        after = {key: value for key, value in before.items() if key != slug}
        moved = True
    else:
        if body is None:
            raise HTTPException(status_code=422, detail="season definition required")
        if action == "create" and body.preset is not None and body.preset not in {p.key for p in PRESETS}:
            raise HTTPException(status_code=422, detail="Unknown ready-made season.")
        columns = season_columns(body, checked_rule(session, body, editing=slug if row else None))
        if row is None:
            slug = make_slug(body.name, set(session.scalars(select(SeasonDef.slug))))
            preset = body.preset
        projected = SeasonDef(slug=slug, preset=preset, **columns)
        after = {**before, slug: season_from_row(projected)}
        moved = row is not None and calendar_of(row) != calendar_of(projected)
        retitled = row is None or (row.name, row.emoji) != (projected.name, projected.emoji)
    changed = [
        c.slug
        for c in following
        if moved
        and c.enabled
        and pass_owed(
            shown_today(c, c.seasons, now, before),
            shown_today(c, [s for s in c.seasons if s != slug] if action == "delete" else c.seasons, now, after),
        )
    ]
    return SeasonMutation(
        action,
        slug,
        columns,
        preset,
        tuple(c.id for c in following),
        tuple(visibility_step(slug) for slug in changed),
        retitled,
    )


def apply_season_in_session(session: Session, state, mutation: SeasonMutation) -> SeasonDef | None:
    """Persist an already projected change; a title conflict rolls back with all jobs."""
    if mutation.action == "delete":
        row = stored_season(session, mutation.slug)
        for row_id in mutation.following_ids:
            collection = session.get(Collection, row_id)
            collection.seasons = [slug for slug in collection.seasons if slug != mutation.slug]
        session.delete(row)
        return None
    if mutation.action == "create":
        row = SeasonDef(slug=mutation.slug, preset=mutation.preset, **mutation.columns)
        session.add(row)
    else:
        row = stored_season(session, mutation.slug)
        for key, value in mutation.columns.items():
            setattr(row, key, value)
    session.flush()
    if mutation.retitled:
        reject_row_title_clashes(session, state, season_from_row(row))
    return row


def season_view_in_session(session: Session, row: SeasonDef) -> dict:
    catalogue = load_catalogue(session)
    return season_view(
        season_from_row(row), row, rows_by_season(session, catalogue), context_builder.local_now().date()
    )
