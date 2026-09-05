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

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from shortlist.engine.models import MediaType, UserProfile, WatchedItem
from shortlist.server.db.models import WatchedTitle, WatchSyncState, utcnow

#: What the reader dates a row it can find no watch date for — a show marked watched rather than
#: played carries no `lastViewedAt`, and when its episodes cannot date it either this is the honest
#: answer. A real and common value, so it is compared against, never treated as corrupt.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

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
        library: str = "",
        force_full: bool = False,
        reconcile: bool = False,
        repair_dates=None,
        now: datetime | None = None,
    ) -> SyncOutcome:
        """Bring one (person, library) up to date. `read(since)` performs the PMS call.

        The cursor is advanced only after `read` returns — a read that raises leaves the cursor
        exactly where it was, so the next attempt re-covers the same ground rather than skipping it.

        `read` may return a `WatchedRead` (what the PMS client gives back) or a bare list of items.
        A bare list carries no coverage claim, so it never deletes — see `_read_items`.

        Args:
            library: The library's display name, for the watched page to group and filter on. Blank
                from a caller that doesn't know it, which never CLEARS a name already on record —
                see `_upsert`.
            force_full: Read the whole library rather than resuming from the cursor. Every sync sets
                this (issue #108); it says nothing about whether anything may be DELETED.
            reconcile: May this pass drop cached titles the read did not return? Separate from
                `force_full` because reading completely and deleting are different risks; the sync
                sets both, `prefill_history` sets both, and the guards on the replace branch below
                are what make deleting at that cadence safe.
            repair_dates: ``f(show_keys) -> {rating_key: datetime}`` — the real watch date for shows
                Plex re-counted without re-dating (see `_shows_plex_recounted_but_did_not_redate`).
                Optional: without it those shows keep their stale date, which is what happened before
                this existed. Called at most once per section, and only when something actually
                changed, so a quiet night makes no request at all.
        """
        now = now or utcnow()
        full = force_full or self.needs_full(session, user_id, section_key, now=now)
        state = _state(session, user_id, section_key)
        since = None if full else _aware(state.cursor_viewed_at) if state else None

        items, covers_window = _read_items(read(since))
        if repair_dates is not None:
            items = _repair_stale_show_dates(session, user_id, section_key, items, repair_dates)

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
            # Delete what the read did NOT return — the un-watches — rather than deleting the section
            # and rebuilding it. Same end state; hugely less churn now that this runs on every sync
            # instead of weekly. Measured on a 47-user server, the blanket version rewrote ~14,700
            # rows every pass and cost ~35s of the 65s sync; the targeted one deletes nothing on a
            # quiet night, which is almost every night.
            #
            # TRANSFERRED rows are exempt (`source_viewed_at IS NOT NULL`). They did not come from
            # this read and Plex may not know them at all: a watch-history transfer that did not
            # scrobble leaves rows the PMS has never heard of, so a blind replace deletes the entire
            # transfer on the first sync — and `needs_full` is True for a brand-new watching account,
            # so that is the FIRST sync, every time.
            #
            # Keyed exactly as `_upsert` WRITES them — `_cache_key`, not `item.rating_key`. An item
            # the PMS gave no `ratingKey` is stored under its negated tmdb_id, so building this set
            # from `rating_key` alone left every such row out of it: they matched `notin_` on every
            # pass and were deleted and re-inserted each time, which is the churn the targeted delete
            # exists to avoid. `rating_key` is NOT NULL on the table, so there is no null case to
            # handle here — the keyless rows are the negative ones.
            keys = {key for key in (_cache_key(item) for item in items) if key is not None}
            stale = session.query(WatchedTitle).filter(
                WatchedTitle.user_id == user_id,
                WatchedTitle.section_key == section_key,
                WatchedTitle.source_viewed_at.is_(None),
            )
            if keys:
                # `notin_` expands to one bound parameter per key, and the runtime image is
                # python:3.12-slim on Debian, whose SQLite caps host parameters at 32,766 (upstream's
                # own default is 250,000). The ceiling is on the number of watched TITLES in one
                # section — bounded by the library's size, ~5k on the largest server measured — so it
                # is out of reach here, unlike the returned-set diff `_drop_vanished_since` avoids.
                stale = stale.filter(WatchedTitle.rating_key.notin_(keys))
            stale.delete(synchronize_session=False)
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
            _upsert(session, user_id, section_key, media_type, item, library)
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

    The missing-timestamp case used to be safe by construction — a title the PMS reports with no
    `lastViewedAt` was stamped 1970 by the reader, sat outside every window, and so could never be
    deleted here. That is no longer true on its own: a show marked watched carries no `lastViewedAt`
    and the full read now dates it from its newest watched EPISODE, which puts it squarely inside the
    window. Two things keep it safe instead, and both are needed. The reader RETURNS such a show on
    an incremental walk rather than skipping it (`plex_pms.watched_titles`), so it is never absent
    from one; and `_upsert` refuses to write the epoch over a real date, so a failed episode read
    cannot push the row back outside the window and lose its date.

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


def _shows_plex_recounted_but_did_not_redate(
    session: Session, user_id: int, section_key: str, items: list[WatchedItem]
) -> set[int]:
    """Shows whose episode count went UP while the show's own date stood still.

    That combination has exactly one cause: the episodes were MARKED rather than played. Plex updates
    a show's own `lastViewedAt` when the show is played and not when its episodes are marked, so a
    series someone finishes by ticking "mark as watched" keeps whatever date it had — a partly-watched
    series finished today still reads as finished months ago (issue #108, reported after the first
    round of fixes).

    That date is not cosmetic: it is the recency half of a seed's weight, halving every ~45 days. A
    series marked watched today but dated two years ago weighs about zero, so it never seeds — the
    person finishes a show and Shortlist cannot use it to find them anything similar.

    Only titles with a PREVIOUS cached count qualify, and this is the load-bearing guard rather than
    an optimisation. A show being seen for the first time has no count to have risen from, so
    "it went up" would be true of every row on a first sync, a rebuilt cache, or a newly added
    library — and re-dating those to now would tell Shortlist that everything in a person's history
    was watched today. That is far worse than the stale date this repairs: a wrong OLD date makes one
    show seed weakly, a wrong NEW date makes their whole back catalogue seed at full strength.
    """
    counted = {
        item.rating_key: item for item in items if item.rating_key is not None and item.viewed_leaf_count is not None
    }
    if not counted:
        return set()
    stale: set[int] = set()
    rows = (
        session.query(WatchedTitle)
        .filter(
            WatchedTitle.user_id == user_id,
            WatchedTitle.section_key == section_key,
            WatchedTitle.rating_key.in_(list(counted)),
        )
        .all()
    )
    for row in rows:
        item = counted[row.rating_key]
        if row.viewed_leaf_count is None or item.viewed_leaf_count <= row.viewed_leaf_count:
            continue  # nothing new was watched or marked
        # `viewed_at`, never `source_viewed_at`. The question here is whether the date PLEX reports
        # stood still, so it has to be compared against the last date Plex gave us. A transferred row
        # carries the ORIGINAL account's historical date in `source_viewed_at`, which is always older
        # than the replica's stamp — so mixing the two clocks made `item.watched_at > cached_date`
        # true every pass and a transferred account's marked-watched shows were silently never
        # repaired. `_to_item` still prefers `source_viewed_at`, so the transfer's true date keeps
        # winning everywhere it should.
        cached_date = _aware(row.viewed_at)
        if cached_date is None or item.watched_at > cached_date:
            continue  # Plex moved the date too, so they PLAYED it and Plex is already right
        stale.add(row.rating_key)
    return stale


def _repair_stale_show_dates(
    session: Session, user_id: int, section_key: str, items: list[WatchedItem], repair_dates
) -> list[WatchedItem]:
    """Give the shows Plex re-counted but did not re-date their real date, from their episodes.

    Never moves a date BACKWARDS. The episode answer replaces the show's date only when it is newer:
    a show can hold episodes watched long ago beside a recent play, and the show's own row is right
    in that case.

    Dates are best-effort. A failure here logs and returns the items untouched — they keep the stale
    date, which is exactly the behaviour before this existed, and no read is lost over it.
    """
    stale = _shows_plex_recounted_but_did_not_redate(session, user_id, section_key, items)
    if not stale:
        return items
    try:
        dates = repair_dates(stale)
    except Exception as e:
        logger.warning(
            "watch cache: section {} — could not date {} re-counted show(s) from their episodes ({})",
            section_key,
            len(stale),
            type(e).__name__,
        )
        return items
    if not dates:
        return items
    out = []
    repaired = 0
    for item in items:
        # Gated on OUR `stale` set, not merely on what the callback returned. The "must have a
        # previous cached count" rule is computed here and is the one thing standing between this and
        # re-dating a whole back catalogue, so it is enforced here too rather than trusted to a
        # different module's key handling.
        when = dates.get(item.rating_key) if item.rating_key in stale else None
        if when is not None and when > item.watched_at:
            repaired += 1
            out.append(replace(item, watched_at=when))
        else:
            out.append(item)
    if repaired:
        logger.info(
            "watch cache: section {} — took the real watch date from the episodes of {} marked-watched show(s)",
            section_key,
            repaired,
        )
    return out


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


def _upsert(
    session: Session,
    user_id: int,
    section_key: str,
    media_type: MediaType,
    item: WatchedItem,
    library: str = "",
) -> None:
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
    # BEFORE the assignments below overwrite it: the date guard needs to know whether anything new
    # was watched or marked since last time, and `viewed_leaf_count` is about to be replaced.
    previous_leaf_count = row.viewed_leaf_count
    row.tmdb_id = item.tmdb_id
    row.media_type = media_type.value
    # Only when the caller knows it. Writing "" unconditionally would let any caller that doesn't
    # pass a name (an older test, a future one-off backfill) silently blank a name already on record,
    # and the page would lose the library line until the next sync put it back.
    if library:
        row.library = library
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
    # A WORSE date never overwrites a better one. Two ways that happens, and both put back the exact
    # symptom of #108 within a night, silently, with no other copy of the good value.
    #
    # 1. The epoch. A show marked watched has no `lastViewedAt` of its own and is dated from its
    #    newest watched episode; when that episode read fails the reader honestly degrades to 1970,
    #    and writing that here would rewrite a correct date back to "finished 20697d ago".
    #
    # 2. Plex's still-stale show date, on a quiet night. This is subtler and it defeated the repair
    #    entirely. `_repair_stale_show_dates` only fires while the count is RISING, and this function
    #    then persists the new count — so the next sync sees an unchanged count, does not repair, and
    #    Plex reports the same stale show date it always did. Writing it through reverted the repair
    #    after exactly one night, every night, for ever. An unchanged count means Plex has learnt
    #    nothing new about this show, so its date carries no new information and must not win.
    #
    # A FALLING count still writes through: that is an un-mark, and the date should follow it back.
    # A first insert still records whatever it has, epoch included — it is all that is known.
    incoming = item.watched_at or utcnow()
    cached = _aware(row.viewed_at)
    keep_cached = (
        cached is not None
        and incoming < cached
        and (
            incoming <= _EPOCH
            or (
                media_type is MediaType.SHOW
                and previous_leaf_count is not None
                and item.viewed_leaf_count == previous_leaf_count
            )
        )
    )
    if not keep_cached:
        row.viewed_at = incoming


def _to_item(row: WatchedTitle) -> WatchedItem:
    return WatchedItem(
        title=row.title,
        media_type=MediaType(row.media_type),
        # The true date, not the scrobble date — same reason `watched_set` orders on it. Everything
        # downstream (recency windows, "because you recently watched X") reads this field.
        watched_at=_aware(row.source_viewed_at) or _aware(row.viewed_at) or _EPOCH,
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
