"""DB-backed implementations of the engine's storage protocols (SnapshotStore, TMDB cache).

These are the narrow seam between the pure engine (which knows only the protocols) and the
server's SQLAlchemy schema. Keeping them here — not in the run service — lets the run service be
about orchestration and nothing else.
"""

from __future__ import annotations

import json
import time

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from rowarr.engine.models import FilterSnapshot, UserType, dedupe_slug, slugify
from rowarr.server.db.models import CacheRow, RestrictionSnapshotRow, User


def unique_slug(session: Session, username: str) -> str:
    """A slug no other user already holds. Slugs are UNIQUE in the DB and are what row labels are
    built from, so two Plex display names that slugify alike (a real possibility — Plex names are
    free text) must not collide: the second user would fail to save, and their share filter would
    then never be written.
    """
    return dedupe_slug(slugify(username), lambda slug: session.query(User).filter_by(slug=slug).first() is not None)


class DbSnapshotStore:
    """Engine SnapshotStore over the restriction_snapshots table."""

    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory

    def get(self, plex_account_id: int) -> FilterSnapshot | None:
        with self._sessions() as session:
            user = session.query(User).filter_by(plex_account_id=plex_account_id).one_or_none()
            if user is None:
                return None
            row = (
                session.query(RestrictionSnapshotRow)
                .filter_by(user_id=user.id, reason="initial")
                .order_by(RestrictionSnapshotRow.id)
                .first()
            )
            if row is None:
                return None
            return FilterSnapshot(
                plex_account_id=plex_account_id,
                username=user.username,
                taken_at=row.taken_at,
                filters=row.filters_before,
            )

    def save(self, snapshot: FilterSnapshot) -> None:
        with self._sessions() as session:
            user = session.query(User).filter_by(plex_account_id=snapshot.plex_account_id).one_or_none()
            if user is None:
                # An account that shares the server but that Rowarr has never seen — someone the
                # owner invited to Plex since the last time the Users page was opened. We still
                # have to write their share filter (a row is visible to anyone whose filter does
                # not exclude it), and rule 2 says we cannot write it without a snapshot first.
                # So record them: disabled (we build no row for them) but restorable, because
                # uninstall reaches snapshots through this table.
                user = User(
                    plex_account_id=snapshot.plex_account_id,
                    username=snapshot.username,
                    slug=unique_slug(session, snapshot.username),
                    user_type=UserType.SHARED.value,
                    enabled=False,
                )
                session.add(user)
                session.flush()
                logger.info("{}: first seen during a run — recorded so their filters can be restored", user.username)
            session.add(
                RestrictionSnapshotRow(
                    user_id=user.id,
                    taken_at=snapshot.taken_at,
                    reason="initial",
                    filters_before=snapshot.filters,
                    filters_after={},
                )
            )
            session.commit()


class DbCache:
    """Engine TMDB cache over the caches table."""

    def __init__(self, session_factory: sessionmaker[Session], kind: str = "tmdb"):
        self._sessions = session_factory
        self._kind = kind

    def get(self, key: str) -> str | None:
        with self._sessions() as session:
            row = session.get(CacheRow, (self._kind, key))
            if row and row.expires_at > time.time():
                return json.dumps(row.value)
            return None

    def set(self, key: str, value: str, ttl_s: int) -> None:
        with self._sessions() as session:
            row = session.get(CacheRow, (self._kind, key))
            payload = json.loads(value)
            if row is None:
                session.add(CacheRow(kind=self._kind, key=key, value=payload, expires_at=time.time() + ttl_s))
            else:
                row.value = payload
                row.expires_at = time.time() + ttl_s
            session.commit()
