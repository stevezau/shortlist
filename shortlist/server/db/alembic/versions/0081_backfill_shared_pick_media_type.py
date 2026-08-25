"""Put `media_type` back on the shared-row picks that were written without it.

`RunSharedRow.picks` is a JSON blob, and `_shared_key` refuses to guess a title's type when the blob
carries none — correctly, because TMDB ids are namespaced per type and matching on the id alone would
credit the wrong title. `_pick_dicts` writes `media_type` now, but the rows already stored predate it,
so every watch off a shared row before that fix credits nothing.

Measured on the maintainer's server before this ran: 28 plays by five people fell in the window one
un-keyed run governed, resolving to 9 distinct credits that the dashboard could not see. Those are
real shared-row watches reported as nothing.

Only rows carrying a `rating_key` can be repaired, and only where `picks` independently agrees on the
title: the type is taken from a pick row whose rating key AND tmdb id both match the blob entry, so
this joins on two fields rather than trusting a rating key Plex may since have reused
(`metadata_items.id` reuse is recorded in `watch_events`). An entry that fails either test is left
exactly as it was.

Older rows carry no `tmdb_id` and no `rating_key` at all — their blobs hold only title/year/rank —
so nothing can be recovered for them by any means, and this does not try.

Idempotent: an entry that already has a `media_type` is skipped, so re-running changes nothing.

Revision ID: 0081
Revises: 0080
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # (rating_key, tmdb_id) -> media_type. Keyed on BOTH so a reused rating key cannot silently
    # retype a title; a rating key that maps to two different tmdb ids simply matches neither blob
    # entry unless the ids line up.
    known: dict[tuple[int, int], str] = {}
    for rating_key, tmdb_id, media_type in bind.execute(
        sa.text(
            "SELECT DISTINCT rating_key, tmdb_id, media_type FROM picks "
            "WHERE rating_key IS NOT NULL AND rating_key > 0 AND tmdb_id IS NOT NULL AND media_type IS NOT NULL"
        )
    ):
        known[(int(rating_key), int(tmdb_id))] = str(media_type)
    if not known:
        return

    # `run_shared_rows` is keyed on (run_id, collection_slug) — it has no surrogate id.
    for run_id, slug, blob in bind.execute(
        sa.text("SELECT run_id, collection_slug, picks FROM run_shared_rows WHERE picks IS NOT NULL")
    ).fetchall():
        try:
            picks = json.loads(blob) if isinstance(blob, str) else blob
        except (TypeError, ValueError):
            continue  # an unreadable blob is left alone rather than rewritten
        if not isinstance(picks, list):
            continue
        changed = False
        for pick in picks:
            if not isinstance(pick, dict) or pick.get("media_type"):
                continue  # already typed, or not a pick — either way, nothing to do
            rating_key, tmdb_id = pick.get("rating_key"), pick.get("tmdb_id")
            if not rating_key or not tmdb_id:
                continue
            resolved = known.get((int(rating_key), int(tmdb_id)))
            if resolved:
                pick["media_type"] = resolved
                changed = True
        if changed:
            bind.execute(
                sa.text("UPDATE run_shared_rows SET picks = :picks WHERE run_id = :run_id AND collection_slug = :slug"),
                {"picks": json.dumps(picks), "run_id": run_id, "slug": slug},
            )


def downgrade() -> None:
    """Deliberately empty.

    The absent `media_type` was not a value anyone chose — it was a field the writer forgot. Stripping
    it back out would restore a blob that credits nothing, which is the bug, not a prior state worth
    returning to.
    """
