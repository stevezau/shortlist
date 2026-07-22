"""Local watch-history store — sync the full per-user play history incrementally, read it complete.

Plex's history API returns only the most recent ~200 plays per call (and each source is session-
based), so a heavy watcher's older watches were invisible to the already-watched filter and got
recommended again (SFLIX/MooHouse 'Hawking', 2026-07-20). This mirrors the FULL per-user history
into ``watch_events``, synced incrementally (per-user high-water mark on ``User.watch_synced_at``),
and the engine reads the complete set. It's a drop-in ``HistorySource`` — ``fetch`` syncs then reads
— so it slots into ``ctx.history_source`` with no run-plumbing changes and the engine's existing
ratingKey→tmdb resolution and watched-filter logic are unchanged.

Plex's history API returns playback SESSIONS only — it never returns a mark-as-watched, at any
depth or date window. That was assumed to be a shrinking legacy gap; it is not. Measured on SFLIX:
one account's history API reports 11,462 plays covering ~1,000 distinct titles, while the PMS
database records ~14,500 watched — the difference being marks, including six titles that surfaced in
that user's row all flagged inside a 23-second window (a bulk mark).

So there are two sources, both feeding this one table:

* the history API — works wherever Shortlist runs, and reports partial completion;
* the PMS database (optional, read-only) — the only source that sees marks, and it sees them for
  every account in one read.

The DB source only fills GAPS: a title the play history already covers is left alone, so flags never
inflate play counts that the finished-show fraction depends on.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.clients.plex_db import PlexDbReader, WatchedFlag
from shortlist.engine.history import HistorySource
from shortlist.engine.models import MediaType, UserProfile, WatchedItem
from shortlist.server.db.models import User, WatchEvent, utcnow

# Re-pull a little before the watermark each run, so a play written slightly out of order (or landing
# during the previous run) isn't skipped. The unique constraint dedups the re-pulled overlap.
_OVERLAP = timedelta(hours=6)

# When a mark carries no timestamp. Deliberately the epoch rather than "now": these rows are read
# back by recency-sensitive code, and dating an unknown mark as today would make it look like a
# fresh watch.
_FLAG_FALLBACK_AT = datetime(1970, 1, 1, tzinfo=UTC)


class StoreHistorySource:
    """Syncs ``watch_events`` from ``upstream`` (incremental), then returns the COMPLETE stored
    history for the user. ``upstream`` is the real Plex/Tautulli source; this is what the engine sees."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        upstream: HistorySource,
        *,
        min_completion: float,
        flags: PlexDbReader | None = None,
        flag_account_id: Callable[[UserProfile], int] | None = None,
    ):
        self._sessions = sessions
        self._upstream = upstream
        self._min_completion = min_completion
        # Optional, off unless the owner points us at their PMS database. See `_sync_flags`.
        self._flags = flags
        # UserProfile -> the PMS-LOCAL account id. The owner's plex.tv id appears nowhere in the
        # PMS account space (their local row is id=1), so passing it through would silently match
        # zero rows for the one person who can even configure this. Same resolution the history
        # source already does; injected so this stays a pure store with no Plex client of its own.
        self._flag_account_id = flag_account_id

    @property
    def flags_configured(self) -> bool:
        """Whether a PMS database is mounted for the reconcile — so the Tools action can tell "not
        set up, mount it" apart from "read it, nothing new to add"."""
        return self._flags is not None

    def fetch(self, user: UserProfile, *, min_completion: float, since: datetime | None = None) -> list[WatchedItem]:
        # `since` is ignored on the read: the store already holds the complete history; the engine
        # wants everything, and the incremental window is an internal sync detail.
        #
        # NOTE: this does NOT read the PMS database. That is `reconcile_flags`, a deliberate one-off
        # the owner runs from Tools — not every run. Reading someone's live multi-gigabyte production
        # database, opening its WAL and scanning every account's marks, is too heavy and too invasive
        # to do nightly for a payload (new marks) most users produce rarely; a mark also can't be seen
        # by the history API, so the reconcile is a repair to run when watched state drifts, not a
        # feed. See `reconcile_flags`.
        self._sync(user)
        return self._load(user, min_completion=min_completion)

    def reconcile_flags(self, user: UserProfile) -> int:
        """Fill watched-history gaps from the PMS database for one user; return the number of events
        added. A no-op (returns 0) unless the owner has mounted their Plex database — see
        `_sync_flags`. This is the on-demand Tools action, invoked by the owner, never by a run."""
        return self._sync_flags(user)

    def _sync(self, user: UserProfile) -> None:
        """Pull plays newer than the user's watermark and upsert them; advance the watermark.

        Fail-soft: if the upstream fetch errors, keep whatever is already stored (a run must never die
        because the history API hiccuped) and leave the watermark so next run retries the same window.
        """
        with self._sessions() as session:
            row = session.query(User).filter_by(slug=user.slug).first()
            if row is None:
                return
            watermark = row.watch_synced_at
            # SQLite hands DateTime back timezone-NAIVE; the upstream sources compare it against
            # timezone-aware watch times (Plex/Tautulli), so normalise to aware UTC or the subtraction
            # and comparison raise a TypeError and the whole sync fails soft (no new events ever land).
            if watermark is not None and watermark.tzinfo is None:
                watermark = watermark.replace(tzinfo=UTC)
            since = (watermark - _OVERLAP) if watermark is not None else None
            try:
                new_items = self._upstream.fetch(user, min_completion=self._min_completion, since=since)
            except Exception as e:
                logger.warning(
                    "{}: watch-history sync failed ({}) — using the {} events already stored",
                    user.slug,
                    type(e).__name__,
                    session.query(WatchEvent).filter_by(user_id=row.id).count(),
                )
                return
            # Wrap the writes too (not just the fetch): the first-run backfill is ~thousands of rows,
            # and this runs inside the engine's per-user thread pool, so several users backfilling at
            # once contend for SQLite's single writer. Batch-commit to release the lock periodically,
            # and on any write error roll back and leave the watermark so next run retries from the same
            # point (dedup makes the re-pull harmless) — a locked DB must never fail the user's run.
            inserted = 0
            try:
                for i, item in enumerate(new_items):
                    if item.rating_key is None:
                        continue  # no ratingKey -> can't resolve to a tmdb_id, so it can never match a candidate
                    stmt = (
                        sqlite_insert(WatchEvent)
                        .values(
                            user_id=row.id,
                            rating_key=item.rating_key,
                            media_type=item.media_type.value,
                            title=item.title,
                            year=item.year,
                            watched_at=item.watched_at,
                            completion=item.completion,
                        )
                        .on_conflict_do_nothing(index_elements=["user_id", "rating_key", "watched_at"])
                    )
                    inserted += session.execute(stmt).rowcount or 0
                    if (i + 1) % 2000 == 0:
                        session.commit()  # release the writer lock between batches so other users can sync
                row.watch_synced_at = utcnow()
                session.commit()
            except Exception as e:
                session.rollback()
                logger.warning(
                    "{}: watch-history store write failed ({}) — watermark left for next run to retry",
                    user.slug,
                    type(e).__name__,
                )
                return
            logger.debug(
                "{}: watch-history sync +{} new events (since={})",
                user.slug,
                inserted,
                since.isoformat() if since else "full backfill",
            )

    def _sync_flags(self, user: UserProfile) -> int:
        """Fill gaps from the PMS database's watched flags — the only source that sees marks. Returns
        the number of events added.

        GAPS ONLY: a ratingKey the play history already covers is skipped entirely, so a flag can
        never add a second row for a title that was genuinely played. That matters because
        `watch_events` holds one row per PLAY and the finished-show fraction counts them; a flag is
        "watched at least once", not another play.

        Fail-soft: an unreadable database leaves whatever is already stored and returns 0. It is
        someone's live 2 GB PMS database — never a reason to fail the action.
        """
        if self._flags is None:
            return 0
        with self._sessions() as session:
            row = session.query(User).filter_by(slug=user.slug).first()
            if row is None:
                return 0
            try:
                account_id = self._flag_account_id(user) if self._flag_account_id else row.plex_account_id
                watched = self._flags.watched_for(account_id)
            except Exception as e:
                # A permission-denied mount raises OSError, not PlexDbUnavailable, and a torn read on
                # a live database raises sqlite3.DatabaseError — degrade on all of them.
                logger.warning(
                    "{}: could not read watched flags from the Plex database ({}: {})",
                    user.slug,
                    type(e).__name__,
                    e,
                )
                return 0
            if not watched:
                return 0
            # "Already stored" means different things by media type, so the dedup key does too:
            #   * a MOVIE is a single watch — skip if its ratingKey is stored AT ALL, so a mark
            #     never adds a second row for a title the play history already played (the finished
            #     check is set membership, and the store holds one row per play, not per mark);
            #   * a SHOW arrives as one event per marked EPISODE, and the finished-show fraction
            #     COUNTS those events — so each must survive as its own row, keyed on
            #     (ratingKey, watched_at). The episode-timestamp spread in `plex_db` is what keeps a
            #     bulk-marked season from collapsing to one event under that key.
            stored = {
                (rk, at)
                for rk, at in session.query(WatchEvent.rating_key, WatchEvent.watched_at).filter_by(user_id=row.id)
            }
            played_keys = {rk for (rk, _at) in stored}

            def already_stored(flag: WatchedFlag) -> bool:
                if flag.media_type == MediaType.MOVIE.value:
                    return flag.rating_key in played_keys
                return (flag.rating_key, flag.last_viewed_at or _FLAG_FALLBACK_AT) in stored

            missing = [flag for flag in watched if not already_stored(flag)]
            if not missing:
                logger.debug("{}: Plex flags add nothing — all {} already stored", user.slug, len(watched))
                return 0
            added = 0
            try:
                for i, flag in enumerate(missing):
                    stmt = (
                        sqlite_insert(WatchEvent)
                        .values(
                            user_id=row.id,
                            rating_key=flag.rating_key,
                            media_type=flag.media_type,
                            title=flag.title,
                            year=flag.year,
                            # A mark has no session, so Plex may record no timestamp. The filter
                            # cares THAT it was watched, not when — `_FLAG_FALLBACK_AT` keeps the
                            # column honest without inventing a plausible-looking recent date.
                            watched_at=flag.last_viewed_at or _FLAG_FALLBACK_AT,
                            completion=1.0,
                        )
                        .on_conflict_do_nothing(index_elements=["user_id", "rating_key", "watched_at"])
                    )
                    added += session.execute(stmt).rowcount or 0
                    if (i + 1) % 2000 == 0:
                        session.commit()
                session.commit()
            except Exception as e:
                session.rollback()
                logger.warning(
                    "{}: writing Plex watched flags failed ({}) — retry the reconcile", user.slug, type(e).__name__
                )
                return 0
            logger.info(
                "{}: +{} watched title(s) from the Plex database that the play history never saw",
                user.slug,
                added,
            )
            return added

    def _load(self, user: UserProfile, *, min_completion: float) -> list[WatchedItem]:
        with self._sessions() as session:
            row = session.query(User).filter_by(slug=user.slug).first()
            if row is None:
                return []
            events = session.query(WatchEvent).filter_by(user_id=row.id).all()
        return [
            WatchedItem(
                title=e.title,
                media_type=MediaType(e.media_type),
                watched_at=e.watched_at if e.watched_at.tzinfo else e.watched_at.replace(tzinfo=UTC),
                year=e.year,
                rating_key=e.rating_key,
                completion=e.completion,
            )
            for e in events
            if e.completion >= min_completion
        ]
