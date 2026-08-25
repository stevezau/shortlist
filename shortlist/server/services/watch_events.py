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
        natural = (event.plex_account_id, event.rating_key, _as_utc(event.viewed_at))
        if not event.history_key and natural in keyless:
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
    return {
        rating_key: (tmdb_id, media_type)
        for rating_key, tmdb_id, media_type in session.query(PickRow.rating_key, PickRow.tmdb_id, PickRow.media_type)
        .filter(PickRow.rating_key != 0, PickRow.tmdb_id.isnot(None))
        .distinct()
        .all()
    }


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
        # (user_slug, row_slug, library_key) actually ON PLEX, from the delivery ledger. `collections`
        # says a row is CONFIGURED; the ledger says the collection exists, and the two diverge every
        # time a row is muted for someone, they leave its audience, or a cold start skips them — the
        # run DELETES the collection (`_forget_removed_deliveries`) while the row definition stays.
        # The snapshot path has always joined this; the event path did not, and would happily credit
        # a row the person has not been able to see for a month.
        self._on_plex = {(row.user_slug, row.collection_slug, row.library_key) for row in session.query(Delivery).all()}
        self._slug_by_user = {u.id: u.slug for u in session.query(User).all()}
        self._per_person = self._load_per_person()
        # A shared row's delivery is filed under `shared_<slug>` (see `_persist_shared_row_report`),
        # so the same ledger answers "is this collection actually on the server" for shared rows.
        self._shared_on_plex = {
            slug
            for (user_slug, slug, _lib) in self._on_plex
            # Built from the constant, never spelled out. If the two ever drift this set silently
            # empties and every shared-row credit stops, with no error anywhere.
            if user_slug == f"{SHARED_SLUG_PREFIX}_{slug}"
        }
        self._shared_titles: dict[tuple[int, str], str] = {}
        #: (slug, run_id) -> who could SEE that delivery; None = everyone. Filled by `_load_shared`.
        self._audience: dict[tuple[str, int], set[int] | None] = {}
        #: (slug, run_id) -> who had the row switched OFF at that delivery. A deny-list, so a public
        #: row stays public: folding mutes into the audience froze it to whoever existed that night.
        self._shared_muted: dict[tuple[str, int], set[int]] = {}
        #: (slug, run_id) -> when that delivery landed, for rows that recorded it.
        self._shared_delivered: dict[tuple[str, int], datetime] = {}
        self._shared = self._load_shared()

    def _load_per_person(self) -> dict[tuple[int, str, str], list[tuple[datetime, set[tuple[int, str]]]]]:
        """(user, row, library) -> the delivery timeline, oldest first: (delivered_at, title keys).

        Keyed per (user, row, library) because rows carry their own crons: the newest run overall is
        routinely scoped to ONE row, so a global "latest delivery" would read every other row as
        empty. Picks with no `run_id` are skipped — `DELETE /api/runs` and the retention prune both
        detach them, and a pick that cannot be placed in time cannot support a claim about time.
        """
        rows = (
            self._session.query(
                PickRow.user_id,
                PickRow.collection_slug,
                PickRow.section_key,
                PickRow.run_id,
                PickRow.tmdb_id,
                PickRow.media_type,
                PickRow.created_at,
            )
            .filter(PickRow.run_id.isnot(None), PickRow.collection_slug.in_(self._live_slugs))
            .all()
        )
        by_delivery: dict[tuple[int, str, str, int], set[tuple[int, str]]] = defaultdict(set)
        # WHEN this delivery actually landed, from the picks themselves — NOT `Run.started_at`.
        # A run persists each person as they finish, so `created_at` trails the run's start by minutes
        # to tens of minutes (a TV collection write alone costs ~16.5s, times 47 people). Two clocks
        # meant two silent failures: `event_credits` would say a title was in the row while
        # `_apply_outcomes` found no pick old enough to stamp — a credit computed and thrown away,
        # permanently, because a watched title is never re-delivered; and a play during a run was
        # judged against the row the run was BUILDING rather than the one Plex was still serving.
        delivered_at: dict[tuple[int, str, str, int], datetime] = {}
        for user_id, slug, section, run_id, tmdb_id, media_type, created in rows:
            group = (user_id, slug, section, run_id)
            by_delivery[group].add((tmdb_id, media_type))
            when = _as_utc(created)
            if group not in delivered_at or when < delivered_at[group]:
                delivered_at[group] = when

        out: dict[tuple[int, str, str], list[tuple[datetime, set[tuple[int, str]]]]] = defaultdict(list)
        for (user_id, slug, section, run_id), keys in by_delivery.items():
            started = delivered_at.get((user_id, slug, section, run_id))
            if started is None:
                continue
            # Still on Plex for THIS person, per the ledger. Without it a row muted a month ago keeps
            # crediting for ever: `_contained_at` takes the newest delivery at or before T and nothing
            # ever expires it, so the last contents of a row that stopped being delivered stay
            # "in their row" indefinitely — the exact regression the membership rule was built to fix.
            if (self._slug_by_user.get(user_id), slug, section) not in self._on_plex:
                continue
            out[(user_id, slug, section)].append((started, keys))
        for timeline in out.values():
            timeline.sort(key=lambda entry: entry[0])
        return out

    def _load_shared(self) -> dict[str, list[tuple[datetime, set[tuple[int, str]]]]]:
        """Shared rows, same timeline shape, from `run_shared_rows.picks`.

        Shared rows write no pick rows at all — `RunSharedRow`'s docstring records why — so their
        contents come out of that JSON.

        `picks` has carried `tmdb_id` since migration 0066; what older rows lack is `media_type` (added
        to `_pick_dicts` 2026-08-24) and the `audience` snapshot (0076). Both absences make a row
        un-creditable, via `_shared_key` and `_shared_visible_to` — and the second is the one that
        matters, because NULL audience cannot be told apart from "public".
        """
        out: dict[str, list[tuple[datetime, set[tuple[int, str]]]]] = defaultdict(list)
        # Oldest run first, so the title map below ends up holding the NEWEST rendering of each title
        # rather than whichever row SQLite happened to return first.
        for slug, run_id, picks, audience, muted, delivered_at in (
            self._session.query(
                RunSharedRow.collection_slug,
                RunSharedRow.run_id,
                RunSharedRow.picks,
                RunSharedRow.audience,
                RunSharedRow.muted,
                RunSharedRow.delivered_at,
            )
            # NEVER a dry run. `_persist_shared_row_report` writes `picks`, `audience` and
            # `delivered_at` unconditionally — only the delivery-ledger write is gated — so a preview
            # would otherwise enter this timeline as the NEWEST delivery. Both directions are wrong
            # and both last: a real watch of a title that was genuinely on the shelf becomes
            # uncreditable, and a title that only ever existed in a preview is credited as though the
            # row had shown it. The owner's own runbook says to dry-run first, always, so this is the
            # normal path rather than an edge case.
            #
            # Filtered on READ rather than gated on write, so the run page keeps showing what a
            # preview would have delivered — and so dry-run rows already in the database are fixed
            # rather than only new ones.
            .join(Run, Run.id == RunSharedRow.run_id)
            .filter(Run.dry_run.isnot(True))
            .order_by(RunSharedRow.run_id)
            .all()
        ):
            # Recorded before the guard below, because `_delivered_at` is the ONE place the "when did
            # this row land" rule lives and it reads this map. That rule was written twice — here and
            # in the helper — and two copies of the clock that decides credit TIMING is precisely the
            # drift this feature has been bitten by. Rows the guard skips never reach `_audience`, and
            # nothing reads a delivery time for a slug that is not in `_audience`.
            if delivered_at:
                self._shared_delivered[(slug, run_id)] = _as_utc(delivered_at)
            # The row's OWN delivery time, falling back to the run's start only for rows written
            # before that column existed. See `RunSharedRow.delivered_at`: a run persists each row as
            # it finishes, so its start can be tens of minutes early, and a play in that gap is judged
            # against contents Plex was not serving yet.
            started = self._delivered_at(slug, run_id)
            if started is None or slug not in self._live_slugs:
                continue
            # Audience and mutes are filled HERE rather than by a second pass. They came from a
            # separate query over the identical rows — same join, same dry-run filter — which is two
            # reads of one table and two places for the filter to drift apart.
            self._audience[(slug, run_id)] = audience if audience is None else set(audience)
            if muted:
                self._shared_muted[(slug, run_id)] = set(muted)
            keys = {
                key
                for p in (picks or [])
                if isinstance(p, dict) and p.get("tmdb_id")
                if (key := self._shared_key(p)) is not None
            }
            if keys:
                out[slug].append((started, keys))
            for p in picks or []:
                if isinstance(p, dict) and p.get("tmdb_id"):
                    key = self._shared_key(p)
                    if key is not None:
                        # Overwrite, not setdefault — later runs win, so a retitled item shows its
                        # current name. Paired with the ORDER BY above; without it this was arbitrary.
                        self._shared_titles[key] = str(p.get("title") or "")
        for timeline in out.values():
            timeline.sort(key=lambda entry: entry[0])
        return out

    @staticmethod
    def _shared_key(pick: dict) -> tuple[int, str] | None:
        """`(tmdb_id, media_type)` for one shared-row pick, or None when the type is not recorded.

        None rather than a guess, and the guess was tried and reverted. TMDB ids are namespaced per
        type, so an id with the wrong type is a DIFFERENT title and would credit the row for something
        nobody watched. Two ways deriving it from the pick's own rating key went wrong, both real:

        * The key is live in this JSON, which looks like it makes it safe — but
          `_forget_removed_deliveries` in this same package records that Plex REUSES
          `metadata_items.id`. A legacy pick for a film whose key has since been reused by a series
          resolves to a real, different title.
        * Worse, the rows it rescued were exactly those written before migration 0076 — which are the
          rows with NO `audience` snapshot, and `_shared_visible_to` reads NULL as "everyone". So the
          rescue handed a SUBSET row's credits to people who were never in its audience. Refusing
          those rows is what closes that hole; nothing else holds it shut.

        Rows written from 2026-08-24 on carry `media_type` (`_pick_dicts`), so this is a one-run gap on
        existing installs, not a permanent limit.
        """
        media_type = str(pick.get("media_type") or "")
        return (int(pick["tmdb_id"]), media_type) if media_type else None

    def shared_pool(self) -> set[tuple[int, str]]:
        """Every title any live shared row has ever carried.

        The shared-row twin of `event_credits`' `owned` set. It cannot be built from `picks` — that is
        the entire reason shared rows needed their own path — so it comes from the delivered-picks JSON.
        """
        return {key for timeline in self._shared.values() for _at, keys in timeline for key in keys}

    def shared_title(self, key: tuple[int, str]) -> str:
        """The title text as a shared row rendered it, for the credit record's own display."""
        return self._shared_titles.get(key, "")

    def _delivered_at(self, slug: str, run_id: int) -> datetime | None:
        """When that shared row's contents landed, by the same rule `_load_shared` uses."""
        return self._shared_delivered.get((slug, run_id)) or self._run_started.get(run_id)

    @staticmethod
    def _contained_at(
        timeline: list[tuple[datetime, set[tuple[int, str]]]], keys: set[tuple[int, str]], when: datetime
    ) -> bool:
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

    def visible_rows(self, user: User, keys: set[tuple[int, str]], when: datetime) -> list[str]:
        """Every row slug that was showing this title to this person at `when`."""
        when = _as_utc(when)
        found = [
            slug
            for (user_id, slug, _section), timeline in self._per_person.items()
            if user_id == user.id and self._contained_at(timeline, keys, when)
        ]
        # PERSONAL ROWS ONLY. Shared rows are answered by `visible_shared_rows` and credited into
        # `shared_row_watches`, deliberately kept apart: a shared row has no per-user pick row, so
        # letting one satisfy membership HERE would stamp somebody's personal pick for the same title
        # — crediting a personal row for a title it had already dropped, because a different row was
        # still showing it.
        return sorted(set(found))

    def visible_shared_rows(self, user: User, keys: set[tuple[int, str]], when: datetime) -> list[str]:
        """Every SHARED row slug that was showing this title to this person at `when`.

        Three gates, all needed: the row still exists (`_live_slugs`, applied when the timeline is
        built), the collection is on Plex (the delivery ledger — a shared row files under
        `shared_<slug>`), and the person was in its audience at that delivery.
        """
        when = _as_utc(when)
        return sorted(
            slug
            for slug, timeline in self._shared.items()
            if slug in self._shared_on_plex
            and self._contained_at(timeline, keys, when)
            and self._shared_visible_to(slug, user, when)
        )

    def _shared_visible_to(self, slug: str, user: User, when: datetime) -> bool:
        """Was this shared row visible to this person at `when`, per the delivery's own snapshot."""
        best: set[int] | None = None
        best_muted: set[int] = set()
        best_at: datetime | None = None
        for (row_slug, run_id), audience in self._audience.items():
            if row_slug != slug:
                continue
            started = self._delivered_at(row_slug, run_id)
            if started is None or started > when:
                continue
            if best_at is None or started > best_at:
                best_at, best = started, audience
                best_muted = self._shared_muted.get((row_slug, run_id), set())
        # Two questions, and they are separate on purpose. `audience` is the ALLOW-list — None means
        # public, and that is also what every pre-snapshot row carries. `muted` is the deny-list, and
        # it has to stay outside the allow-list or a single mute turns "everyone" into a fixed roster
        # that nobody joined later can be in.
        if user.plex_account_id in best_muted:
            return False
        return best is None or user.plex_account_id in best


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
    events = session.query(WatchEvent)
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
