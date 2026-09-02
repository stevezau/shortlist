"""The watched-title cache: read the PMS incrementally, keep the set in SQLite.

**Why this exists.** The watched set drives every recommendation and the dashboard's hit rate, and it
was read COMPLETE — per user, per library, 500 titles a page with full metadata and GUIDs — on the
nightly sync and then again inside every run. On a 40-user server that is hundreds of large XML
responses a night for a set that changes by a handful of items.

**The rule that keeps it correct.** An incremental read is a partial answer by construction: it cannot
show a title that was un-watched, deleted, or whose timestamp never moved. So the cache is never
treated as authoritative — ``last_full_at`` drives a periodic COMPLETE re-read that replaces the
section outright. Incremental is an optimisation on top of a full read, not a replacement for one.

**Un-watching.** Removal happens at three different scopes, because no cheaper one subsumes the next:

* within the incremental window, a cached title the read did not return is dropped — but only when
  the read PROVES it covered that window (`WatchedRead.covers_window`). A truncated walk looks
  identical from here, so unproven coverage tops up and deletes nothing;
* outside it, only the periodic full read can notice, since nothing in an incremental response
  points at a title watched before the cursor;
* whole libraries removed from the server are swept by `forget_dead_sections`, which the full read
  cannot do — it only ever replaces sections it successfully read.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func
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
        """This person's cached watched titles, newest first.

        Ordered by the TRUE watch date — `source_viewed_at` when a transfer recorded one, else what
        Plex reported. Seeds come off the front of this list, so ordering by the Plex date alone
        would rank a transferred history by the day it was scrobbled rather than by when the person
        actually watched anything. See `WatchedTitle.source_viewed_at`.
        """
        rows = (
            session.query(WatchedTitle)
            .filter(WatchedTitle.user_id == user_id)
            .order_by(func.coalesce(WatchedTitle.source_viewed_at, WatchedTitle.viewed_at).desc())
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
        reconcile: bool = False,
        now: datetime | None = None,
    ) -> SyncOutcome:
        """Bring one (person, library) up to date. `read(since)` performs the PMS call.

        The cursor is advanced only after `read` returns — a read that raises leaves the cursor
        exactly where it was, so the next attempt re-covers the same ground rather than skipping it.

        `read` may return a `WatchedRead` (what the PMS client gives back) or a bare list of items.
        A bare list carries no coverage claim, so it never deletes — see `_read_items`.

        Args:
            force_full: Read the whole library rather than resuming from the cursor. Every sync sets
                this (issue #108); it says nothing about whether anything may be DELETED.
            reconcile: May this pass drop cached titles the read did not return? Separate from
                `force_full` because reading completely and deleting are different risks; the sync
                sets both, `prefill_history` sets both, and the guards on the replace branch below
                are what make deleting at that cadence safe.
        """
        now = now or utcnow()
        full = force_full or self.needs_full(session, user_id, section_key, now=now)
        state = _state(session, user_id, section_key)
        since = None if full else _aware(state.cursor_viewed_at) if state else None

        items, covers_window = _read_items(read(since))

        # THREE conditions before this section may be REPLACED — deleted, then refilled from what the
        # read returned. It is the only path here that destroys watch history, and every one of them
        # answers a way it has been shown to go wrong:
        #
        # * `reconcile` — every sync sets this now. It was confined to the periodic pass while a
        #   complete read could delete on no proof at all; that made UN-WATCHING take up to a week,
        #   reported by a user the day the #108 fix shipped. The two conditions below are therefore
        #   the whole guarantee, not a second line behind a rare cadence.
        # * `covers_window` — a PMS that omits `totalSize` and caps the container answers a short
        #   page with a 200, indistinguishable from "they un-watched all of it".
        #
        # * a SECOND read agreeing, when the first would delete most of the section. `covers_window`
        #   is derived from the same response it validates, so a server that under-reports
        #   `totalSize` proves itself complete — and `totalSize="0"` is the extreme of that, erasing
        #   the section outright while reporting success. One extra request, only on the rare pass
        #   that would drop half a library, is the same shape as plex-safety rule 4's second read
        #   before an orphan delete. A server lying CONSISTENTLY still defeats it; a transient short
        #   answer, which is the realistic failure, does not.
        replace = full and reconcile and covers_window
        refused_by_confirm = False
        if replace:
            cached = _section_count(session, user_id, section_key)
            if cached and len(items) * 2 < cached:
                items, replace = _confirm_shrink(read, items, cached, user.username, section_key)
                refused_by_confirm = not replace
        if replace:
            # Replace, don't merge: a full read is the ONLY thing that can notice an un-watch of
            # something watched long ago, and merging would keep the very rows it exists to drop.
            #
            # TRANSFERRED rows are the exception and must survive (`source_viewed_at IS NOT NULL`).
            # They did not come from this read and Plex may not know them at all: a watch-history
            # transfer that did not scrobble leaves rows the PMS has never heard of, so a blind
            # replace deletes the entire transfer on the first sync — and `needs_full` is True for a
            # brand-new watching account, so that is the FIRST sync, every time. A scrobbled row is
            # returned by the read and simply gets updated in place, keeping its true date because
            # `_upsert` never writes that column.
            session.query(WatchedTitle).filter(
                WatchedTitle.user_id == user_id,
                WatchedTitle.section_key == section_key,
                WatchedTitle.source_viewed_at.is_(None),
            ).delete(synchronize_session=False)
            session.flush()
        elif full and reconcile and not refused_by_confirm:
            # Only for genuine unproven coverage. `_confirm_shrink` has already said its piece, and
            # adding this line after it sent the operator hunting a `totalSize` problem that isn't
            # there — the read DID prove coverage; a second read disagreed with it.
            logger.warning(
                "watch cache: {} section {} — the complete read could not prove it saw the whole "
                "library, so this reconcile tops up without deleting",
                user.username,
                section_key,
            )
        elif since is not None and covers_window:
            _drop_vanished_since(session, user_id, section_key, since, items, user.username)
        elif since is not None:
            # Read the window but could not prove it read ALL of it — a server that omits `totalSize`
            # and caps the container, or one whose sort was not honoured. Absence means "we did not
            # read that far", not "un-watched", so top up and delete nothing. Degrades to exactly the
            # pre-cache behaviour: a title lingers until the periodic full read, which is a stale row
            # rather than a deleted one.
            logger.debug(
                "watch cache: {} section {} — window coverage unproven, topping up without deleting",
                user.username,
                section_key,
            )

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
        # Only a PROVEN complete read stamps `last_full_at` — `needs_full` asks "was this library read
        # end to end?", and an unproven walk cannot answer yes. Not gated on `reconcile`: this records
        # the READ, not the deletion.
        if full and covers_window:
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

    def forget_dead_sections(self, session: Session, user_id: int, live_section_keys: set[str]) -> int:
        """Drop cached titles and cursors for libraries the server no longer has.

        Nothing else ever removes these. The periodic full read only replaces sections it successfully
        READ, so a library deleted from the PMS leaves its rows behind for ever — still counted in the
        watched set, still suppressing recommendations, for titles that no longer exist.

        NOT the same as a library someone simply isn't shared. That one raises `SectionNotShared`, is
        expected on every sync for every library a person doesn't have, and their history for it is
        still true — so it is deliberately left alone. Only libraries gone SERVER-wide are swept.

        The cursor goes with the titles: dropping rows but keeping `WatchSyncState` would leave
        `needs_full` answering False against an empty cache, so a section that came back would stay
        thin until its next scheduled full read instead of self-healing on the very next sync.

        Returns the number of cached titles dropped.
        """
        if not live_section_keys:
            # An empty library list is far likelier a PMS blip than a server with no libraries, and
            # acting on it would wipe every cached watch for this person. Do nothing.
            return 0
        dropped = (
            session.query(WatchedTitle)
            .filter(WatchedTitle.user_id == user_id, WatchedTitle.section_key.notin_(live_section_keys))
            .delete(synchronize_session=False)
        )
        session.query(WatchSyncState).filter(
            WatchSyncState.user_id == user_id, WatchSyncState.section_key.notin_(live_section_keys)
        ).delete(synchronize_session=False)
        if dropped:
            session.flush()
            logger.info("watch cache: dropped {} cached title(s) from libraries no longer on the server", dropped)
        return dropped


def _section_count(session: Session, user_id: int, section_key: str) -> int:
    """How many DELETABLE titles are cached for this (person, library) right now.

    Excludes transferred rows for the same reason the replace does (`source_viewed_at IS NOT NULL`):
    they are exempt from deletion, so counting them puts the two sides of the shrink comparison on
    different populations. On a watching account carrying a transfer that inflated the count
    permanently — the guard fired on every reconcile pass for ever, bought a second full page-walk
    each time, and told the operator that titles had vanished when nothing had.
    """
    return (
        session.query(WatchedTitle)
        .filter(
            WatchedTitle.user_id == user_id,
            WatchedTitle.section_key == section_key,
            WatchedTitle.source_viewed_at.is_(None),
        )
        .count()
    )


def _confirm_shrink(read, items, cached: int, username: str, section_key: str) -> tuple[list[WatchedItem], bool]:
    """Ask the server a second time before dropping most of a library.

    Returns `(items, replace)` — the CONFIRMING read's items when it agrees, so the delete acts on
    the fresher answer, and `replace=False` when it does not. A read that RAISES is a refusal: the
    likeliest real outcome here is a second full library read failing against a PMS that just
    answered short, and that is evidence against the first answer, not for it.
    """
    try:
        second, second_covers = _read_items(read(None))
    except Exception as e:
        logger.warning(
            "watch cache: {} section {} — the read returned {} of {} cached titles and the confirming "
            "read failed ({}); keeping them rather than treating it as a mass un-watch",
            username,
            section_key,
            len(items),
            cached,
            type(e).__name__,
        )
        return items, False
    if second_covers and len(second) * 2 < cached:
        logger.info(
            "watch cache: {} section {} — {} of {} cached titles are gone, confirmed by a second read",
            username,
            section_key,
            cached - len(second),
            cached,
        )
        return second, True
    logger.warning(
        "watch cache: {} section {} — the read returned {} of {} cached titles but a second read "
        "returned {}; keeping them rather than treating the first as a mass un-watch",
        username,
        section_key,
        len(items),
        cached,
        len(second),
    )
    # Keep whichever answer saw MORE — nothing is being deleted either way, and the richer read is
    # the better thing to upsert from.
    return (second if len(second) > len(items) else items), False


def _read_items(result) -> tuple[list[WatchedItem], bool]:
    """Normalise what `read(since)` handed back into (items, covers_window).

    A bare list is treated as NOT covering its window. That is the safe default and it is the honest
    one: a caller that returns a plain list has made no claim about how much of the window it read,
    and the only thing the flag gates is deletion. Test doubles and any future history source that
    yields plain items therefore top up without ever removing anything.
    """
    items = getattr(result, "items", result)
    return list(items), bool(getattr(result, "covers_window", False))


def _drop_vanished_since(
    session: Session,
    user_id: int,
    section_key: str,
    since: datetime,
    items: list[WatchedItem],
    username: str,
) -> int:
    """Remove cached titles the incremental read should have returned and didn't — an un-watch.

    Only ever called when the read PROVED it covered its window (`WatchedRead.covers_window`) — the
    walk either saw a timestamp older than the cutoff or reached the library's reported total, with
    the sort honoured throughout. Under that condition what it returned IS every title in this
    library viewed at or after `since`, so a cached row inside the same window that the walk did not
    return is no longer watched: un-watched, or deleted from the library.

    Do NOT relax that guard. A truncated walk looks identical from here — the caller cannot tell an
    un-watch from "the server stopped sending" — and on a PMS that omits `totalSize` and caps the
    container, an ordinary quiet night would delete everything else in the window.

    Only the window is authoritative, and that is the limit of what any incremental scheme can do: an
    un-watch of something viewed BEFORE the cursor leaves no trace in an incremental response at all,
    so that one still waits for the periodic full read.

    Safe against the missing-timestamp case by construction: a title the PMS reports with no
    `lastViewedAt` is stamped 1970 by the reader and cached as 1970, so it sits outside every window
    and this can never delete it — which matters, because the incremental walk skips such a title
    rather than returning it.

    Args:
        since: The cutoff handed to the reader. Must be UTC — SQLite strips tzinfo on bind rather
            than converting, so a non-UTC aware value would compare against UTC rows as if it were
            UTC. Every caller gets this from `_aware`, which assumes UTC for the naive values SQLite
            hands back.

    Returns:
        The number of cached titles dropped.
    """
    # Loaded and diffed in Python rather than a `rating_key NOT IN (...)` delete: the window holds
    # only what was viewed since the cursor (a handful), while the returned set can be the whole
    # library on a server that reports a total and pages through it — so this is both the smaller
    # query and the one with no bound-variable ceiling.
    window = (
        session.query(WatchedTitle)
        .filter(
            WatchedTitle.user_id == user_id,
            WatchedTitle.section_key == section_key,
            WatchedTitle.viewed_at >= since,
            # Transferred rows are not Plex's to vanish. A non-scrobbled transfer leaves rows the PMS
            # has never heard of, so every incremental read "fails to return" them — which reads as
            # an un-watch and deletes the transfer piecemeal. Same exemption as the full replace.
            WatchedTitle.source_viewed_at.is_(None),
        )
        .all()
    )
    if not window:
        return 0
    still_watched = {key for key in (_cache_key(item) for item in items) if key is not None}
    vanished = [row for row in window if row.rating_key not in still_watched]
    for row in vanished:
        session.delete(row)
    if vanished:
        session.flush()
        logger.info(
            "watch cache: {} section {} — dropped {} un-watched title(s): {}",
            username,
            section_key,
            len(vanished),
            ", ".join(row.title for row in vanished[:5]),
        )
    return len(vanished)


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
    # Written unconditionally, INCLUDING when it is None — an un-rating has to be able to clear the
    # column. Guarding this with `if item.user_rating is not None` would make a rating permanent:
    # someone who thumbs-downs a title and then changes their mind would keep the old value for ever,
    # and their row would stay quietly shaped by a judgement they withdrew.
    row.user_rating = item.user_rating
    row.viewed_at = item.watched_at or utcnow()


def _to_item(row: WatchedTitle) -> WatchedItem:
    return WatchedItem(
        title=row.title,
        media_type=MediaType(row.media_type),
        # The true date, not the scrobble date — same reason `watched_set` orders on it. Everything
        # downstream (recency windows, "because you recently watched X") reads this field.
        watched_at=_aware(row.source_viewed_at) or _aware(row.viewed_at) or datetime(1970, 1, 1, tzinfo=UTC),
        tmdb_id=row.tmdb_id,
        year=row.year,
        # The negative fallback key is ours, not Plex's — hand back None rather than a rating key
        # that would 404 against the PMS.
        rating_key=row.rating_key if row.rating_key > 0 else None,
        watch_count=row.watch_count or 1,
        viewed_leaf_count=row.viewed_leaf_count,
        leaf_count=row.leaf_count,
        user_rating=row.user_rating,
    )
