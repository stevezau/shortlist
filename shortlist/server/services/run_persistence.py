"""Run persistence + audit — everything a finished run leaves behind in SQLite.

The run rows, per-user results, picks, the delivery ledger, the approval inbox, the `events` audit
trail (plex-safety rule 10) and the retention prunes that trim them. Written as plain functions
taking a session (or a session factory) so the retention pass can be driven by the
`maintenance.prune` job without going through a live `RunService`.

Nothing here talks to Plex. It reads a finished ``EngineReport`` and writes what it says.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import and_, func
from sqlalchemy.orm import Session, sessionmaker

from shortlist.engine.models import SHARED_SLUG_PREFIX
from shortlist.engine.requests import QUEUE_REASON_PREFIXES
from shortlist.server.db.models import (
    Collection,
    CollectionAudience,
    CollectionUserOverride,
    Delivery,
    Event,
    PickRow,
    RequestCandidate,
    Run,
    RunLogLine,
    RunSharedRow,
    RunUser,
    SharedRowWatch,
    User,
    WatchEvent,
    WatchSession,
)
from shortlist.server.services import jobs
from shortlist.server.services.audit import add_audit
from shortlist.server.services.watch_events import (
    RowMembership,
    _attribution_floor,
    _scan_plays,
    event_credits,
    session_progress,
    shared_credits,
    tmdb_by_rating_key,
)

# Bounds the effectiveness report's MATURED cohort (a pick delivered more recently than this has not
# had a fair chance to be watched yet). It no longer gates whether a pick is credited: that is
# `reconcile_watched`'s "was it in their row at the time" test, which needs no clock.
#: Watch history never outlives this, whatever `runs.retention` says. It is the one table here that
#: grows with the whole server's viewing rather than with Shortlist's own activity.
WATCH_RETENTION_MONTHS = 6

HIT_WINDOW_DAYS = 30


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _record_deliveries(session: Session, user_slug: str, breakdown: list[dict]) -> None:
    """Upsert this user's delivery-ledger rows from a run's per-(row, library) breakdown.

    The ledger is what lets a later reconcile find a collection it cannot re-derive a title for (a
    `{top_seed}` row renders differently every run). It is written here, on the persist path, so it
    stays in step with what the run actually delivered.

    Entries with no `rating_key` are skipped: legacy breakdowns predate the field, and a dry run never
    reaches this function at all. A row without a key is simply not in the ledger, which leaves the
    reconciles exactly where they were before it existed — the fallback still applies.
    """
    for entry in breakdown or []:
        rating_key = int(entry.get("rating_key") or 0)
        slug, library_key = entry.get("row_slug") or "", str(entry.get("library_key") or "")
        if not rating_key or not slug or not library_key:
            continue
        row = session.get(Delivery, (slug, user_slug, library_key))
        if row is None:
            row = Delivery(collection_slug=slug, user_slug=user_slug, library_key=library_key)
            session.add(row)
        row.rating_key = rating_key
        row.title = entry.get("row_title") or ""
        row.updated_at = datetime.now(UTC)


def _forget_removed_deliveries(session: Session, user_slug: str, removed: list[dict]) -> None:
    """Drop the ledger rows for collections this run DELETED in-run (a muted/retired row, or one a
    cold start skipped), so the ledger stays a record of what IS on the server.

    The on-demand reconciles already do this via `collection_reconcile._forget_deliveries`; an in-run
    removal had no equivalent, so its ratingKey survived its collection. That matters because these
    paths REPEAT — a cold-skipped user is skipped again every night — and Plex reuses
    `metadata_items.id`, so a kept key can come to name a different collection. `promote_user_rows`
    reads this ledger too, so it is not only removals that a stale key would misdirect.
    """
    for entry in removed or []:
        slug, library_key = entry.get("row_slug") or "", str(entry.get("library_key") or "")
        if not slug or not library_key:
            continue
        row = session.get(Delivery, (slug, user_slug, library_key))
        if row is not None:
            session.delete(row)


def _why_json(why) -> list[dict]:
    """Serialize a missing title's provenance for storage + the API: [{user, row, seed, source, row_slug}].

    `row` is the RENDERED name the person sees; `row_slug` is the stable identity beside it, which is
    what resolves the row's Sonarr/Radarr target when the owner approves months later.
    """
    return [{"user": w.user, "row": w.row, "seed": w.seed, "source": w.source, "row_slug": w.row_slug} for w in why]


def _is_failure_detail(detail: str | None) -> bool:
    """Whether a detail describes a send that was ATTEMPTED and failed, rather than a threshold reason
    for one that was never attempted.

    The queue reasons are settings-derived and finite (`engine/requests.py`); anything else came from
    an exception while actually talking to Radarr/Sonarr, and is the fact worth keeping.
    """
    if not detail:
        return False
    return not detail.startswith(QUEUE_REASON_PREFIXES)


def _refresh_pending(row: RequestCandidate, m) -> None:
    """Update a still-pending inbox row with what this run now knows about the title."""
    row.title = m.title
    row.year = m.year
    row.imdb_id = m.imdb_id or row.imdb_id  # keep a known id if a later run couldn't re-fetch it
    # Same rule as imdb_id, and it is also what backfills rows queued before 0044: the first run to
    # re-surface the title fills the artwork in.
    row.poster_path = m.poster_path or row.poster_path
    row.overview = m.overview or row.overview  # same rule again — and the backfill for pre-0071 rows
    row.rating = m.rating
    row.vote_count = m.vote_count
    row.demand = m.demand
    row.tags = sorted(m.tags)
    row.wanters = sorted(m.wanters)
    row.why = _why_json(m.why)
    # Keep the newest claim: that is the row whose target an approval would use now.
    row.row_slug = _row_slug(m) or row.row_slug
    # A queued title now always carries a reason ("max_per_run (5) already filled"), so a plain
    # `m.detail or row.detail` would overwrite yesterday's REAL failure ("Sonarr returned HTTP 503")
    # with today's threshold note — erasing the only record that Sonarr was broken. A failure detail
    # is the more important fact, so it survives until a send actually succeeds.
    if m.detail and (not _is_failure_detail(row.detail) or _is_failure_detail(m.detail)):
        row.detail = m.detail
    row.excluded = m.excluded  # refresh the exclusion flag each run (a removed exclusion clears it)


def _candidate_row(m, run_id: int, *, status: str) -> RequestCandidate:
    """One inbox row for a missing title, in whichever state the run left it."""
    return RequestCandidate(
        tmdb_id=m.tmdb_id,
        media_type=m.media_type.value,
        title=m.title,
        year=m.year,
        imdb_id=m.imdb_id,
        poster_path=m.poster_path,
        overview=m.overview,
        rating=m.rating,
        vote_count=m.vote_count,
        demand=m.demand,
        tags=sorted(m.tags),
        wanters=sorted(m.wanters),
        why=_why_json(m.why),
        status=status,
        detail=m.detail,  # why it is not here yet: a threshold, or a real send failure
        excluded=m.excluded,  # on a Sonarr/Radarr exclusion list — flagged in the inbox
        arr_slug=m.arr_slug,  # set for auto-sent titles, so the inbox deep-links to the arr page
        row_slug=_row_slug(m),
        first_seen_run_id=run_id,
    )


def _row_slug(m) -> str | None:
    """Which row this title came from, for resolving its Sonarr/Radarr target on a later approval.

    Reads the slug the ENGINE recorded on the title, not the first `why` entry. Those diverged the
    moment `_merge_across_rows` began unioning provenance across rows: a title two rows wanted now
    carries both their slugs in `why`, and the one that matters is the row that actually CLAIMED it —
    the row whose Sonarr/Radarr target the run used, and the one a later approval must reuse.

    Falls back to the first recorded slug for a title that was never claimed (everything queued), and
    to None when there is no provenance at all — which sends the approval to the global config, the
    same behaviour as every row queued before per-row settings existed.
    """
    if getattr(m, "row_slug", None):
        return m.row_slug
    return next((w.row_slug for w in (m.why or []) if w.row_slug), None)


def live_pick_ids(session: Session) -> dict[int, set[int]]:
    """The picks that are on Plex RIGHT NOW, as ``{user_id: {pick_id}}``.

    A row+library's live contents are the picks from the MAX ``run_id`` that delivered it — the same
    definition `context_builder._previous_picks` carries into the engine for carry-forward, so "what
    is in their row" means one thing in this codebase rather than two. Grouping per (user, row,
    library) is what makes it correct: rows carry their own crons, so the newest run is routinely
    scoped to ONE row, and taking the newest run overall would read every other row as empty.

    Existing is not the same as ON PLEX, so both are required. The row must still exist and be
    enabled, AND the delivery ledger must still carry its `(row, user, library)` entry — the ledger is
    the record of what is actually on the server, and `_forget_removed_deliveries` drops the entry
    whenever a run REMOVES a collection (a muted or retired row, a cold-start skip, a user leaving the
    audience). Testing `collections` alone would leave those picks creditable for ever: the Plex
    collection is gone, the row definition stays so the owner can switch it back on, and no later run
    re-delivers that group to move its MAX ``run_id``. Measured on the maintainer's server: 184 ledger
    entries against 185 pick groups, the one difference being a row whose collection Plex no longer
    has. The ledger join also drops the blank-`section_key` picks predating multi-row, which no
    `library_key` can match.

    Picks whose run was detached (`DELETE /api/runs`, or the retention prune) have no ``run_id`` and
    so read as not-live until that row next delivers, which re-stamps them. Carry-forward already
    behaves exactly this way — the clear-runs endpoint says so in as many words — and the cost here
    is the same shape: a watch in that window is not credited.
    """
    live_slugs = [slug for (slug,) in session.query(Collection.slug).filter(Collection.enabled.is_(True)).all()]
    if not live_slugs:
        return {}
    # Matched in Python, not as a third SQL join: the ledger is keyed by user SLUG where picks carry
    # user_id, and it is small (one row per row/user/library actually on the server).
    slug_by_user = {uid: slug for uid, slug in session.query(User.id, User.slug).all()}
    on_plex = {
        (row.user_slug, row.collection_slug, row.library_key)
        for row in session.query(Delivery).filter(Delivery.collection_slug.in_(live_slugs))
    }
    latest = (
        session.query(
            PickRow.user_id.label("user_id"),
            PickRow.collection_slug.label("slug"),
            PickRow.section_key.label("section_key"),
            func.max(PickRow.run_id).label("mrun"),
        )
        .filter(PickRow.collection_slug.in_(live_slugs))
        .group_by(PickRow.user_id, PickRow.collection_slug, PickRow.section_key)
        .subquery()
    )
    rows = (
        session.query(PickRow.id, PickRow.user_id, PickRow.collection_slug, PickRow.section_key)
        .join(
            latest,
            and_(
                PickRow.user_id == latest.c.user_id,
                PickRow.collection_slug == latest.c.slug,
                PickRow.section_key == latest.c.section_key,
                PickRow.run_id == latest.c.mrun,
            ),
        )
        .all()
    )
    out: dict[int, set[int]] = {}
    for pick_id, user_id, slug, section_key in rows:
        if (slug_by_user.get(user_id), slug, section_key) not in on_plex:
            continue
        out.setdefault(user_id, set()).add(pick_id)
    return out


@dataclass
class _Outcome:
    """Everything concluded about one (person, title), before any of it is written.

    The whole point is that this is decided ONCE. Five separate call sites used to write these three
    columns — `_credit_from_events`, the candidate loop, `_stamp_percent`, `_spread_credit` and
    `_refresh_finished_progress` — and every ordering bug this feature has had came from that: a
    percentage walking backwards because one writer had the guard and another did not, a credit and a
    percentage landing on different delivery rows, a completion dated before its own credit. With one
    decider and one writer those are not bugs to fix, they are states that cannot be represented.
    """

    #: When the row got them to press play. Never moves once set: the hit belongs to the week it
    #: happened, and a rewatch three weeks later is not when the recommendation worked.
    watched_at: datetime | None = None
    finished_at: datetime | None = None
    #: Furthest observed, 0-100. None means no live session watched it happen — which is NOT 0%.
    max_percent: int | None = None
    #: The rows that may carry this credit. A row that had already dropped the title must not collect
    #: a hit its shelf could not have caused (`row_effectiveness` counts per `collection_slug`).
    slugs: frozenset[str] = frozenset()


def _decide_outcomes(
    session: Session,
    user: User,
    *,
    credits: dict[tuple[int, int, str], tuple[datetime, frozenset[str]]],
    progress: dict,
    latest_watch: dict[tuple[int, str], datetime],
    finished_keys: set[tuple[int, str]],
    live_pick_ids_for_user: set[int],
) -> dict[tuple[int, str], _Outcome]:
    """Work out what is true for this person, from every source, without writing anything.

    Two ways a credit is justified, in priority order:

    1. **An event** — the play log or a live session, at a moment `RowMembership` confirms the title
       was in a row they could see. This is the real timestamp and it knows which rows.
    2. **The snapshot** — Plex flagged the title watched with no play behind it. 893 of one real
       user's 1,840 watched titles are in this state (marked by hand, bulk-marked, or watched before
       the log existed), so it is not a fallback for rare cases; it is half the data.
    """
    desired: dict[tuple[int, str], _Outcome] = defaultdict(_Outcome)

    # 1. Event credits — the strongest evidence, and the only one that knows the exact moment.
    for (user_id, tmdb_id, media_type), (when, slugs) in credits.items():
        if user_id != user.id:
            continue
        out = desired[(tmdb_id, media_type)]
        out.watched_at = when if out.watched_at is None else min(out.watched_at, when)
        out.slugs |= slugs

    # 2. The snapshot path, for titles Plex calls watched with no event behind them. Bounded to rows
    #    currently on the person's shelf, and to a watch no earlier than THAT ROW first showed it —
    #    per row, because a `rewatch` row leading with titles they have already seen would otherwise
    #    inherit another row's older delivery and credit a watch from before it existed.
    if live_pick_ids_for_user and latest_watch:
        first_delivered: dict[tuple[str, int, str], datetime] = {
            (slug, tid, mt): _as_utc(when)
            for slug, tid, mt, when in session.query(
                PickRow.collection_slug, PickRow.tmdb_id, PickRow.media_type, func.min(PickRow.created_at)
            )
            .filter(PickRow.user_id == user.id)
            .group_by(PickRow.collection_slug, PickRow.tmdb_id, PickRow.media_type)
            .all()
        }
        for pick in (
            session.query(PickRow).filter(PickRow.user_id == user.id, PickRow.id.in_(live_pick_ids_for_user)).all()
        ):
            key = (pick.tmdb_id, pick.media_type)
            watched = latest_watch.get(key)
            # A percentage is a fact ABOUT a credited watch, never a reason to invent one — and this
            # line is where that invariant is established, so step 4 below can stamp unconditionally.
            # `desired` is a defaultdict, so READING it here mints an entry. That is not a style nit:
            # an entry created on the reject path below survives, collects a percentage at step 4, and
            # passes the filter at the end — so a title this branch explicitly refused to credit came
            # out the far side as a "dropped" pick, dated to today's delivery. Membership decides the
            # credit; a bare lookup must never be what decides it exists.
            if watched is None or (key in desired and desired[key].watched_at is not None):
                continue
            since = first_delivered.get((pick.collection_slug, *key))
            if since is None or watched < since:
                continue  # recommending something they had already seen is not a hit
            out = desired[key]
            out.watched_at = watched
            out.slugs |= {pick.collection_slug}

    # 3. Completion. Deliberately NOT membership-gated, and that is the whole reason it is a separate
    #    source: being watched is what makes the engine drop a title from a row, so by the time
    #    someone finishes a series months later the row has long since moved on. Re-testing membership
    #    would refuse to upgrade a single "started" to "finished", ever.
    #
    #    Covers two groups: titles credited in THIS pass (above), and titles credited in an earlier
    #    one that are only now complete — which is the normal shape for a series.
    already_credited = {
        (tmdb_id, media_type)
        for tmdb_id, media_type in session.query(PickRow.tmdb_id, PickRow.media_type)
        .filter(PickRow.user_id == user.id, PickRow.watched_at.isnot(None), PickRow.finished_at.is_(None))
        .distinct()
        .all()
    }
    for key in (set(desired) | already_credited) & finished_keys:
        completed = latest_watch.get(key, desired[key].watched_at)
        if completed is not None:
            desired[key].finished_at = completed

    # 4. Progress, from live sessions only. Series get none: an episode's percentage is not the
    #    show's, and one episode of sixty arriving as 100% told the dashboard people abandon shows
    #    just before the end.
    #
    #    Stamped unconditionally, which is safe ONLY because every key in `desired` already carries a
    #    credit or a completion by this point — see the `key in desired` guard in step 2. A future
    #    source that mints a bare entry would reintroduce a percentage on an uncredited title, which
    #    surfaces as a "dropped" pick nobody was ever recommended.
    for key in list(desired):
        found = progress.get((user.plex_account_id, key[0], key[1]))
        if found and found[1] is not None:
            desired[key].max_percent = found[1]

    return {
        key: out
        for key, out in desired.items()
        if out.watched_at is not None or out.max_percent is not None or out.finished_at is not None
    }


def _apply_outcomes(session: Session, user: User, desired: dict[tuple[int, str], _Outcome]) -> int:
    """Write the decided state onto every delivery row it belongs on. The ONLY writer.

    The report intersects at ROW level — `_landing` and `row_effectiveness` pick a cohort by
    `created_at` and count rows in it that also carry `watched_at` — so a credit on one delivery row
    and a percentage on another is invisible to it. Every row for the title gets the same answer here,
    subject to two bounds that are the point of the whole feature:

    * `created_at <= watched_at` — a delivery made after they watched it cannot be why they did;
    * `collection_slug in slugs` — only the rows that were actually showing it.
    """
    # ONE query for the user, grouped in Python — not one per title. A real server carries ~139
    # credited-but-unfinished titles per person plus the day's credits, across 47 people, seven times
    # a day, against a single SQLite writer.
    by_title: dict[tuple[int, str], list[PickRow]] = defaultdict(list)
    if desired:
        for pick in (
            session.query(PickRow)
            .filter(
                PickRow.user_id == user.id,
                PickRow.tmdb_id.in_({tmdb_id for tmdb_id, _mt in desired}),
            )
            .all()
        ):
            by_title[(pick.tmdb_id, pick.media_type)].append(pick)

    wrote = 0
    for (tmdb_id, media_type), out in desired.items():
        for pick in by_title.get((tmdb_id, media_type), []):
            before = (pick.watched_at, pick.finished_at, pick.max_percent)
            eligible = (
                out.watched_at is not None
                and _as_utc(pick.created_at) <= out.watched_at
                and (not out.slugs or pick.collection_slug in out.slugs)
            )
            if eligible and pick.watched_at is None:
                pick.watched_at = out.watched_at
            # Completion lands on any row already carrying a credit, whether or not this pass added
            # one — a series finished months after the row dropped it still finished.
            if out.finished_at is not None and pick.finished_at is None and (eligible or pick.watched_at is not None):
                # Never before this row's own credit.
                pick.finished_at = max(out.finished_at, _as_utc(pick.watched_at or out.finished_at))
            # Progress is monotonic and applies to any row of the title, credited or not: retention
            # deletes old sessions, so the maximum a session can report SHRINKS over time, and a fresh
            # short session must never overwrite a real earlier one.
            if out.max_percent is not None and (pick.max_percent is None or pick.max_percent < out.max_percent):
                pick.max_percent = out.max_percent
            # Counted, not assumed. `desired` is recomputed from the whole event log every pass, so
            # "this user has a credit" is true for everyone who has ever watched anything — using it
            # as "something changed" made one person pressing stop report all 47 users as credited
            # and told every open dashboard to refetch.
            if (pick.watched_at, pick.finished_at, pick.max_percent) != before:
                wrote += 1
    return wrote


def _decide_shared(
    user: User,
    *,
    shared: dict[tuple[int, str, int, str], datetime],
    existing: dict[tuple[str, int, str], SharedRowWatch],
    progress: dict,
    latest_watch: dict[tuple[int, str], datetime],
    finished_keys: set[tuple[int, str]],
) -> dict[tuple[str, int, str], _Outcome]:
    """What is true for this person on the SHARED rows, decided and written nowhere.

    The shared twin of `_decide_outcomes`, and much the smaller of the two because it has only ONE
    source. There is no snapshot path here on purpose: the snapshot asks "is this title on their shelf
    NOW and did Plex flag it watched", and for a shared row that question cannot distinguish the row
    doing its job from the title merely being popular — everyone sees a shared row, so every watch of
    every title on it would credit. An event with a timestamp is the only evidence that survives that.
    """
    desired: dict[tuple[str, int, str], _Outcome] = {}
    for (user_id, slug, tmdb_id, media_type), when in shared.items():
        if user_id != user.id:
            continue
        out = desired.setdefault((slug, tmdb_id, media_type), _Outcome())
        out.watched_at = when if out.watched_at is None else min(out.watched_at, when)

    # Completion, ungated on membership for the same reason as the personal path: finishing a series
    # months later happens long after the row moved on. Covers this pass's credits and any earlier
    # one still open.
    open_before = {key for key, row in existing.items() if row.watched_at is not None and row.finished_at is None}
    for key in set(desired) | open_before:
        title_key = (key[1], key[2])
        if title_key not in finished_keys:
            continue
        out = desired.setdefault(key, _Outcome())
        completed = latest_watch.get(title_key, out.watched_at)
        if completed is not None:
            out.finished_at = completed

    # Films only — an episode's percentage is not the series', so `session_progress` returns None for
    # a show and nothing is written.
    for key, out in desired.items():
        found = progress.get((user.plex_account_id, key[1], key[2]))
        if found and found[1] is not None:
            out.max_percent = found[1]

    return {k: v for k, v in desired.items() if any((v.watched_at, v.finished_at, v.max_percent))}


def _apply_shared(
    session: Session,
    user: User,
    desired: dict[tuple[str, int, str], _Outcome],
    existing: dict[tuple[str, int, str], SharedRowWatch],
    membership: RowMembership,
) -> int:
    """Write the decided shared-row state. The ONLY writer for `shared_row_watches`."""
    wrote = 0
    for key, out in desired.items():
        slug, tmdb_id, media_type = key
        row = existing.get(key)
        created = row is None
        if row is None:
            row = SharedRowWatch(
                user_id=user.id,
                collection_slug=slug,
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=membership.shared_title((tmdb_id, media_type)),
            )
            session.add(row)
            existing[key] = row
        before = (row.watched_at, row.finished_at, row.max_percent)
        if row.watched_at is None and out.watched_at is not None:
            row.watched_at = out.watched_at
        # Only onto a credited watch, and never before its own credit.
        if out.finished_at is not None and row.finished_at is None and row.watched_at is not None:
            row.finished_at = max(out.finished_at, _as_utc(row.watched_at))
        # Monotonic, same as `picks.max_percent`: retention deletes old sessions, so the maximum a
        # session can report shrinks over time and a fresh short one must not overwrite a real
        # earlier one.
        if out.max_percent is not None and (row.max_percent is None or row.max_percent < out.max_percent):
            row.max_percent = out.max_percent
        if created or (row.watched_at, row.finished_at, row.max_percent) != before:
            wrote += 1
    return wrote


#: How recent a credit must be for its ABSENCE to be read as an un-watch.
#:
#: A title goes missing from a watched-titles read for two very different reasons: the person
#: un-watched it, or it is no longer in the library at all. Nothing distinguishes them — the read is
#: "everything in this section with the watched flag set", and a deleted file is in no section. So an
#: unbounded withdrawal would erase a year of hit-rate history the first time the owner tidied up
#: their movies folder, silently, on the weekly pass.
#:
#: Bounding it keeps the case this exists for — Plex flags something watched wrongly and the person
#: corrects it, which happens within days — and leaves settled history alone. The same 30 days the
#: report already treats as a pick's fair chance.
UNWATCH_WITHDRAW_DAYS = HIT_WINDOW_DAYS


def _withdraw_unwatched(
    session: Session,
    user: User,
    latest_watch: dict[tuple[int, str], datetime],
    observed: set[tuple[int, str]],
    *,
    now: datetime,
) -> int:
    """Take back credits that Plex's flag was the ONLY evidence for, once that flag is gone.

    Someone can un-watch a title, and Plex marks things watched wrongly often enough that correcting
    it is normal housekeeping — a stray scrobble from a phone that woke in a pocket, a client
    reporting a play for a trailer. Nothing here ever withdrew a credit, so one bad flag counted
    toward the hit rate for ever and the dashboard's headline could only ever drift upward.

    Two credits are NOT withdrawn, and the distinction is the whole point:

    * **Anything we watched happen.** A `watch_sessions` row or a play-log event means we saw them
      press play. Un-marking it in Plex afterwards does not undo that the row worked — the credit is
      a fact about a moment, not a mirror of a checkbox. This is also what protects a partial watch,
      which never sets the flag at all and would otherwise be withdrawn the instant it was credited.
    * **Anything outside this read.** See the caller: only a FULL re-read can tell "they un-watched
      it" from "this pass did not look".
    * **Anything settled.** See `UNWATCH_WITHDRAW_DAYS`: past that, a title missing from the read is
      far more likely to have left the library than to have been un-watched.

    Returns how many were withdrawn, for the log.
    """
    cutoff = now - timedelta(days=UNWATCH_WITHDRAW_DAYS)
    withdrawn = 0
    for pick in session.query(PickRow).filter(PickRow.user_id == user.id, PickRow.watched_at.isnot(None)).all():
        key = (pick.tmdb_id, pick.media_type)
        if key in latest_watch or key in observed:
            continue
        if _as_utc(pick.watched_at) < cutoff:
            continue  # settled history — see UNWATCH_WITHDRAW_DAYS
        pick.watched_at = None
        pick.finished_at = None
        # And the percentage with it. `resolve_outcomes` derives bounced/dropped from `max_percent`
        # ALONE, so a withdrawn pick that kept one still renders as an abandonment with no credit
        # behind it — the exact "a percentage on an uncredited title" state step 4 of
        # `_decide_outcomes` calls a bug.
        pick.max_percent = None
        withdrawn += 1
    return withdrawn


def reconcile_from_events(sessions: sessionmaker[Session]) -> int:
    """Apply the credits that PLAYBACK alone can justify, reading nothing from Plex. Returns the
    number of people whose picks changed.

    The cheap sibling of :func:`reconcile_watched`, for the moment a live session settles. Someone
    watches twenty minutes of a pick and stops; the row earned that, and until this existed the fact
    sat in `watch_sessions` until the next scheduled sync hours later — so the dashboard showed
    nothing, and the owner reasonably concluded tracking was broken.

    Deliberately NOT the full reconcile:

    * **No Plex reads.** Everything here is already in our database. The full pass re-reads every
      user's watched set from the PMS, which is minutes of work and pointless in response to one
      person pressing stop.
    * **No snapshot path.** That path exists for titles Plex flagged watched with no play behind
      them; by definition a settling session is not one of those.
    * **`history_depth` is not touched.** It means "how many titles this person has watched", answered
      by the history read this function deliberately skips. Writing it from an empty history is
      exactly the bug `reconcile_watched` documents — every user reading "0 titles watched".

    Idempotent, like every job handler: it recomputes from the same events and writes the same
    answers, and `_apply_outcomes` never moves a stamp that is already there.
    """
    changed = 0
    with sessions() as session:
        membership = RowMembership(session)
        tmdb_of = tmdb_by_rating_key(session)
        scan = _scan_plays(session, tmdb_of)
        credits = event_credits(session, membership, scan)
        shared = shared_credits(session, membership, scan)
        if not credits and not shared:
            return 0
        progress = session_progress(session, _attribution_floor(session), tmdb_of)

        # Only the people an event actually names. The full pass walks every profile it was handed;
        # here that would be 47 users to apply at most one person's watch.
        touched = {user_id for user_id, _t, _m in credits} | {user_id for user_id, _s, _t, _m in shared}
        shared_rows: dict[int, dict[tuple[str, int, str], SharedRowWatch]] = defaultdict(dict)
        for row in session.query(SharedRowWatch).filter(SharedRowWatch.user_id.in_(touched)).all():
            shared_rows[row.user_id][(row.collection_slug, row.tmdb_id, row.media_type)] = row

        for user in session.query(User).filter(User.id.in_(touched), User.removed_at.is_(None)).all():
            desired = _decide_outcomes(
                session,
                user,
                credits=credits,
                progress=progress,
                latest_watch={},
                finished_keys=set(),
                # Empty: the snapshot path is what this argument drives, and it is deliberately not
                # run here. Passing the live set would credit a title Plex flagged watched, from a
                # function that never asked Plex anything.
                live_pick_ids_for_user=set(),
            )
            existing = shared_rows[user.id]
            shared_desired = _decide_shared(
                user, shared=shared, existing=existing, progress=progress, latest_watch={}, finished_keys=set()
            )
            if not desired and not shared_desired:
                continue
            wrote = _apply_outcomes(session, user, desired)
            wrote += _apply_shared(session, user, shared_desired, existing, membership)
            if wrote:
                changed += 1
        session.commit()
    return changed


def reconcile_watched(
    sessions: sessionmaker[Session],
    profiles,
    live_picks: dict[int, set[int]] | None = None,
    *,
    full_resync: bool = False,
) -> None:
    """Mark the picks a person actually watched — the hit rate, and the whole point of the app.

    `picks.watched_at` was declared, migrated and read by the hit-rate query, but never WRITTEN:
    every user's hit rate was structurally 0%, while the docs promised "expect 20-40%".

    **A pick is credited only if the title was in one of their LIVE rows at the time.** It used to be
    credited on a 30-day clock from delivery with no membership test at all, which credited a title
    the row had dropped weeks earlier — they could not have watched it from a shelf that no longer
    showed it, so ~27 of those 30 days were only ever measuring "they found it some other way". This
    cannot be done by asking "is it in the row now" at the far end of a run: the engine drops titles
    the person has watched, so the rebuild removes a title *because* it was watched, and every real
    hit would score zero. Hence `live_picks` — a snapshot taken BEFORE the rebuild (see
    `RunService.start_run`), which is what was on their shelf during the window they were watching in.

    `history_depth` is refreshed here too; it was likewise surfaced in the UI and written nowhere, so
    every user read "0 titles watched".

    `finished_at` is stamped alongside, and answers the harder question. `watched_at` comes from
    Plex's binary flag, which for a SERIES flips on the first finished episode — so it has always
    scored one episode of a 60-episode show like a whole film (measured 2026-08-16: only 21 of 158
    credited show picks were actually finished). See `WatchedItem.is_finished` for the threshold and
    why it is ours to choose rather than Plex's to report.

    Completion is deliberately NOT gated on membership: once a pick is credited, the title leaves the
    row (that is what being watched does), so re-testing membership when they finish a series months
    later would refuse to upgrade a single "started" to "finished".

    A new credit is carried onto every delivery row of that title by :func:`_apply_outcomes`, subject
    to its two bounds — `created_at <= watched_at` (a delivery made after they watched cannot be why)
    and `collection_slug in slugs` (only rows that were actually showing it). The membership test
    decides IF; those bounds decide where the report can see it.

    Args:
        sessions: Session factory; one session covers the whole reconcile.
        profiles: The profiles whose `history` this pass read. An empty history contributes nothing.
        live_picks: What was in each person's rows before this run rebuilt them, from
            :func:`live_pick_ids`. Computed fresh when omitted — correct for the standalone watch
            sweep, which rebuilds no rows, and wrong for a run, which already has.
    """
    with sessions() as session:
        # Membership is now a question about the PAST — "was this in their row when they pressed
        # play" — answered from the play log's exact timestamps against the delivery history in
        # `picks` + `runs`. `live_picks` is the old snapshot path, kept only for the callers that
        # have no event for a watch (a title Plex marked watched with no play recorded, which is 48%
        # of one real user's watched set) and therefore still have to ask about now.
        membership = RowMembership(session)
        # Built ONCE and handed to everything below. `tmdb_by_rating_key` is a DISTINCT over the
        # largest table in the schema (158,737 pick rows on a real server) and `_scan_plays` walks the
        # whole event log; between them the credit path was rebuilding both up to five times per pass,
        # seven passes a day, for byte-identical results.
        tmdb_of = tmdb_by_rating_key(session)
        scan = _scan_plays(session, tmdb_of)
        # Every title we have WATCHED HAPPEN for each account — a session or a play-log entry.
        # Used only to decide what `_withdraw_unwatched` must keep its hands off.
        observed: dict[int, set[tuple[int, str]]] = defaultdict(set)
        for account_id, _when, keys in scan:
            observed[account_id] |= keys
        credits = event_credits(session, membership, scan)
        # Shared rows credit into their own table: they write no pick rows, so before this they were
        # invisible to watch tracking entirely — a title that lived only on a shared row credited
        # nothing at all.
        shared = shared_credits(session, membership, scan)
        shared_rows: dict[int, dict[tuple[str, int, str], SharedRowWatch]] = defaultdict(dict)
        for row in session.query(SharedRowWatch).all():
            shared_rows[row.user_id][(row.collection_slug, row.tmdb_id, row.media_type)] = row
        # How far each title actually got, keyed the same way the events are. Stamped onto the pick so
        # the report can separate "opened and closed" from "gave it a real go" without joining
        # sessions on every read.
        progress = session_progress(session, _attribution_floor(session), tmdb_of)
        # The snapshot path is NOT redundant now that events exist, and was very nearly deleted as
        # such. It credits a title Plex flagged as watched with no play event behind it — 893 of one
        # real user's 1,840 watched titles, 48%: marked watched by hand, bulk-marked, or watched
        # before the log existed. None of those ever generated an event, so the event path is blind
        # to them. It runs first and wins where it has an answer; this catches the rest.
        live = live_picks if live_picks is not None else live_pick_ids(session)
        for profile in profiles:
            user = session.query(User).filter_by(slug=profile.slug).first()
            if user is None:
                continue
            # Only when this pass actually READ their history. A scoped run — "the common case,
            # not the rare one" per `WatchSync.has_a_row_in_scope` — leaves every out-of-scope profile
            # with an empty list, and writing that reintroduced the exact bug the docstring above
            # says this line fixed: "every user read 0 titles watched", now for the majority of
            # people on most nights. An empty history with no prior value still writes 0, which
            # is the truth for someone who genuinely has none.
            if profile.history or "history_depth" not in (user.prefs or {}):
                user.prefs = {**(user.prefs or {}), "history_depth": len(profile.history)}

            latest_watch: dict[tuple[int, str], datetime] = {}
            # Titles they have FINISHED, not merely started (`WatchedItem.is_finished`). A movie is
            # always here; a series only once every episode is watched.
            finished_keys: set[tuple[int, str]] = set()
            for item in profile.history:
                if item.tmdb_id is None:
                    continue
                key = (item.tmdb_id, str(item.media_type))
                when = _as_utc(item.watched_at)
                if key not in latest_watch or when > latest_watch[key]:
                    latest_watch[key] = when
                if item.is_finished:
                    finished_keys.add(key)

            # DECIDE, then WRITE. Not gated on `latest_watch`: that is Plex's binary flag, and the two
            # cases this feature exists for do not set it — a title the row has since dropped, and a
            # partial watch, which never flips the flag at all.
            desired = _decide_outcomes(
                session,
                user,
                credits=credits,
                progress=progress,
                latest_watch=latest_watch,
                finished_keys=finished_keys,
                live_pick_ids_for_user=live.get(user.id, set()),
            )
            _apply_outcomes(session, user, desired)

            # Only on a FULL re-read, and only for someone whose history actually came back. An
            # incremental read sees an un-watch only inside the window it covered (see
            # `WatchSync._full_resync_due`), so "absent from this read" would withdraw half a
            # roster's credits on any night the cursor was narrow. An EMPTY history is excluded for
            # the same reason from the other direction: a read that failed and a person who has
            # watched nothing are indistinguishable here, and wrongly wiping real history is far
            # worse than leaving one stale credit for someone who un-watched their only title.
            if full_resync and profile.history:
                gone = _withdraw_unwatched(
                    session,
                    user,
                    latest_watch,
                    observed.get(user.plex_account_id, set()),
                    now=datetime.now(UTC),
                )
                if gone:
                    logger.info("watch-sync: withdrew {} un-watched credit(s) for {}", gone, user.username)

            existing = shared_rows[user.id]
            _apply_shared(
                session,
                user,
                _decide_shared(
                    user,
                    shared=shared,
                    existing=existing,
                    progress=progress,
                    latest_watch=latest_watch,
                    finished_keys=finished_keys,
                ),
                existing,
                membership,
            )
        session.commit()


def persist_user_live(
    sessions: sessionmaker[Session],
    persist_lock: threading.Lock,
    run_id: int,
    profile,
    user_report,
    dry_run: bool,
) -> None:
    """Persist ONE user's results as they finish (called from the engine's worker threads), so the
    run page shows each person on completion rather than the whole roster only at run's end. Its
    commits are serialized (SQLite single-writer) and it never re-writes a user already stored, so
    the end-of-run `persist_report` stays a safe backstop + reconciler. A shared-row/unknown slug
    has no user row here and is handled only at run end."""
    with persist_lock, sessions() as session:
        user = session.query(User).filter_by(slug=profile.slug).first()
        if user is None:
            return
        if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is not None:
            return
        _persist_user_report(session, run_id, user, user_report, dry_run)
        session.commit()


def persist_report(
    sessions: sessionmaker[Session], run_id: int, report, *, status: str | None = None, error: str | None = None
) -> None:
    """Persist a run's outcome. `status`/`error` override what the report says — the gated
    path uses them so a refused run is never even momentarily written as a success."""
    with sessions() as session:
        run = session.get(Run, run_id)
        users_by_slug = {u.slug: u for u in session.query(User).all()}
        # Skipped is its OWN outcome, not a success: a skipped user built nothing, and folding
        # them into `ok` made a run where every single person was skipped report "3 succeeded ·
        # all succeeded" above three rows badged "Skipped".
        ok = errors = skipped = 0
        for user_report in report.users:
            user = users_by_slug.get(user_report.slug)
            if user is None:
                # A SHARED row files its report under `shared_<slug>`, which is nobody's user
                # slug — so this `continue` silently dropped it: a real Plex collection was
                # created, labelled and promoted with no run record and NO AUDIT EVENT at all
                # (plex-safety rule 10), and a failed shared row produced an errored run with
                # nothing to show for it.
                if user_report.slug.startswith(f"{SHARED_SLUG_PREFIX}_"):
                    if user_report.status == "error":
                        errors += 1
                    elif user_report.status == "skipped":
                        skipped += 1
                    # The event is the AUDIT record (rule 10) and stays. The row beside it is the
                    # queryable one: the event carries status and diff titles, but not the trace,
                    # breakdown, token spend or picks, so "why did this row pick that" had no answer.
                    _persist_shared_row_report(session, run_id, user_report, report.dry_run)
                    _emit_shared_row_event(session, run_id, user_report, report.dry_run)
                continue
            if user_report.status == "error":
                errors += 1
            elif user_report.status == "skipped":
                skipped += 1
            else:
                ok += 1
            # Skip anyone already written by the live per-user persist — still counted above for
            # the finalize stats. This backstops users the live path missed (e.g. it errored).
            if session.query(RunUser).filter_by(run_id=run_id, user_id=user.id).first() is None:
                _persist_user_report(session, run_id, user, user_report, report.dry_run)
        _emit_sweep_event(session, run_id, report)
        _emit_privacy_sync_events(session, run_id, report)
        _emit_hub_ordering_events(session, run_id, report)
        _emit_request_events(session, run_id, report)
        persist_request_queue(session, run_id, report)
        if report.error:
            _add_event(session, "run", "error", run_id, error=report.error)
        _finalize_run(run, report, status, error, ok, errors, skipped)
        session.commit()
    # Retention is applied AFTER this transaction commits, as its own `maintenance.prune` job.
    # It used to share this transaction: a bulk delete across runs/run_users/run_log_lines/picks
    # that failed took the persist down with it, discarding the results of a run that had already
    # written to Plex. Housekeeping must never be able to cost a run its record.
    _queue_retention_prune(sessions)


def _queue_retention_prune(sessions: sessionmaker[Session]) -> None:
    """Queue the retention pass. Never raises — the run is already persisted and safe."""
    try:
        jobs.enqueue(sessions, "maintenance.prune")
    except Exception as e:
        logger.warning("could not queue the retention prune ({}) — the next run will", type(e).__name__)


def prune_expired_cache(session: Session) -> int:
    """Drop cache rows whose TTL has passed.

    `DbCache.get` filters on `expires_at`, but nothing ever DELETED an expired row, so the table
    only grew. `library_index` is the worst of them: its key deliberately changes whenever the
    library changes, so every library edit stranded a whole-library JSON blob that could never be
    read again — in the same file the nightly backup copies in full and keeps ten of.
    """
    from shortlist.server.db.models import CacheRow

    removed = session.query(CacheRow).filter(CacheRow.expires_at < time.time()).delete(synchronize_session=False)
    if removed:
        logger.info("pruned {} expired cache row(s)", removed)
    return removed


def prune_runs(session: Session, retention_months: int) -> int:
    """Delete runs older than `retention_months`, returning how many went. 0 = keep forever.

    What is pruned: the run row, its per-user results/traces, and its activity log — the storage
    hog (~100 KB per user per run in trace blobs).

    What is NOT, and must never be:

    * **picks** — the impact ledger. Their `run_id` is nulled, not the row deleted, so the
      dashboard's history survives a run being pruned.
    * **deliveries** — the record of which Plex collection is which row for which person. It is
      keyed by SLUG rather than by foreign key precisely so it outlives runs and rows; deleting
      it would strand real collections on real users' servers with nothing left that knows to
      clean them up.
    * **shared_row_watches** — the impact ledger for SHARED rows, and kept for the same reason as
      `picks`. It is deliberately NOT on the watch-table ceiling below: that ceiling exists because
      `watch_events`/`watch_sessions` grow with every play by every account on the server, while this
      is bounded by (people x titles a shared row has recommended) and is the only record that a
      shared row ever worked.
    """
    # Watch history ages out FIRST, on its own ceiling, before any of the run bookkeeping below. It is
    # not tied to a run and it grows with every play by every account on the server rather than with
    # Shortlist's own activity — so behind the run guards it grew without bound in three reachable
    # states: `runs.retention = 0` ("keep run history for ever", a supported setting), no run old
    # enough to prune yet, and after `DELETE /api/runs`, which is offered as a way to RECLAIM space.
    watch_cutoff = datetime.now(UTC) - timedelta(days=max(retention_months, WATCH_RETENTION_MONTHS) * 30)
    session.query(WatchEvent).filter(WatchEvent.viewed_at < watch_cutoff).delete(synchronize_session=False)
    session.query(WatchSession).filter(WatchSession.started_at < watch_cutoff).delete(synchronize_session=False)

    if retention_months <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_months * 30)
    old = session.query(Run.id).filter(Run.started_at < cutoff).all()
    old_ids = [rid for (rid,) in old]
    if not old_ids:
        return 0
    session.query(PickRow).filter(PickRow.run_id.in_(old_ids)).update({PickRow.run_id: None}, synchronize_session=False)
    session.query(RunUser).filter(RunUser.run_id.in_(old_ids)).delete(synchronize_session=False)
    # Explicit, not left to the FK's ON DELETE CASCADE: SQLite only enforces foreign keys when
    # `PRAGMA foreign_keys` is on, and a bulk ORM delete does not cascade in Python either.
    session.query(RunLogLine).filter(RunLogLine.run_id.in_(old_ids)).delete(synchronize_session=False)
    session.query(RunSharedRow).filter(RunSharedRow.run_id.in_(old_ids)).delete(synchronize_session=False)
    return session.query(Run).filter(Run.id.in_(old_ids)).delete(synchronize_session=False)


def prune_events(session: Session, retention_months: int) -> int:
    """Trim the audit trail, returning how many events went. 0 = keep forever, the default.

    Separate from run retention on purpose: `events` is the answer to "what changed on whose
    share at 03:31" (plex-safety rule 10), so it is the one thing an operator may want to keep
    far longer than the run detail around it.
    """
    if retention_months <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=retention_months * 30)
    return session.query(Event).filter(Event.ts < cutoff).delete(synchronize_session=False)


def _add_event(session: Session, scope: str, level: str, run_id: int, *, dry_run: bool | None = None, **fields):
    """Append one audit Event, injecting the run_id (and dry_run, where relevant) that every
    emitter shares (plex-safety rule 10). Callers pass only their distinctive message fields."""
    extra = {"dry_run": dry_run} if dry_run is not None else {}
    add_audit(session, scope, level, run_id=run_id, **extra, **fields)


def _emit_shared_row_event(session: Session, run_id: int, user_report, dry_run: bool) -> None:
    """The audit record for a shared row — it has no user, so it gets no RunUser row.

    Rule 10: every write, real or dry-run, leaves a structured event with its diff. "What changed
    on the shared row at 03:31" must be answerable from the UI.
    """
    _add_event(
        session,
        "run.shared",
        "error" if user_report.status == "error" else "info",
        run_id,
        dry_run=dry_run,
        row=user_report.slug,
        status=user_report.status,
        picks=len(user_report.picks),
        error=user_report.error,
        # A shared row gets no RunUser row, so this event is the ONLY place its outcome is
        # recorded — without the reason, "why did my shared row build nothing" is answerable
        # from the container log and nowhere else (issue #3).
        reason=user_report.reason,
        diff=user_report.diff.__dict__ if user_report.diff else {},
    )


def _pick_dicts(user_report) -> list[dict]:
    """A shared row's picks as JSON, matching the field set `get_run` renders for a user's picks.

    Kept out of the `picks` TABLE on purpose: `PickRow.user_id` is non-nullable and RESTRICT-keyed to
    a real account, and nullable-user pick rows would leak titles nobody watched into every per-user
    hit-rate and history query.
    """
    return [
        {
            # The ids come FIRST because they are what makes this blob joinable. Without them a
            # shared row's picks — which live only here, never in `picks` — could not be matched to a
            # watch event at all: the event arrives carrying a rating key, and this carried title and
            # year. Title matching is the kind of guess that has bitten this codebase before.
            "tmdb_id": p.tmdb_id,
            # TMDB ids are namespaced PER TYPE — movie 1399 is not show 1399 — so the id alone does
            # not identify a title. Omitting this made every shared-row credit a silent no-op on the
            # real server: the pool keyed `(tmdb_id, "")` and never intersected the `(tmdb_id,
            # "movie")` the play log resolves to. Found on SFLIX 2026-08-24, not by any test.
            "media_type": p.media_type.value,
            "rating_key": p.rating_key,
            "rank": p.rank,
            "title": p.title,
            "reason": p.reason,
            "seed_title": p.seed_title,
            "sources": list(p.sources),
            "affinity": p.affinity,
            "year": p.year,
            "rating": p.rating,
        }
        for p in user_report.picks
    ]


def _shared_audience(session: Session, slug: str) -> list[int] | None:
    """The plex account ids that could SEE this shared row at delivery; None = everyone.

    Snapshotted per run because `collection_audience` is current state with no history. Without it,
    adding someone to a subset row today would retroactively credit every watch they made before they
    could see the row — attribution would silently change for the past every time the owner edited an
    audience.
    """
    collection = session.query(Collection).filter_by(slug=slug).first()
    if collection is None:
        return None
    # Muted is the OTHER way someone does not get a row, and it applies to a public row too — so a
    # snapshot that only looked at `collection_audience` would credit a shared row for a play by
    # someone who had switched it off. Both are current state with no history, which is exactly why
    # they are snapshotted here rather than read back at report time.
    muted = {
        account_id
        for (account_id,) in session.query(User.plex_account_id)
        .join(CollectionUserOverride, CollectionUserOverride.user_id == User.id)
        .filter(
            CollectionUserOverride.collection_id == collection.id,
            CollectionUserOverride.muted.is_(True),
        )
        .all()
    }
    if collection.audience != "subset":
        # Public: everyone EXCEPT anyone who muted it. `None` still means "no restriction at all", so
        # a row nobody has muted keeps the cheap representation.
        if not muted:
            return None
        everyone = {a for (a,) in session.query(User.plex_account_id).filter(User.removed_at.is_(None)).all()}
        return sorted(everyone - muted)
    audience = {
        account_id
        for (account_id,) in session.query(User.plex_account_id)
        .join(CollectionAudience, CollectionAudience.user_id == User.id)
        .filter(CollectionAudience.collection_id == collection.id)
        .all()
    }
    return sorted(audience - muted)


def _persist_shared_row_report(session: Session, run_id: int, user_report, dry_run: bool) -> None:
    """One shared row's `run_shared_rows` record, and its delivery-ledger entries.

    The ledger write is not incidental. `_record_deliveries` was only ever called from
    `_persist_user_report`, which a shared row never reached — so a shared row's collections were
    absent from the ledger entirely, and a later reconcile had no ratingKey to find a row whose title
    it cannot re-derive. `delivery.py` already files them under this same `shared_<slug>` key.
    """
    slug = user_report.slug.removeprefix(f"{SHARED_SLUG_PREFIX}_")
    breakdown = user_report.breakdown or []
    # As RENDERED this run, from what was actually delivered — a row renamed later must not rewrite
    # what a past run says it built. Falls back to the slug when nothing was delivered (a skip).
    row_title = next((entry.get("row_title") or "" for entry in breakdown if entry.get("row_title")), slug)
    # Upsert: `persist_report` is a backstop that can run over a row a live path already wrote, and a
    # second INSERT on the same (run_id, collection_slug) would raise and cost the whole persist.
    row = session.get(RunSharedRow, (run_id, slug))
    if row is None:
        row = RunSharedRow(run_id=run_id, collection_slug=slug)
        session.add(row)
    row.row_title = row_title
    row.status = user_report.status
    row.error = user_report.error
    row.reason = user_report.reason
    row.duration_ms = int(user_report.duration_s * 1000)
    row.llm_tokens = user_report.llm_tokens
    row.llm_tokens_by_step = dict(user_report.llm_tokens_by_step)
    row.exa_searches = user_report.exa_searches
    row.diff = user_report.diff.__dict__ if user_report.diff else {}
    row.breakdown = breakdown
    row.trace = user_report.trace
    row.picks = _pick_dicts(user_report)
    row.audience = _shared_audience(session, slug)
    # Stamped HERE, as the row is persisted, which is the moment its contents are on Plex. See the
    # column's own comment for why `Run.started_at` is the wrong clock.
    row.delivered_at = datetime.now(UTC)
    if not dry_run:
        _forget_removed_deliveries(session, user_report.slug, user_report.removed_deliveries)
        _record_deliveries(session, user_report.slug, breakdown)


def _cost_blob(user_report) -> dict | None:
    """The per-row cost record for `RunUser.cost`, in integer milliseconds, or None when nothing
    was measured.

    None rather than `{}` on the empty case: an empty blob is indistinguishable from a real
    measurement of zero, and a person who never reached the shared gather (no rows due for them)
    has nothing recorded rather than a zero cost.
    """
    if not user_report.row_timing and not user_report.pool_costs and not user_report.setup_s:
        return None
    return {
        "setup_ms": int(user_report.setup_s * 1000),
        "rows": {
            slug: {"duration_ms": int(cost["duration_s"] * 1000), "blocked_ms": int(cost["blocked_s"] * 1000)}
            for slug, cost in user_report.row_timing.items()
        },
        "pools": [
            {
                "label": pool["label"],
                "tokens": pool["tokens"],
                "exa_searches": pool["exa_searches"],
                "duration_ms": int(pool["duration_s"] * 1000),
                "rows": list(pool["rows"]),
            }
            for pool in user_report.pool_costs
        ],
    }


def _persist_user_report(session: Session, run_id: int, user: User, user_report, dry_run: bool) -> None:
    """One user's RunUser row, their picks (non-dry-run only), and their run.user audit event."""
    user.cold_start = user_report.status == "cold_start"
    session.add(
        RunUser(
            run_id=run_id,
            user_id=user.id,
            status=user_report.status,
            error=user_report.error,
            reason=user_report.reason,
            duration_ms=int(user_report.duration_s * 1000),
            llm_tokens=user_report.llm_tokens,
            llm_tokens_by_step=dict(user_report.llm_tokens_by_step),
            exa_searches=user_report.exa_searches,
            diff=user_report.diff.__dict__ if user_report.diff else {},
            breakdown=user_report.breakdown,
            trace=user_report.trace,
            rows_considered=user_report.rows_considered or {},
            cost=_cost_blob(user_report),
        )
    )
    if not dry_run:
        # Forget BEFORE recording: a row removed and then re-delivered in the same run (a retitle that
        # went through delete+create) must end up with the entry the delivery just wrote, not without one.
        _forget_removed_deliveries(session, user.slug, user_report.removed_deliveries)
        _record_deliveries(session, user.slug, user_report.breakdown)
        for pick in user_report.picks:
            session.add(
                PickRow(
                    run_id=run_id,
                    user_id=user.id,
                    tmdb_id=pick.tmdb_id,
                    media_type=pick.media_type.value,
                    rating_key=pick.rating_key,
                    rank=pick.rank,
                    collection_slug=pick.collection_slug,
                    section_key=pick.section_key,
                    library=pick.library,
                    title=pick.title,
                    reason=pick.reason,
                    sources=",".join(pick.sources),
                    affinity=pick.affinity,
                    seed_tmdb_id=pick.seed_tmdb_id,
                    seed_title=pick.seed_title,
                    rating=pick.rating,
                    year=pick.year,
                    recipe=pick.recipe,
                )
            )
    _add_event(
        session,
        "run.user",
        "error" if user_report.status == "error" else "info",
        run_id,
        dry_run=dry_run,
        user=user_report.slug,
        status=user_report.status,
        diff=user_report.diff.__dict__ if user_report.diff else {},
        privacy_synced=user_report.privacy_synced,
        llm_tokens=user_report.llm_tokens,
        exa_searches=user_report.exa_searches,
        error=user_report.error,
    )


def _emit_sweep_event(session: Session, run_id: int, report) -> None:
    # Rows deleted because Plex could not hide them. This is a SERVER-wide sweep, so it
    # can touch users who were not in this run at all (paused, disabled) — those have no
    # RunUser row to carry the audit, and deleting someone's row is the most destructive
    # thing a run does. It gets its own event (plex-safety rule 10).
    if not report.swept_rows:
        return
    _add_event(
        session,
        "run.sweep",
        "warning",
        run_id,
        dry_run=report.dry_run,
        reason="row was broken beyond repair-in-place — no share filter could hide it (wrong "
        "type for its library, or no shortlist label at all — an orphan from an interrupted "
        "run), or it shared a collection tag with other users' rows and held their picks",
        deleted=report.swept_rows,
    )


def _emit_privacy_sync_events(session: Session, run_id: int, report) -> None:
    # Share-filter writes. Most of these accounts are NOT in this run's user list — they
    # are simply people the server is shared with — so they have no RunUser row to carry
    # the audit. Changing someone's Plex share permissions is the most sensitive thing
    # Shortlist does; "what changed on whose share at 03:31" has to be answerable for every
    # one of them (plex-safety rule 10).
    for account_id, write in report.filter_writes.items():
        _add_event(
            session,
            "run.privacy_sync",
            "info",
            run_id,
            dry_run=report.dry_run,
            plex_account_id=account_id,
            username=write["username"],
            fields={field: {"before": before, "after": after} for field, (before, after) in write["fields"].items()},
        )


def _emit_hub_ordering_events(session: Session, run_id: int, report) -> None:
    # Recommended-shelf reorders. Moving a managed hub shifts every collection's position on a
    # server-wide shelf that a co-managing tool (Kometa) also cares about, so each library we
    # actually moved rows in is audited — "what changed on the shelf at 03:31" (plex-safety rule 10).
    for entry in report.hub_orderings:
        # `verified` is the whole point of the record. "We asked" and "it happened" are different
        # facts — a co-managing tool (agregarr, Kometa) reorders the same shelf on its own clock — and an
        # audit that only ever said the first is how a shelf owned by another tool was reported as a
        # successful reorder for weeks (SFLIX 2026-08-12). A dry run asked for nothing, so it is neither
        # verified nor a warning.
        verified = entry.get("verified")
        _add_event(
            session,
            "run.hub_order",
            "warning" if verified is False else "info",
            run_id,
            dry_run=report.dry_run,
            library=entry.get("library"),
            anchor=entry.get("anchor"),
            moved=entry.get("moved", []),
            verified=verified,
        )


def _emit_request_events(session: Session, run_id: int, report) -> None:
    # Sonarr/Radarr requests. Adding a title to a download app is a real outward-facing
    # write (it consumes disk and bandwidth), so every request — and every skip — is audited
    # with the app's own outcome message, dry-run included (plex-safety rule 10 spirit).
    # A separate, always-checked signal (independent of whether any title was sent): MDBList ran
    # out of quota mid-run, so ratings fell back to TMDB. Drives the owner's quota notification.
    if report.requests is not None and report.requests.warnings:
        for msg in report.requests.warnings:
            _add_event(session, "requests.incomplete_config", "warning", run_id, dry_run=report.dry_run, detail=msg)
    if report.requests is not None and report.requests.ratings_rate_limited:
        _add_event(session, "requests.rate_limited", "warning", run_id, dry_run=report.dry_run)
    # A run that asked for nothing used to emit nothing at all, so "Shortlist has sent Radarr nothing
    # for five days" left no trace in the app — the only record was a single INFO line in the
    # container log. Record the shape of the zero: how many titles cleared the base floors, how many
    # the rating gate got to rate, and what that cost. A gate that stopped short of the pool is the
    # actionable case (raise max_per_run / lower the floor); one that rated everything and still
    # passed nothing means the floors themselves are too high for this library.
    # Fires on `wanted`, not on `pool_size`: a run that wanted 702 titles and passed none of them
    # through the base floors is the MOST actionable shape there is (loosen min_demand or the year
    # window), and keying on the pool skipped exactly that case. `wanted == 0` stays silent — nothing
    # was missing, which is not a problem to report.
    if report.requests is not None and report.requests.wanted and not report.requests.considered:
        _add_event(
            session,
            "requests.none_qualified",
            "warning",
            run_id,
            dry_run=report.dry_run,
            wanted=report.requests.wanted,
            pool_size=report.requests.pool_size,
            examined=report.requests.examined,
            lookups_spent=report.requests.lookups_spent,
            exhausted_pool=report.requests.examined >= report.requests.pool_size,
        )
    if report.requests is None or not report.requests.outcomes:
        return
    _add_event(
        session,
        "run.requests",
        "info",
        run_id,
        dry_run=report.dry_run,
        considered=report.requests.considered,
        outcomes=[
            {
                "tmdb_id": o.tmdb_id,
                "title": o.title,
                "media_type": o.media_type.value,
                "status": o.status,
                "detail": o.detail,
            }
            for o in report.requests.outcomes
        ],
    )


def persist_request_queue(session: Session, run_id: int, report) -> None:
    """Save the titles a run wanted but did not auto-send, for the owner to approve by hand.

    Real runs only — a dry run is a preview and must not mutate the inbox. One row per
    (tmdb_id, media_type): a re-surfaced title refreshes the live facts of a still-pending row;
    a title already sent or rejected is left alone, so a download-in-progress isn't re-queued and
    a dismissed suggestion can't reappear every night.

    A pending title that has since ARRIVED in the library (grabbed elsewhere) is dropped, so the
    inbox never lingers on titles the owner already has. Same for one an ARR now tracks (added
    by hand, by another tool, or before the sent-ledger existed): while it downloads — or
    forever, if unaired — it's absent from Plex, so only the arr-presence prune can catch it.
    """
    if report.requests is None or report.dry_run:
        return
    existing = {(r.tmdb_id, r.media_type): r for r in session.query(RequestCandidate).all()}
    # Drop pending candidates the library now holds; leave sent/rejected alone (owner-actioned).
    present = {(tid, mt.value) for tid, mt in report.library_present}
    present |= report.requests.arr_present  # best-effort; empty when a check was skipped/failed
    for key in [k for k, r in existing.items() if r.status == "pending" and k in present]:
        session.delete(existing.pop(key))
    for m in report.requests.queued:
        row = existing.get((m.tmdb_id, m.media_type.value))
        if row is None:
            session.add(_candidate_row(m, run_id, status="pending"))
        elif row.status == "pending":
            _refresh_pending(row, m)

    # The titles this run AUTO-SENT are filed as `sent` too. Without this the ledger only knew
    # about titles the owner sent by hand, so an auto-sent title still downloading was "missing"
    # again tomorrow: it out-ranked everything by demand, re-consumed one of `max_per_run` every
    # single night, and the queue starved on the same few titles forever.
    # The Arr's answer per auto-sent title, so the sent log records the outcome ("requested",
    # "already in Radarr", …), not just that it went.
    auto_outcomes = {(o.tmdb_id, o.media_type.value): o for o in report.requests.outcomes}
    for m in report.requests.sent:
        row = existing.get((m.tmdb_id, m.media_type.value))
        outcome = auto_outcomes.get((m.tmdb_id, m.media_type.value))
        if row is None:
            new_row = _candidate_row(m, run_id, status="sent")
            new_row.sent_at = datetime.now(UTC)
            if outcome is not None:
                new_row.detail = outcome.detail
            session.add(new_row)
        else:
            # Only on the TRANSITION: a title re-surfaced by a later run is not a second send,
            # and re-stamping would keep sliding it into the current window for ever.
            if row.status != "sent":
                row.sent_at = datetime.now(UTC)
            row.status = "sent"
            if outcome is not None:
                row.detail = outcome.detail
            if m.arr_slug:  # keep an existing slug if this pass somehow didn't resolve one
                row.arr_slug = m.arr_slug


def _finalize_run(
    run: Run, report, status: str | None, error: str | None, ok: int, errors: int, skipped: int = 0
) -> None:
    # `report.ok` — not `errors == 0`. A run-level failure (the sweep could not run, so we
    # refused to write) has no per-user error to count, and must never report success.
    run.status = status or ("ok" if report.ok else "error")
    run.finished_at = datetime.now(UTC)
    # Run-total AI cost, summed from every user (real + shared). by_step merges each user's
    # {llm_web: n} so the run header can show WHERE the tokens went. (Since the curate step was
    # removed, llm_web — web-search title discovery — is the only paid AI path left.)
    tokens_by_step: dict[str, int] = {}
    for user_report in report.users:
        for step, n in user_report.llm_tokens_by_step.items():
            tokens_by_step[step] = tokens_by_step.get(step, 0) + n
    # Titles added to / rotated out of everyone's rows this run (summed across users' diffs), so
    # the runs list can show at a glance how much actually changed on Plex.
    titles_added = sum(len(u.diff.added) for u in report.users if u.diff)
    titles_removed = sum(len(u.diff.removed) for u in report.users if u.diff)
    stats = {
        "users_ok": ok,
        "users_error": errors,
        # Built nothing, but nothing went wrong — see RunUser.reason for which case it was.
        "users_skipped": skipped,
        "dry_run": report.dry_run,
        "rows_swept": sum(len(titles) for titles in report.swept_rows.values()),
        "shares_updated": len(report.filter_writes),
        "titles_added": titles_added,
        "titles_removed": titles_removed,
        "titles_requested": report.requests.requested if report.requests else 0,
        "requests_warnings": report.requests.warnings if report.requests else [],
        # What "0 requested" was arrived at from — see RequestReport. Kept beside the count because a
        # zero on its own is unreadable, and the run page is where it gets read.
        # How many are WAITING for the owner. Without it "0 requested" reads as a failure even when
        # the run worked perfectly and simply put five titles in the inbox for approval.
        "requests_queued": len(report.requests.queued) if report.requests else 0,
        "requests_wanted": report.requests.wanted if report.requests else 0,
        # Per row, because the aggregates cannot answer the question the feature exists to make
        # answerable: WHICH row was starved. Written only when there is something to say — a run with
        # requests off, or one that never reached the request phase, records no empty dicts.
        **(
            {
                "requests_by_row": {
                    slug: {
                        "pool": report.requests.pool_by_row.get(slug, 0),
                        "examined": report.requests.examined_by_row.get(slug, 0),
                        "considered": report.requests.considered_by_row.get(slug, 0),
                        "claimed": report.requests.claimed_by_row.get(slug, 0),
                        "sent": report.requests.sent_by_row.get(slug, 0),
                    }
                    for slug in report.requests.pool_by_row
                }
            }
            if report.requests and report.requests.pool_by_row
            else {}
        ),
        "requests_pool": report.requests.pool_size if report.requests else 0,
        "requests_examined": report.requests.examined if report.requests else 0,
        "requests_lookups": report.requests.lookups_spent if report.requests else 0,
        "llm_tokens": sum(u.llm_tokens for u in report.users),
        "llm_tokens_by_step": tokens_by_step,
        "exa_searches": sum(u.exa_searches for u in report.users),
        # Cache hits served from the shared 14-day web-search cache. Reported so the UI can read
        # "1 searched · N from cache" — without it a fully-cached run shows a bare exa_searches:1
        # and looks like the source did nothing (it didn't: the cache did the work).
        "exa_cache_hits": sum(u.exa_cache_hits for u in report.users),
        "error": error or report.error,
        # Every account whose share filter Plex refused this run. These are the reason nothing
        # was promoted, so the UI can say so instead of leaving "Failed" unexplained (issue #1).
        "promotion_blockers": list(report.promotion_blockers),
    }
    # Accounts Plex refuses a hide-list for that can nonetheless SEE other people's rows. Not a
    # blocker — nothing we do hides them — so the run succeeds and this is how the owner finds out.
    #
    # Present ONLY when the run reached the privacy phase and actually looked. Both readers pick the
    # latest run carrying this key and treat it as the truth, so writing it unconditionally meant a
    # run that died in the sweep phase published an empty finding and silently cleared a live alert
    # and every badge. Absent now means "this run did not measure", which is what they already
    # assumed it meant.
    if report.unhideable_measured:
        stats["unhideable_rows"] = {name: list(keys) for name, keys in report.unhideable_rows.items()}
    # Accounts the owner left alone whose excludes could not be taken back off. Written only when
    # non-empty: an empty key would read as a measurement on every run that never got this far.
    # Accounts whose filter Shortlist wrote and Plex is not applying. Written on every run that
    # actually MEASURED, empty included — that empty dict is what lets a fixed server clear the
    # alert. Keyed on the measured flag rather than on emptiness, because the notification reads the
    # newest run carrying the key: writing only when non-empty pinned an undismissable error card
    # through every clean run that followed one bad night.
    if report.filters_enforcement_measured:
        stats["filters_not_enforced"] = {name: list(keys) for name, keys in report.filters_not_enforced.items()}
    if report.left_alone_failures:
        stats["left_alone_failures"] = {str(account): why for account, why in report.left_alone_failures.items()}
    # Assigned whole rather than mutated in place: `stats` is a JSON column, and an in-place edit
    # after assignment would not reliably mark it dirty.
    run.stats = stats
