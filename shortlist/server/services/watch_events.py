"""The play-event feed, and the question it exists to answer: was this in their row at the time?

Two halves.

**Ingest** pulls Plex's own play log into `watch_events`. One admin call, incremental on a stored
cursor, deduped on Plex's `historyKey`. It is the cheap half and the self-healing one — the log lives
on the server, so a week of Shortlist downtime costs nothing but latency.

**Attribution** answers, for a play at time T, whether the title was in a row that person could see at
T. That is the whole point: crediting a pick to the moment someone pressed play, rather than to the
state of their row now. Being watched is exactly what makes the engine DROP a title from a row, so
"is it in their row now" is false for precisely the titles that earned their credit.

Nothing here talks to Plex except `ingest_play_history`, and nothing here writes to Plex at all.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from shortlist.engine.models import SHARED_SLUG_PREFIX
from shortlist.server.db.models import (
    Collection,
    Delivery,
    PickRow,
    RowDeliverySnapshot,
    User,
    WatchEvent,
    WatchSession,
)
from shortlist.server.services.delivery_snapshots import utc as _as_utc
from shortlist.server.services.watch_identity import verified_owner_account_id

#: Where the incremental read resumes from.
CURSOR_KEY = "sync.history_cursor"
#: A first read with no cursor. Six years of history exists; picks do not go back anywhere near that
#: far, so anything older can never be attributed to anything.
BACKFILL_DAYS = 90


def ingest_play_history(session: Session, plex, store, *, limit: int = 20000) -> int:
    """Pull new plays into `watch_events`. Returns how many rows were added.

    Idempotent by construction: `history_key` is unique, so re-reading an overlapping window inserts
    nothing. That matters because the cursor is deliberately rewound slightly — a play landing in the
    log a second after we read past it would otherwise be lost for ever, and the cost of re-asking is
    a handful of rows that collide on insert.

    Args:
        session: Open session; the caller commits.
        plex: A `PlexClient` (admin token — one call covers every account).
        store: `SettingsStore`, for the cursor.
        limit: Cap on a single read, so a first run cannot pull the entire log.
    """
    raw = store.get(CURSOR_KEY)
    since: datetime | None = None
    if isinstance(raw, str) and raw:
        try:
            # Rewound by a minute. The log is written by the server as plays complete, and our read is
            # a moment in time; without the overlap, an event stamped in the second we were reading is
            # simply never seen again.
            since = _as_utc(datetime.fromisoformat(raw)) - timedelta(minutes=1)
        except ValueError:
            logger.warning("watch-events: unreadable history cursor {!r}, backfilling instead", raw)
    if since is None:
        since = datetime.now(UTC) - timedelta(days=BACKFILL_DAYS)

    events = plex.play_history(since=since, limit=limit)
    if not events:
        return 0
    # PMS history uses the same local owner alias as active sessions. Resolve only at this
    # verified ingestion boundary; keep the raw parser and previously persisted evidence intact.
    owner_account_id = verified_owner_account_id(session, plex) if any(e.plex_account_id == 1 for e in events) else None

    known = {
        key
        for (key,) in session.query(WatchEvent.history_key)
        .filter(WatchEvent.history_key.in_([e.history_key for e in events if e.history_key]))
        .all()
    }
    # A key-less event needs its own identity or the deliberate one-minute cursor rewind duplicates it
    # on every sync — SQLite allows unlimited NULLs in a UNIQUE column, so the constraint does not
    # catch it. `_play_event` does permit `history_key=None`, so this path is contemplated.
    keyless = {
        (account, rating_key, _as_utc(viewed))
        for account, rating_key, viewed in session.query(
            WatchEvent.plex_account_id, WatchEvent.rating_key, WatchEvent.viewed_at
        ).filter(WatchEvent.history_key.is_(None))
    }
    added = 0
    for event in events:
        if event.history_key and event.history_key in known:
            continue
        account_id = owner_account_id if event.plex_account_id == 1 and owner_account_id else event.plex_account_id
        original = (event.plex_account_id, event.rating_key, _as_utc(event.viewed_at))
        natural = (account_id, event.rating_key, _as_utc(event.viewed_at))
        # A prior keyless row may still carry unresolved local ID1. The overlapping cursor must
        # neither duplicate that old evidence under a new identity nor rewrite it retrospectively.
        if not event.history_key and (original in keyless or natural in keyless):
            continue
        session.add(
            WatchEvent(
                plex_account_id=account_id,
                rating_key=event.rating_key,
                show_rating_key=event.show_rating_key,
                media_type=event.media_type,
                viewed_at=event.viewed_at,
                source="history",
                history_key=event.history_key,
            )
        )
        known.add(event.history_key)
        keyless.add(natural)
        added += 1

    # The cursor only ever moves FORWARD. An earlier version parked it at the oldest row of a full
    # page, meaning to walk backwards through the backlog — but `since` is a lower bound and the read
    # is newest-first, so the next call returned the same newest page again. The cursor regressed and
    # never advanced: 20,000 rows re-read six times a day, inserting nothing, and the older backlog
    # never fetched at all. A full page is instead reported so the operator knows a backlog exists;
    # the next sweep picks up from the newest seen, which is the only direction that converges.
    # CLAMPED to now. A PMS whose clock was ahead when it recorded a play — a NAS booting before NTP —
    # leaves one history row stamped in the future, and parking the cursor there means every later
    # read asks for `viewedAt >` a date that has not happened. The ingest then returns 0 for ever,
    # logs "0 new play(s)" as though all were well, and `if not events: return 0` means the cursor is
    # never re-examined. There is no UI to reset it.
    cursor = min(max(e.viewed_at for e in events), datetime.now(UTC))
    if len(events) >= limit:
        logger.warning(
            "watch-events: the play log returned a full page ({}) — older history beyond this window is not backfilled",
            limit,
        )
    store.set(CURSOR_KEY, cursor.isoformat())
    logger.info("watch-events: {} new play(s) from the history log, cursor at {}", added, cursor.isoformat())
    return added


@dataclass(frozen=True)
class _StartEvent:
    """A session start, shaped like a `WatchEvent` so one scan can consider both."""

    plex_account_id: int
    viewed_at: datetime
    #: `(tmdb_id, media_type)` pairs, resolved before the event reaches the scan. Carried outright
    #: rather than as a rating key behind a flag — one field holding two id spaces is how the last
    #: key-space collision started.
    title_keys: frozenset[tuple[int, str]] = frozenset()


def tmdb_by_rating_key(session: Session) -> dict[int, tuple[int, str]]:
    """Plex rating key -> `(tmdb_id, media_type)`, learned from the picks that carry both.

    Everything downstream keys on TMDB ids rather than rating keys, and this is why. A pick CARRIED
    FORWARD from a previous run is persisted with `rating_key = 0`: `context_builder._previous_picks`
    rebuilds it from the database with a placeholder and delivery remaps it to the library's real key,
    which never gets written back. On the maintainer's server that is **110,801 of 158,737 picks —
    70%**, and 3,122 of the 4,539 in the current delivery.

    So matching a watch event to a row on `rating_key` is blind to two thirds of what is actually on
    people's shelves. Measured before this existed: 6,303 real play events produced 6 credits.

    A title keeps its TMDB id across every delivery, and its FIRST delivery carries the real rating
    key — so this map is built once from the rows that have both, and answers for all the rest.

    The MEDIA TYPE travels with the id and is not optional. TMDB namespaces ids per type: movie 1399
    and show 1399 are different titles, both sequences start at 1, and they overlap heavily. Keying on
    the bare number is the same key-space collision as keying on a rating key, one layer down — a play
    of the film would credit the series, stamp its percentage onto it, and could mark it finished.
    """
    found = {
        rating_key: (tmdb_id, media_type)
        for rating_key, tmdb_id, media_type in session.query(PickRow.rating_key, PickRow.tmdb_id, PickRow.media_type)
        .filter(PickRow.rating_key != 0, PickRow.tmdb_id.isnot(None))
        .distinct()
        .all()
    }
    for row in session.query(RowDeliverySnapshot).order_by(RowDeliverySnapshot.delivered_at, RowDeliverySnapshot.id):
        for pick in row.picks:
            if pick.get("rating_key") and pick.get("tmdb_id") and pick.get("media_type") in ("movie", "show"):
                found[int(pick["rating_key"])] = (int(pick["tmdb_id"]), pick["media_type"])
    return found


class RowMembership:
    """Delivered membership at playback time, independent of disposable run logs.

    Current ledger guards still refuse removed rows. A title rotated out of a still-live row
    may earn a late credit for a play inside its earlier delivery interval.
    """

    def __init__(self, session: Session) -> None:
        enabled = {slug for (slug,) in session.query(Collection.slug).filter(Collection.enabled.is_(True))}
        ledger = {(d.user_slug, d.collection_slug, d.library_key): d.rating_key for d in session.query(Delivery)}
        users = {u.id: u for u in session.query(User).filter(User.removed_at.is_(None))}
        self._per_person: dict[int, list[RowDeliverySnapshot]] = defaultdict(list)
        self._shared: list[RowDeliverySnapshot] = []
        self._shared_titles: dict[tuple[int, str], str] = {}
        for row in session.query(RowDeliverySnapshot).order_by(
            RowDeliverySnapshot.delivered_at, RowDeliverySnapshot.id
        ):
            if row.collection_slug not in enabled:
                continue
            current_key = ledger.get((row.user_slug, row.collection_slug, row.library_key))
            if current_key is None or (row.ended_at is None and current_key != row.rating_key):
                continue
            if row.shared:
                if row.user_slug != f"{SHARED_SLUG_PREFIX}_{row.collection_slug}":
                    continue
                self._shared.append(row)
                for pick in row.picks:
                    key = self._key(pick)
                    if key is not None:
                        self._shared_titles[key] = str(pick.get("title") or "")
            else:
                user = users.get(row.user_id)
                if user is not None and user.slug == row.user_slug:
                    self._per_person[user.id].append(row)

    @staticmethod
    def _key(pick: dict) -> tuple[int, str] | None:
        if pick.get("tmdb_id") and pick.get("media_type") in ("movie", "show"):
            return int(pick["tmdb_id"]), pick["media_type"]
        return None

    @classmethod
    def _contains(cls, row: RowDeliverySnapshot, keys: set[tuple[int, str]], when: datetime) -> bool:
        return (
            _as_utc(row.delivered_at) <= when
            and (row.ended_at is None or when < _as_utc(row.ended_at))
            and bool(keys & {key for pick in row.picks if (key := cls._key(pick)) is not None})
        )

    def visible_rows(self, user: User, keys: set[tuple[int, str]], when: datetime) -> list[str]:
        """Personal rows showing this title to this person at the supplied play time."""
        return sorted(
            {
                row.collection_slug
                for row in self._per_person.get(user.id, [])
                if self._contains(row, keys, _as_utc(when))
            }
        )

    def visible_shared_rows(self, user: User, keys: set[tuple[int, str]], when: datetime) -> list[str]:
        """Shared rows whose recorded audience could see the title at the supplied play time."""
        return sorted(
            {
                row.collection_slug
                for row in self._shared
                if self._contains(row, keys, _as_utc(when))
                and (row.audience is None or user.plex_account_id in row.audience)
                and user.plex_account_id not in row.muted
            }
        )

    def shared_pool(self) -> set[tuple[int, str]]:
        """All typed titles carried by retained shared delivery intervals."""
        return set(self._shared_titles)

    def shared_title(self, key: tuple[int, str]) -> str:
        """The newest recorded display title for a shared pick."""
        return self._shared_titles.get(key, "")


def _attribution_floor(session: Session) -> datetime | None:
    """The oldest moment any event could still be attributed to anything.

    Nothing before the first pick we hold can be credited — there was no row to have been in — so
    every scan below is bounded by it. It also keeps `event_credits` from re-reading the entire event
    log on every reconcile, six times a day, against a table with no ceiling: 6,303 rows after one
    ingest on a real server, and growing by ~100 a day for ever.

    That second sentence used to come first, and reading it as the WHOLE story is a mistake an audit
    actually made: it dismissed all four filters that apply this bound as "pure guard-clause
    optimisations", safe to delete. They are not. Each one changes what gets credited, because the
    floor is `min(PickRow.created_at)` while a SHARED row's delivery time is
    `RunSharedRow.delivered_at` — not a pick row at all. Membership therefore does NOT independently
    reject every pre-floor play, and removing a filter has been shown to:

    * mint a shared-row credit from a play that predates every pick (`_scan_plays`, `_session_starts`);
    * flip an abandonment into "finished", because `session_progress` returns the MAX percentage
      across all sittings and an ancient 95% sitting then outranks a recent 10% one;
    * suppress a withdrawal, since `_scan_plays` also builds the `observed` set that
      `_withdraw_unwatched` refuses to touch.

    Pinned by `TestTheAttributionFloorIsCorrectnessNotJustSpeed`.
    """
    oldest = session.query(func.min(PickRow.created_at)).scalar()
    return _as_utc(oldest) if oldest else None


def _session_starts(
    session: Session, since: datetime | None = None, tmdb_of: dict[int, tuple[int, str]] | None = None
) -> list[tuple[WatchSession, set[tuple[int, str]]]]:
    """Every session row with the title keys it resolves to — one entry per SITTING.

    The credit scan needs each sitting on its own, because membership is asked of the moment: a first
    sitting before the row existed says nothing about a second one that happened while the row was
    showing it.

    `tmdb_of` is passed in by the reconcile so the map is built ONCE per pass. Built here when absent,
    which keeps the function callable on its own.
    """
    tmdb_of = tmdb_by_rating_key(session) if tmdb_of is None else tmdb_of
    query = session.query(WatchSession)
    if since is not None:
        query = query.filter(WatchSession.started_at >= since)
    out: list[tuple[WatchSession, set[tuple[int, str]]]] = []
    for row in query.all():
        raw = {row.rating_key} | ({row.show_rating_key} if row.show_rating_key else set())
        keys = {tmdb_of[k] for k in raw if k in tmdb_of}
        if keys:
            out.append((row, keys))
    return out


def session_progress(
    session: Session, since: datetime | None = None, tmdb_of: dict[int, tuple[int, str]] | None = None
) -> dict[tuple[int, int, str], tuple[datetime, int | None]]:
    """`(plex_account_id, tmdb_id, media_type)` -> the earliest start we saw, and the furthest they got.

    The furthest across ALL sittings, not the last one: four sittings of one episode reaching 9%, 15%,
    57% and 100% is one watch that finished, and only the maximum says so. The earliest START is what
    the credit hangs on, because that is the moment the row was doing its job.

    Keyed by TMDB id AND media type, never by rating key: a carried-forward pick's `rating_key` is 0
    (70% of rows on a real server), and the type is required because TMDB namespaces its ids.
    """
    tmdb_of = tmdb_by_rating_key(session) if tmdb_of is None else tmdb_of
    out: dict[tuple[int, int, str], tuple[datetime, int | None]] = {}
    query = session.query(WatchSession)
    if since is not None:
        query = query.filter(WatchSession.started_at >= since)
    for row in query.all():
        raw = {row.rating_key} | ({row.show_rating_key} if row.show_rating_key else set())
        for tmdb_id, media_type in {tmdb_of[k] for k in raw if k in tmdb_of}:
            slot = (row.plex_account_id, tmdb_id, media_type)
            started = _as_utc(row.started_at)
            # A SERIES gets no percentage from a session, only the start. `row.percent` is how far
            # through that EPISODE they got, and the pick it resolves to is the whole show — so one
            # full episode of a sixty-episode series arrived as `max_percent = 100`, which the report
            # then rendered as "stops at 100%" and filed under 75%+. The dashboard would have stated,
            # as fact, that people abandon the show near the end when they quit after episode one.
            # NULL already means "we do not know how far", which is the truth here.
            percent = None if media_type == "show" else row.percent
            if slot not in out:
                out[slot] = (started, percent)
                continue
            prev_started, prev_percent = out[slot]
            out[slot] = (
                min(prev_started, started),
                max((p for p in (prev_percent, percent) if p is not None), default=None),
            )
    return out


def _scan_plays(
    session: Session, tmdb_of: dict[int, tuple[int, str]] | None = None
) -> list[tuple[int, datetime, set[tuple[int, str]]]]:
    """Every credit-bearing play, oldest first: `(plex_account_id, when, resolved title keys)`.

    One scan, two consumers — `event_credits` (personal rows) and `shared_credits` (shared rows) —
    because the sources, the key resolution and the ordering must be identical for the two to agree
    about what happened when. They differ only in which pool of titles they match against.

    A START is evidence a completion cannot be: someone who plays twenty minutes of a film and gives
    up never appears in Plex's history log at all, and by the time they finish it four days later the
    row has moved on. Sessions are folded in as first-class events so the credit lands on the moment
    the row worked.
    """
    # Everything below works in TMDB ids. A watch event carries a Plex rating key, and 70% of pick
    # rows carry `rating_key = 0` — see `tmdb_by_rating_key` for why — so the rating key is only
    # useful as a way to LOOK UP the tmdb id, never as the thing to match on.
    tmdb_of = tmdb_by_rating_key(session) if tmdb_of is None else tmdb_of
    floor = _attribution_floor(session)
    # EVERY sitting, not just the earliest. `session_progress` collapses a title to its first start —
    # right for reporting a percentage, wrong here: once someone had any session predating the row, the
    # title could never be start-credited again, however many times they played it OFF the row
    # afterwards. That is exactly the population this feature exists to measure, because a partial
    # watch sets no Plex flag, so the engine keeps recommending it and there is no history-log row to
    # fall back on.
    starts = [
        _StartEvent(
            plex_account_id=row.plex_account_id,
            viewed_at=_as_utc(row.started_at),
            title_keys=frozenset(keys),
        )
        for row, keys in _session_starts(session, floor, tmdb_of)
    ]
    # `source='transfer'` rows are somebody ELSE's watches, copied onto this account by the
    # watching-account transfer to carry the true dates across. They are real watches for
    # recommendation purposes — recency, seeds, the already-watched filter — but they are NOT this
    # person pressing play on a Shortlist row, and every one of them predates the row that would be
    # credited. Counting them would mint credits for rows that did nothing, inflate
    # `row_effectiveness`, and (through `_CreditInputs.observed`) make those credits impossible to
    # withdraw afterwards.
    #
    # The design said this filter existed before it did; the test that was supposed to cover it
    # asserted only that the rows carried the marker, never that attribution ignored them.
    events = session.query(WatchEvent).filter(WatchEvent.source != "transfer")
    if floor is not None:
        events = events.filter(WatchEvent.viewed_at >= floor)

    out: list[tuple[int, datetime, set[tuple[int, str]]]] = []
    for event in sorted([*events.all(), *starts], key=lambda e: _as_utc(e.viewed_at)):
        # BOTH keys, resolved to `(tmdb_id, media_type)`: a pick for a series stores the show, the log
        # reports the episode played, and on real history 46 of 78 matches were reachable only via the
        # show. A session arrives already resolved.
        if isinstance(event, _StartEvent):
            keys = set(event.title_keys)
        else:
            raw = {event.rating_key} | ({event.show_rating_key} - {None})
            keys = {tmdb_of[k] for k in raw if k in tmdb_of}
        if keys:
            out.append((event.plex_account_id, _as_utc(event.viewed_at), keys))
    return out


def event_credits(
    session: Session,
    membership: RowMembership,
    scan: list[tuple[int, datetime, set[tuple[int, str]]]] | None = None,
) -> dict[tuple[int, int, str], tuple[datetime, frozenset[str]]]:
    """`(user_id, tmdb_id, media_type)` -> the EARLIEST play that a row can be credited for.

    Earliest, not latest, on purpose. The credit belongs to the moment the row got them to press play;
    a rewatch three weeks later is not when the recommendation worked, and taking the newest event
    would file the hit in the wrong week of the trend chart for ever.

    The join runs through `picks`, which is what turns a Plex rating key into the `(tmdb_id,
    media_type)` pair the reconcile keys on. Both of an episode's keys are tried: a pick for a series
    stores the SHOW's key while the log reports the episode played, and on 30 days of real history 46
    of 78 matches were reachable only that way.
    """
    users = {u.plex_account_id: u for u in session.query(User).filter(User.removed_at.is_(None)).all()}
    if not users:
        return {}

    owned: dict[int, set[tuple[int, str]]] = defaultdict(set)
    for user_id, tmdb_id, media_type in (
        session.query(PickRow.user_id, PickRow.tmdb_id, PickRow.media_type).distinct().all()
    ):
        owned[user_id].add((tmdb_id, media_type))

    out: dict[tuple[int, int, str], tuple[datetime, frozenset[str]]] = {}
    for account_id, when, keys in _scan_plays(session) if scan is None else scan:
        user = users.get(account_id)
        if user is None:
            continue
        titles = keys & owned.get(user.id, set())
        if not titles:
            continue
        # Per key, not per event, matching `shared_credits`. One play resolves to as many as two keys
        # (the episode's and its show's), and asking membership with the whole set lets one title's
        # membership carry the other's credit. Only reachable when Plex has reused a metadata id, but
        # it costs nothing to ask the precise question.
        for tmdb_id, media_type in titles:
            rows = membership.visible_rows(user, {(tmdb_id, media_type)}, when)
            if not rows:
                continue
            slot = (user.id, tmdb_id, media_type)
            # The ROWS come out with the credit. `visible_rows` works out exactly which shelves were
            # showing the title, and throwing that away meant the stamp went onto every pick for the
            # person+title — including rows that had dropped it days earlier. `row_effectiveness`
            # filters on `collection_slug`, so those rows' hit rates were inflated by a play their
            # shelf could not have caused.
            if slot not in out or when < out[slot][0]:
                out[slot] = (when, frozenset(rows))
    return out


def shared_credits(
    session: Session,
    membership: RowMembership,
    scan: list[tuple[int, datetime, set[tuple[int, str]]]] | None = None,
) -> dict[tuple[int, str, int, str], datetime]:
    """`(user_id, row slug, tmdb_id, media_type)` -> the earliest play credited to that shared row.

    The twin of `event_credits`, and separate from it for one reason: the title pool. `event_credits`
    matches a play against the titles that person has a `picks` row for, and a shared row writes none
    — so every shared-row watch fell out of that intersection before membership was ever consulted.
    Here the pool is every title a live shared row has carried, and membership is asked of
    `visible_shared_rows`, which additionally tests the run's own audience snapshot.

    Earliest play, same as `event_credits`: the credit belongs to the moment the row got them to press
    play, not to a rewatch three weeks later.
    """
    users = {u.plex_account_id: u for u in session.query(User).filter(User.removed_at.is_(None)).all()}
    pool = membership.shared_pool()
    if not users or not pool:
        return {}

    # Keyed PER ROW, not per title. Two shared rows can carry the same title in different windows, and
    # each must keep its OWN earliest qualifying play: an earlier structure kept one timestamp per
    # title and unioned the rows, so a row that only started showing the title later inherited the
    # earlier play's date and was credited for a play made before it carried it.
    out: dict[tuple[int, str, int, str], datetime] = {}
    for account_id, when, keys in _scan_plays(session) if scan is None else scan:
        user = users.get(account_id)
        if user is None:
            continue
        titles = keys & pool
        if not titles:
            continue
        # Per key, not per event: an episode play resolves to both the episode's and the show's keys,
        # and two shared rows can hold one each. Asking with the whole set would credit both rows for
        # whichever title either of them had.
        for key in titles:
            for slug in membership.visible_shared_rows(user, {key}, when):
                slot = (user.id, slug, *key)
                if slot not in out or when < out[slot]:
                    out[slot] = when
    return out
