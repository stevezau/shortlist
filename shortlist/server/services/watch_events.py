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
from sqlalchemy.orm import Session

from shortlist.server.db.models import (
    Collection,
    PickRow,
    Run,
    RunSharedRow,
    User,
    WatchEvent,
    WatchSession,
)

#: Where the incremental read resumes from.
CURSOR_KEY = "sync.history_cursor"
#: A first read with no cursor. Six years of history exists; picks do not go back anywhere near that
#: far, so anything older can never be attributed to anything.
BACKFILL_DAYS = 90


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


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

    known = {
        key
        for (key,) in session.query(WatchEvent.history_key)
        .filter(WatchEvent.history_key.in_([e.history_key for e in events if e.history_key]))
        .all()
    }
    added = 0
    for event in events:
        if event.history_key and event.history_key in known:
            continue
        session.add(
            WatchEvent(
                plex_account_id=event.plex_account_id,
                rating_key=event.rating_key,
                show_rating_key=event.show_rating_key,
                media_type=event.media_type,
                viewed_at=event.viewed_at,
                source="history",
                history_key=event.history_key,
            )
        )
        known.add(event.history_key)
        added += 1

    # A FULL page means there is more behind it. The read is newest-first, so advancing the cursor to
    # the newest event would step over everything older that we did not get — permanently, because
    # the next pass starts from there. Park the cursor at the OLDEST row instead and walk backwards
    # through the backlog a page at a time.
    cursor = min(e.viewed_at for e in events) if len(events) >= limit else max(e.viewed_at for e in events)
    store.set(CURSOR_KEY, cursor.isoformat())
    logger.info("watch-events: {} new play(s) from the history log, cursor at {}", added, cursor.isoformat())
    return added


@dataclass(frozen=True)
class _StartEvent:
    """A session start, shaped like a `WatchEvent` so one scan can consider both."""

    plex_account_id: int
    rating_key: int
    viewed_at: datetime
    show_rating_key: int | None = None


class RowMembership:
    """Was this title in a row that person could see at a given moment?

    Built once per reconcile and queried per event. Everything it needs is already recorded — the
    picks with the run that delivered them, and the shared rows with theirs — so this is a read of
    history rather than a snapshot of now, and a late event (a backfill, a catch-up after downtime) is
    attributed to the row that was actually on screen at the time.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._run_started: dict[int, datetime] = {
            rid: _as_utc(started) for rid, started in session.query(Run.id, Run.started_at).all()
        }
        self._live_slugs = {slug for (slug,) in session.query(Collection.slug).filter(Collection.enabled.is_(True))}
        self._per_person = self._load_per_person()
        self._shared = self._load_shared()
        self._audience = self._load_audience()

    def _load_per_person(self) -> dict[tuple[int, str, str], list[tuple[datetime, set[int]]]]:
        """(user, row, library) -> the delivery timeline, oldest first: (delivered_at, rating keys).

        Keyed per (user, row, library) because rows carry their own crons: the newest run overall is
        routinely scoped to ONE row, so a global "latest delivery" would read every other row as
        empty. Picks with no `run_id` are skipped — `DELETE /api/runs` and the retention prune both
        detach them, and a pick that cannot be placed in time cannot support a claim about time.
        """
        rows = (
            self._session.query(
                PickRow.user_id, PickRow.collection_slug, PickRow.section_key, PickRow.run_id, PickRow.rating_key
            )
            .filter(PickRow.run_id.isnot(None), PickRow.collection_slug.in_(self._live_slugs))
            .all()
        )
        by_delivery: dict[tuple[int, str, str, int], set[int]] = defaultdict(set)
        for user_id, slug, section, run_id, rating_key in rows:
            by_delivery[(user_id, slug, section, run_id)].add(rating_key)

        out: dict[tuple[int, str, str], list[tuple[datetime, set[int]]]] = defaultdict(list)
        for (user_id, slug, section, run_id), keys in by_delivery.items():
            started = self._run_started.get(run_id)
            if started is not None:
                out[(user_id, slug, section)].append((started, keys))
        for timeline in out.values():
            timeline.sort(key=lambda entry: entry[0])
        return out

    def _load_shared(self) -> dict[str, list[tuple[datetime, set[int]]]]:
        """Shared rows, same timeline shape, from `run_shared_rows.picks`.

        Shared rows write no pick rows at all — `RunSharedRow`'s docstring records why — so their
        contents come out of that JSON. Pre-0076 rows carry no ids in it and simply never match, which
        is correct: shared-row crediting did not exist before them.
        """
        out: dict[str, list[tuple[datetime, set[int]]]] = defaultdict(list)
        for slug, run_id, picks in self._session.query(
            RunSharedRow.collection_slug, RunSharedRow.run_id, RunSharedRow.picks
        ).all():
            started = self._run_started.get(run_id)
            if started is None or slug not in self._live_slugs:
                continue
            keys = {int(p["rating_key"]) for p in (picks or []) if isinstance(p, dict) and p.get("rating_key")}
            if keys:
                out[slug].append((started, keys))
        for timeline in out.values():
            timeline.sort(key=lambda entry: entry[0])
        return out

    def _load_audience(self) -> dict[tuple[str, int], set[int] | None]:
        """Who could SEE each shared row, per delivery. None = everyone.

        Snapshotted on the run rather than read from `collection_audience`, which is current state:
        without the snapshot, adding someone to a subset row today would retroactively credit watches
        from before they could see it. NULL (every pre-0076 row) falls back to "everyone", which is no
        worse than having no audience test at all — the state this replaces.
        """
        out: dict[tuple[str, int], list[tuple[datetime, set[int] | None]]] = {}
        for slug, run_id, audience in self._session.query(
            RunSharedRow.collection_slug, RunSharedRow.run_id, RunSharedRow.audience
        ).all():
            out[(slug, run_id)] = audience if audience is None else set(audience)
        return out

    @staticmethod
    def _contained_at(timeline: list[tuple[datetime, set[int]]], keys: set[int], when: datetime) -> bool:
        """Did the newest delivery at or before `when` contain any of these keys?

        The NEWEST one only. A title dropped on the last rebuild is not in the row any more, however
        many earlier deliveries carried it — that is the entire rule.
        """
        newest: set[int] | None = None
        for delivered_at, delivered in timeline:
            if delivered_at <= when:
                newest = delivered
            else:
                break
        return bool(newest and (keys & newest))

    def visible_rows(self, user: User, keys: set[int], when: datetime) -> list[str]:
        """Every row slug that was showing this title to this person at `when`."""
        when = _as_utc(when)
        found = [
            slug
            for (user_id, slug, _section), timeline in self._per_person.items()
            if user_id == user.id and self._contained_at(timeline, keys, when)
        ]
        for slug, timeline in self._shared.items():
            if not self._contained_at(timeline, keys, when):
                continue
            if self._shared_visible_to(slug, user, when):
                found.append(slug)
        return sorted(set(found))

    def _shared_visible_to(self, slug: str, user: User, when: datetime) -> bool:
        """Was this shared row visible to this person at `when`, per the delivery's own snapshot."""
        best: set[int] | None = None
        best_at: datetime | None = None
        for (row_slug, run_id), audience in self._audience.items():
            if row_slug != slug:
                continue
            started = self._run_started.get(run_id)
            if started is None or started > when:
                continue
            if best_at is None or started > best_at:
                best_at, best = started, audience
        # None means public — and it is also what every pre-snapshot row carries.
        return best is None or user.plex_account_id in best


def session_progress(session: Session) -> dict[tuple[int, int], tuple[datetime, int | None]]:
    """`(plex_account_id, rating_key)` -> the earliest start we saw, and the furthest they got.

    The furthest across ALL sittings, not the last one: your example was four sittings of one episode
    reaching 9%, 15%, 57% and 100%, and only the last of those describes what happened. The earliest
    START is what the credit hangs on, because that is the moment the row was doing its job.
    """
    out: dict[tuple[int, int], tuple[datetime, int | None]] = {}
    for row in session.query(WatchSession).all():
        for key in {row.rating_key} | ({row.show_rating_key} if row.show_rating_key else set()):
            slot = (row.plex_account_id, key)
            started = _as_utc(row.started_at)
            percent = row.percent
            if slot not in out:
                out[slot] = (started, percent)
                continue
            prev_started, prev_percent = out[slot]
            out[slot] = (
                min(prev_started, started),
                max((p for p in (prev_percent, percent) if p is not None), default=None),
            )
    return out


def event_credits(session: Session, membership: RowMembership) -> dict[tuple[int, int, str], datetime]:
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

    # rating key -> the titles it identifies, per user. Built from picks so a key we never
    # recommended costs nothing: the overwhelming majority of plays on a server are not ours (1,977
    # of 2,049 events over 30 days on the maintainer's box).
    by_key: dict[tuple[int, int], set[tuple[int, str]]] = defaultdict(set)
    for user_id, rating_key, tmdb_id, media_type in session.query(
        PickRow.user_id, PickRow.rating_key, PickRow.tmdb_id, PickRow.media_type
    ).all():
        by_key[(user_id, rating_key)].add((tmdb_id, media_type))

    # A START is evidence a completion cannot be: someone who plays twenty minutes of a film and gives
    # up never appears in Plex's history log at all, and by the time they finish it four days later
    # the row has moved on. Sessions are folded in as first-class events so the credit lands on the
    # moment the row worked.
    starts = [
        _StartEvent(plex_account_id=account_id, rating_key=rating_key, viewed_at=started)
        for (account_id, rating_key), (started, _percent) in session_progress(session).items()
    ]
    scan = sorted(
        [*session.query(WatchEvent).all(), *starts],
        key=lambda e: _as_utc(e.viewed_at),
    )

    out: dict[tuple[int, int, str], datetime] = {}
    for event in scan:
        user = users.get(event.plex_account_id)
        if user is None:
            continue
        keys = {event.rating_key} | ({getattr(event, "show_rating_key", None)} - {None})
        titles = {t for k in keys for t in by_key.get((user.id, k), ())}
        if not titles:
            continue
        when = _as_utc(event.viewed_at)
        if not membership.visible_rows(user, keys, when):
            continue
        for tmdb_id, media_type in titles:
            slot = (user.id, tmdb_id, media_type)
            if slot not in out or when < out[slot]:
                out[slot] = when
    return out
