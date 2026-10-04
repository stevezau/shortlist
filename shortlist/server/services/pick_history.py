"""What rows showed on earlier real runs (#138), read from the `picks` ledger for the engine's `PickHistory`.

Only real runs count: a dry run writes no `PickRow`, a pick with no run is not from a run at all, and the
join on `Run.dry_run` keeps both out even if that ever changes. A kept pick is restamped by every refresh
night, so "first shown" is the EARLIEST sighting, never the latest.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, TitleKey
from shortlist.server.db.models import PickRow, Run, User


class DbPickHistory:
    """The engine's `PickHistory`, backed by the database.

    Takes the sessions factory, not a session: the engine reads this from worker threads for the length of a
    run, and a session must not be shared across threads (`DbCache` does the same).
    """

    def __init__(self, sessions: Callable[[], Session]) -> None:
        self._sessions = sessions

    def first_shown_since(self, user_slug: str, row_slug: str, since: date) -> set[TitleKey]:
        floor = datetime.combine(since, time.min, tzinfo=UTC)
        first_seen = func.min(PickRow.created_at)
        query = (
            _real_picks(user_slug, row_slug)
            .with_only_columns(PickRow.tmdb_id, PickRow.media_type)
            .group_by(PickRow.tmdb_id, PickRow.media_type)
            .having(first_seen >= floor)
        )
        with self._sessions() as session:
            return {_key(media, tmdb_id) for tmdb_id, media in session.execute(query)}

    def latest(self, user_slug: str, row_slug: str) -> set[TitleKey]:
        newest = _real_picks(user_slug, row_slug).with_only_columns(func.max(PickRow.run_id)).scalar_subquery()
        query = _real_picks(user_slug, row_slug).where(PickRow.run_id == newest)
        with self._sessions() as session:
            return {_key(p.media_type, p.tmdb_id) for p in session.scalars(query)}


def _real_picks(user_slug: str, row_slug: str):
    return (
        select(PickRow)
        .join(Run, Run.id == PickRow.run_id)
        .join(User, User.id == PickRow.user_id)
        .where(User.slug == user_slug, PickRow.collection_slug == row_slug, Run.dry_run.is_(False))
    )


def _key(media_type: str, tmdb_id: int) -> TitleKey:
    return (MediaType(media_type), tmdb_id)
