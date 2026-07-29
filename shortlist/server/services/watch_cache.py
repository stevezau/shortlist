"""The watched-title cache: read the PMS incrementally, keep the set in SQLite.

**Why this exists.** The watched set drives every recommendation and the dashboard's hit rate, and it
was read COMPLETE — per user, per library, 500 titles a page with full metadata and GUIDs — on the
nightly sync and then again inside every run. On a 40-user server that is hundreds of large XML
responses a night for a set that changes by a handful of items.

**The rule that keeps it correct.** An incremental read is a partial answer by construction: Plex's
``lastViewedAt>=`` filter cannot show a title that was un-watched, deleted, or whose timestamp never
moved. So the cache is never treated as authoritative — ``last_full_at`` drives a periodic COMPLETE
re-read that replaces the section outright. Incremental is an optimisation on top of a full read, not
a replacement for one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, UserProfile, WatchedItem
from shortlist.server.db.models import WatchedTitle, WatchSyncState, utcnow

#: How far BEHIND the newest thing seen the cursor is left.
#:
#: A page walk takes time, and Plex stamps `lastViewedAt` from its own clock, not ours. Resuming from
#: exactly the newest timestamp would skip anything written during the walk, or a second late by a
#: clock a few seconds ahead. Re-reading a small overlap is free — the upsert is keyed on rating_key,
#: so re-seeing a title is a no-op — and skipping a watch is not.
CURSOR_OVERLAP = timedelta(minutes=5)

#: How often a section is read in FULL regardless of its cursor. Weekly: an un-watch or a deleted
#: title is rare and not urgent, and this is the only thing that can ever notice one.
DEFAULT_FULL_EVERY = timedelta(days=7)


@dataclass
class SyncOutcome:
    """What one section's sync actually cost, for the run log."""

    section_key: str
    full: bool
    fetched: int
    total: int


class WatchCache:
    """Reads the PMS into `watched_titles`, and answers "what has this person watched?" from it."""

    def __init__(self, sessions, *, full_every: timedelta = DEFAULT_FULL_EVERY):
        self._sessions = sessions
        self._full_every = full_every

    # -- reading ------------------------------------------------------------------------

    def watched_set(self, session: Session, user_id: int) -> list[WatchedItem]:
        """This person's cached watched titles, newest first."""
        rows = (
            session.query(WatchedTitle)
            .filter(WatchedTitle.user_id == user_id)
            .order_by(WatchedTitle.viewed_at.desc())
            .all()
        )
        return [_to_item(row) for row in rows]

    # -- writing ------------------------------------------------------------------------

    def needs_full(self, session: Session, user_id: int, section_key: str, *, now: datetime | None = None) -> bool:
        """Should this (person, library) be read in FULL rather than incrementally?

        True whenever there is nothing to be incremental against — no state, no cursor, no record of
        a completed full read — and whenever the last full read is older than `full_every`. Never a
        guess: "I don't know where to resume" always means a complete read.

        Note it does NOT test the row count. Zero cached titles is a perfectly good answer for
        someone who has watched nothing in that library, and treating it as "the cache is broken"
        would read that library in full every single night for ever.
        """
        now = now or utcnow()
        state = _state(session, user_id, section_key)
        if state is None or state.cursor_viewed_at is None or state.last_full_at is None:
            return True
        return _aware(state.last_full_at) <= now - self._full_every

    def force_full_next_time(self, session: Session, user_id: int, section_key: str) -> None:
        """Make the next read of this (person, library) a COMPLETE one.

        The escape hatch for a section that cannot be topped up — most likely because the PMS refused
        the incremental `lastViewedAt>=` filter. A full read sends no filter, so it still works; the
        alternative is a cursor that never advances and a cache that quietly goes stale for ever.
        """
        state = _state(session, user_id, section_key)
        if state is not None:
            state.cursor_viewed_at = None
            session.flush()

    def sync_section(
        self,
        session: Session,
        user: UserProfile,
        user_id: int,
        section_key: str,
        media_type: MediaType,
        read,
        *,
        force_full: bool = False,
        now: datetime | None = None,
    ) -> SyncOutcome:
        """Bring one (person, library) up to date. `read(since)` performs the PMS call.

        The cursor is advanced only after `read` returns — a read that raises leaves the cursor
        exactly where it was, so the next attempt re-covers the same ground rather than skipping it.
        """
        now = now or utcnow()
        full = force_full or self.needs_full(session, user_id, section_key, now=now)
        state = _state(session, user_id, section_key)
        since = None if full else _aware(state.cursor_viewed_at) if state else None

        items = read(since)

        if full:
            # Replace, don't merge: a full read is the ONLY thing that can notice an un-watch or a
            # deleted title, and merging would keep the very rows it exists to drop.
            session.query(WatchedTitle).filter(
                WatchedTitle.user_id == user_id, WatchedTitle.section_key == section_key
            ).delete(synchronize_session=False)
            session.flush()

        for item in items:
            _upsert(session, user_id, section_key, media_type, item)
        session.flush()

        total = (
            session.query(WatchedTitle)
            .filter(WatchedTitle.user_id == user_id, WatchedTitle.section_key == section_key)
            .count()
        )
        newest = max((item.watched_at for item in items if item.watched_at), default=None)
        if state is None:
            state = WatchSyncState(user_id=user_id, section_key=section_key)
            session.add(state)
        if full:
            state.last_full_at = now
            # A full read that returned nothing still establishes a cursor — otherwise a person with
            # an empty library would be read in full for ever.
            state.cursor_viewed_at = (_aware(newest) if newest else now) - CURSOR_OVERLAP
        else:
            state.last_incremental_at = now
            if newest is not None:
                state.cursor_viewed_at = _aware(newest) - CURSOR_OVERLAP
        state.item_count = total
        session.flush()

        logger.debug(
            "watch cache: {} section {} {} -> {} fetched, {} cached",
            user.username,
            section_key,
            "FULL" if full else "incremental",
            len(items),
            total,
        )
        return SyncOutcome(section_key=section_key, full=full, fetched=len(items), total=total)


def _state(session: Session, user_id: int, section_key: str) -> WatchSyncState | None:
    return (
        session.query(WatchSyncState)
        .filter(WatchSyncState.user_id == user_id, WatchSyncState.section_key == section_key)
        .one_or_none()
    )


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands timezone-aware columns back naive; comparing one to an aware datetime raises."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _cache_key(item: WatchedItem) -> int | None:
    """The stable per-section identity to upsert on.

    Plex's `ratingKey` when there is one — it is the item's own id within the library. When there
    isn't (a source that doesn't report one), fall back to the NEGATED tmdb_id: real rating keys are
    always positive, so a negative can only ever be this fallback and the two can never collide.
    Without the fallback such a title is dropped, and the cache silently holds less than the direct
    read did.
    """
    if item.rating_key:
        return item.rating_key
    return -item.tmdb_id if item.tmdb_id else None


def _upsert(session: Session, user_id: int, section_key: str, media_type: MediaType, item: WatchedItem) -> None:
    """Insert or refresh one title. Keyed on `rating_key`, which is Plex's own stable id within a
    section — so an overlap re-read updates a row rather than duplicating it."""
    rating_key = _cache_key(item)
    if rating_key is None:
        # Nothing stable to key on at all. Dropping is right: an unkeyed row could never be updated
        # or deduped, so it would accumulate a fresh copy on every single sync.
        return
    row = (
        session.query(WatchedTitle)
        .filter(
            WatchedTitle.user_id == user_id,
            WatchedTitle.section_key == section_key,
            WatchedTitle.rating_key == rating_key,
        )
        .one_or_none()
    )
    if row is None:
        row = WatchedTitle(user_id=user_id, section_key=section_key, rating_key=rating_key)
        session.add(row)
    row.tmdb_id = item.tmdb_id
    row.media_type = media_type.value
    row.title = item.title or ""
    row.year = item.year
    row.watch_count = item.watch_count or 1
    # None, not 0: for a movie there are no episodes to count, which is not the same claim as
    # "none of its episodes were watched" — and the finished-show check reads these directly.
    row.viewed_leaf_count = item.viewed_leaf_count
    row.leaf_count = item.leaf_count
    row.viewed_at = item.watched_at or utcnow()


def _to_item(row: WatchedTitle) -> WatchedItem:
    return WatchedItem(
        title=row.title,
        media_type=MediaType(row.media_type),
        watched_at=_aware(row.viewed_at) or datetime(1970, 1, 1, tzinfo=UTC),
        tmdb_id=row.tmdb_id,
        year=row.year,
        # The negative fallback key is ours, not Plex's — hand back None rather than a rating key
        # that would 404 against the PMS.
        rating_key=row.rating_key if row.rating_key > 0 else None,
        watch_count=row.watch_count or 1,
        viewed_leaf_count=row.viewed_leaf_count,
        leaf_count=row.leaf_count,
    )
