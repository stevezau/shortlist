"""The effectiveness report's computation: is Shortlist actually getting watched?

Lifted out of ``api/report.py``, where it was a ~370-line ``async def`` handler wrapping eleven nested
closures and roughly thirty queries — including a ``GROUP BY user_id, tmdb_id, media_type`` over the
whole picks table — all of it synchronous SQLAlchemy running ON the event loop. The router now calls
this from a plain ``def`` handler, so Starlette runs the whole thing in a worker thread and a
dashboard load stops stalling SSE, ``/api/system/health`` and every other request for its duration.

Everything is windowed (7/30/90/all). See ``api/report.py``'s module docstring for why: the report
used to be lifetime-cumulative, which made every ratio measure how long Shortlist had been installed
rather than how good the picks were.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, case, cast, func, literal, or_
from sqlalchemy.orm import Session

from shortlist.engine.models import DEFAULT_ROW_TEMPLATE
from shortlist.server.db.models import (
    DEFAULT_SLUG,
    Collection,
    PickRow,
    RequestCandidate,
    Run,
    RunUser,
    SharedRowWatch,
    User,
    iso_utc,
)
from shortlist.server.services.run_service import HIT_WINDOW_DAYS
from shortlist.server.settings_store import SettingsStore

_PLACEHOLDER = re.compile(r"\{[^}]+\}")

#: Selectable report windows, in days. ``None`` = all time.
WINDOWS: dict[str, int | None] = {"7": 7, "30": 30, "90": 90, "all": None}
DEFAULT_WINDOW = "30"

#: How many weekly buckets the trend chart carries. The trend is the LONG view — it deliberately
#: ignores the window selector, because a 7-day window would leave it with one bar.
TREND_WEEKS = 16

# A title within one person's set: (tmdb_id, media_type). `.concat()` (SQL `||`), never func.concat —
# the latter needs SQLite >= 3.44 and the runtime image ships 3.40.
_TITLE = cast(PickRow.tmdb_id, String).concat("-").concat(PickRow.media_type)
# A title across everyone: prefix the person, so one film recommended to two people counts twice.
_PERSON_TITLE = cast(PickRow.user_id, String).concat("-").concat(_TITLE)

# The same identity for a SHARED-row watch. Shared rows write no `picks`, so their credits live in
# `shared_row_watches` — and the headline counts must include them, because `overall.bounced` and
# `overall.dropped` already do (they come off `resolve_outcomes`). Leaving them out of `watched` let
# the Dropped tile exceed the Watched tile beside it: an abandonment with no start.
_SHARED_TITLE = cast(SharedRowWatch.tmdb_id, String).concat("-").concat(SharedRowWatch.media_type)
_SHARED_PERSON_TITLE = cast(SharedRowWatch.user_id, String).concat("-").concat(_SHARED_TITLE)


def _rate(watched: int, delivered: int) -> float | None:
    return round(watched / delivered, 3) if delivered else None


def _delta(current: int | float | None, previous: int | float | None) -> float | None:
    """Change vs the previous equal-length period, or None when there is nothing to compare against
    (the ``all`` window, or a stat that was null in either period)."""
    if current is None or previous is None:
        return None
    return round(current - previous, 1)


def _as_utc(value: datetime) -> datetime:
    """SQLite hands back naive datetimes for some columns and aware ones for others; comparing the
    two raises. Treat naive as UTC, which is what every writer in this app stores."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _in_period(column, start, end=None) -> list:
    """Filters restricting `column` to [start, end). Empty when start is None (all time)."""
    clauses = []
    if start is not None:
        clauses.append(column >= start)
    if end is not None:
        clauses.append(column < end)
    return clauses


def _watched_in(start, end=None) -> list:
    """Filters for picks WATCHED in [start, end) — the numerator side of every count here."""
    return [PickRow.watched_at.isnot(None), *_in_period(PickRow.watched_at, start, end)]


def _finished_in(start, end=None) -> list:
    """Filters for picks credited in [start, end) that have since been FINISHED.

    The window is asked of ``watched_at``, NOT ``finished_at``, and that is the whole point: the UI
    prints the pair as "N watched · M finished" and draws M inside the bar of N, which is only ever
    true if M is a genuine subset of N for the same window.

    Windowing on ``finished_at`` breaks that. A series credited in June and finished in August gives
    `0 watched · 1 finished` in an August window — nonsense text, and a segment wider than the bar
    containing it. Pinned by `test_report_finished_window.py`.

    So the question is "of the picks credited in this window, how many are finished", and the cost is
    accepted deliberately: a series finished this week but credited months ago lands in the window it
    was credited in, not this one. That is the reading the whole dashboard already uses — `watched`
    itself is bucketed by when the pick was credited, not by anything else.
    """
    return [PickRow.finished_at.isnot(None), *_watched_in(start, end)]


def _rank_titles(counts: dict[tuple[int, str], int], limit: int) -> list[tuple[tuple[int, str], int]]:
    """Most watchers first, ties broken by tmdb id then media type — a TOTAL order.

    Its own function so the ordering can be tested directly. Driven from the database it cannot be:
    SQLite emits `GROUP BY` output in ascending key order and Python's sort is stable, so a
    DB-populated test is already in tie-break order before the tie-break runs, and passes with it
    deleted. The only test with teeth hands this a dict whose insertion order is wrong.

    Without the tie-break, equal-scoring titles reshuffle between two renders of identical data —
    the list visibly jumping around on refresh.
    """
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))[:limit]


def _grouped_union(session: Session, cols, key_expr, filters, s_cols, s_key, s_filters) -> dict:
    """{group key -> count of DISTINCT identities} across `picks` and `shared_row_watches` together.

    The grouped twin of :func:`_distinct_across_both`. The union is over `(group, identity)` PAIRS, so
    an identity appearing on both sides under the same group is counted ONCE — which is what makes it
    safe for `per_user`, where one person can watch a title carried by a personal row and a shared one
    at the same time. Summing two grouped counts reports that person as having watched two titles.
    """
    both = (
        session.query(*cols, key_expr.label("k"))
        .filter(*filters)
        .union(session.query(*s_cols, s_key.label("k")).filter(*s_filters))
        .subquery()
    )
    group = [both.c[name] for name in list(both.c.keys())[:-1]]
    rows = session.query(*group, func.count()).group_by(*group).all()
    return {(r[:-1] if len(group) > 1 else r[0]): r[-1] for r in rows}


def _counts(session: Session, group_cols, key_expr, start, shared_cols=None, shared_key=None) -> dict:
    """{group key -> (delivered, watched, finished)} distinct-title counts for one period.

    `delivered` counts picks CREATED in the period; `watched` counts picks WATCHED in it —
    deliberately not the same set. A pick delivered last month and watched this week is a watch this
    week, and pinning it to its delivery period would hide it entirely. These are counts, never a
    ratio, so the mismatch is honest rather than misleading.

    `finished` is a subset of `watched`: the picks they saw out rather than sampled. The gap between
    the two is the whole point of the split — a TV row scores a `watched` on a single episode, so
    ranking rows on that number alone flatters television for a structural reason.

    `shared_cols`/`shared_key` fold SHARED-row credits into `watched`/`finished` — needed because the
    headline tiles count them, and a "People watching: 4" above a By-person section where every bar
    reads 0 is the two being read as one population when they are not.

    Deliberately NOT into `delivered`: a shared row is one collection for the whole server, so there
    is no per-person delivery to count, and inventing one would be a number with no referent. The UI
    renders `delivered` only when it is above zero and never as a fraction of `watched` (two counts
    over different sets, by design), so a shared-only line reads "1 watched" with no delivery clause —
    which is the truth.
    """
    cols = list(group_cols) if isinstance(group_cols, (list, tuple)) else [group_cols]
    s_cols = (
        (list(shared_cols) if isinstance(shared_cols, (list, tuple)) else [shared_cols])
        if shared_cols is not None
        else None
    )

    def scan(*extra):
        rows = session.query(*cols, func.count(func.distinct(key_expr))).filter(*extra).group_by(*cols).all()
        return {(r[:-1] if len(cols) > 1 else r[0]): r[-1] for r in rows}

    delivered = scan(*_in_period(PickRow.created_at, start))
    if s_cols is None:
        watched = scan(*_watched_in(start))
        finished = scan(*_finished_in(start))
    else:
        # A UNION per group, never a sum. Summing double-counts `per_user`: one person who watched a
        # title carried by BOTH a personal and a shared row is one title watched, and each scan
        # returns it. Pinned by
        # `test_shared_row_watches.py::test_one_title_on_both_kinds_of_row_is_not_double_counted_for_a_person`,
        # which caught exactly that when this was a sum.
        watched = _grouped_union(
            session, cols, key_expr, _watched_in(start), s_cols, shared_key, _shared_watched_in(start)
        )
        finished = _grouped_union(
            session, cols, key_expr, _finished_in(start), s_cols, shared_key, _shared_finished_in(start)
        )
    return {
        k: (delivered.get(k, 0), watched.get(k, 0), finished.get(k, 0))
        for k in set(delivered) | set(watched) | set(finished)
    }


def _shared_watched_in(start, end=None) -> list:
    """`_watched_in` for a shared-row credit — same column meaning, same window."""
    return [SharedRowWatch.watched_at.isnot(None), *_in_period(SharedRowWatch.watched_at, start, end)]


def _shared_finished_in(start, end=None) -> list:
    """Windowed on `watched_at`, exactly like `_finished_in` and for the same reason: finished must
    be a genuine subset of watched for the same window, or the UI draws a segment wider than its bar."""
    return [SharedRowWatch.finished_at.isnot(None), *_shared_watched_in(start, end)]


def _distinct_across_both(session: Session, picks_expr, picks_filters, shared_expr, shared_filters) -> int:
    """Count distinct values of one identity across `picks` and `shared_row_watches` together.

    A UNION, not a sum: someone who watched a title that was on BOTH a personal and a shared row is
    one person-title, and adding two counts would report them twice. SQL `UNION` dedupes, which is the
    same "one outcome per person-title" rule `resolve_outcomes` applies on the other side.
    """
    combined = (
        session.query(picks_expr.label("k"))
        .filter(*picks_filters)
        .union(session.query(shared_expr.label("k")).filter(*shared_filters))
    ).subquery()
    return session.query(func.count()).select_from(combined).scalar() or 0


def _watched_count(session: Session, start, end=None) -> int:
    return _distinct_across_both(
        session, _PERSON_TITLE, _watched_in(start, end), _SHARED_PERSON_TITLE, _shared_watched_in(start, end)
    )


def _finished_count(session: Session, start, end=None) -> int:
    return _distinct_across_both(
        session, _PERSON_TITLE, _finished_in(start, end), _SHARED_PERSON_TITLE, _shared_finished_in(start, end)
    )


def _watchers_count(session: Session, start, end=None) -> int:
    return _distinct_across_both(
        session, PickRow.user_id, _watched_in(start, end), SharedRowWatch.user_id, _shared_watched_in(start, end)
    )


def _avg_days_to_watch(session: Session, start, end=None) -> float | None:
    """Average days from FIRST delivery to FIRST watch, over titles first watched in the period.

    Per (user, title), not per delivery row — a title re-recommended nightly is one data point,
    measured from when it was first added (MIN created_at) to when it was first watched.
    """
    firsts = (
        session.query(
            func.min(PickRow.created_at).label("added"),
            func.min(PickRow.watched_at).label("watched"),
        )
        .group_by(PickRow.user_id, PickRow.tmdb_id, PickRow.media_type)
        .subquery()
    )
    clauses = [firsts.c.watched.isnot(None), *_in_period(firsts.c.watched, start, end)]
    value = (
        session.query(func.avg(func.julianday(firsts.c.watched) - func.julianday(firsts.c.added)))
        .filter(*clauses)
        .scalar()
    )
    return round(value, 1) if value is not None else None


def _landing(session: Session, now: datetime, days: int | None) -> dict:
    """The landing rate, over a MATURED cohort.

    The equally-long window ending ``HIT_WINDOW_DAYS`` ago — i.e. the most recent stretch of picks
    that have all had their chance. A pick's chance actually ends when its row drops it, which is
    usually sooner; this stays the conservative outer bound, and a younger pick is excluded because
    including it would recreate the very bug this rewrite exists to fix, inside a smaller box. Note
    this is a SHIFTED window, not an intersection with the selected one: for `window=30` the cohort
    is [now-60d, now-30d). The UI prints `cohort_from`/`cohort_to` so the dates are never left to be
    inferred.
    """
    matured_until = now - timedelta(days=HIT_WINDOW_DAYS)
    matured_since = matured_until - timedelta(days=days) if days is not None else None
    cohort = _in_period(PickRow.created_at, matured_since, matured_until)
    delivered = session.query(func.count(func.distinct(_PERSON_TITLE))).filter(*cohort).scalar() or 0
    watched = (
        session.query(func.count(func.distinct(_PERSON_TITLE))).filter(PickRow.watched_at.isnot(None), *cohort).scalar()
        or 0
    )
    # `watched_at IS NOT NULL` alongside it, for the reason `_finished_in` exists: the UI draws
    # finished INSIDE watched, so the subset is enforced where it is counted rather than assumed
    # from the writer. Nothing clears `watched_at` today; this is what keeps that from mattering.
    finished = (
        session.query(func.count(func.distinct(_PERSON_TITLE)))
        .filter(PickRow.finished_at.isnot(None), PickRow.watched_at.isnot(None), *cohort)
        .scalar()
        or 0
    )
    return {
        "delivered": delivered,
        "watched": watched,
        # Same matured cohort, stricter numerator. Both rates are over the same picks, so they are
        # directly comparable with each other.
        "finished": finished,
        "finished_rate": _rate(finished, delivered),
        "rate": _rate(watched, delivered),
        "cohort_from": iso_utc(matured_since) if matured_since else None,
        "cohort_to": iso_utc(matured_until),
        "matured_days": HIT_WINDOW_DAYS,
    }


class _RowNamer:
    """Renders a row slug + library into the name the dashboard shows for that line.

    Picks outlive the row that made them (deleting a row keeps its watch history), and a slug with no
    Collection behind it must NOT borrow the default row's template — every deleted row would then
    render "✨ Movies Picked for You" and the breakdown would show the default row two, three, five
    times over with different numbers. Those are labelled by slug instead, and flagged ``deleted``.
    """

    def __init__(self, session: Session, default_template: str) -> None:
        self._default = default_template
        # The row's own template, the default row falling back to the global one (the per-user
        # override tier of engine `resolve_row_template` is dropped for this aggregate label, and a
        # custom row uses its stored name). Rendered per library below.
        # The default row's template is the GLOBAL one, full stop — never its own column. The engine
        # forces that column empty when it builds specs (`context_builder.py:604,698`), so reading it
        # here made reports the one surface that could disagree with what Plex actually got: a
        # database carrying a stale value (written before the API guarded it) shows the old name for
        # ever, while delivery uses the global. Ignoring it makes this match delivery on both old and
        # new databases, so no migration is needed to clean the column up.
        self._templates = {
            c.slug: (default_template if c.slug == DEFAULT_SLUG else (c.name_template or c.name))
            for c in session.query(Collection).all()
        }

    def template(self, slug: str) -> str | None:
        """This row's name template, or None once the row itself is gone."""
        if slug in self._templates:
            return self._templates[slug]
        # Pre-0004 picks predate multi-row and carry a blank slug; they ARE the default row.
        return self._default if slug in ("", DEFAULT_SLUG) else None

    def label(self, slug: str, library: str) -> str:
        """The row's display name for THIS library: `{library_name}` becomes the library ("Movies"),
        and any other placeholder (e.g. `{top_seed}`, which is per-person) is dropped for the
        aggregate. A deleted row has no template left, so its slug is the only identity it still has.
        """
        template = self.template(slug)
        if template is None:
            return slug
        name = _PLACEHOLDER.sub(lambda m: library if m.group(0) == "{library_name}" else "", template)
        return " ".join(name.split()) or "Picked for You"


def _breakdown(raw: dict, label) -> list[dict]:
    """Counts, sorted by what was actually watched.

    Sorting by rate is what put `1/31` above `3/103` — a single data point outranking three times the
    evidence. Watched count first, delivered as the tiebreak, so the list reads as "who is getting
    the most out of this".

    Still sorted on `watched`, not `finished`. Ranking by finished would bury every TV row under
    every movie row — a series is finished far less often than a film, for reasons that have nothing
    to do with how good the pick was. The split is shown per line so the difference is visible;
    it is deliberately not the sort key.
    """
    return sorted(
        (
            {**label(key), "delivered": d, "watched": w, "finished": f}
            for key, (d, w, f) in raw.items()
            if label(key) is not None
        ),
        key=lambda r: (r["watched"], r["delivered"]),
        reverse=True,
    )


def _requests_summary(session: Session, since: datetime | None) -> dict:
    """Requests that paid off: titles asked of Sonarr/Radarr that were LATER watched by someone.

    "Later" is the point. This used to be a plain set intersection with no ordering check, so a title
    watched, then deleted from the library, then re-requested counted as a request that paid off. It
    now compares against `sent_at`, stamped once when the status flips to "sent".

    `updated_at` is the fallback for rows sent before that column existed. It is a poor proxy — it has
    `onupdate`, so clearing an old title from the Sent log bumps it — but it is what those rows have,
    and it is no worse than the behaviour they already had.
    """

    def sent_time(row) -> datetime | None:
        return _as_utc(row.sent_at or row.updated_at) if (row.sent_at or row.updated_at) else None

    sent_rows = [
        row
        for row in session.query(RequestCandidate).filter(RequestCandidate.status == "sent").all()
        if (when := sent_time(row)) is not None and (since is None or when >= since)
    ]
    sent = {(row.tmdb_id, row.media_type): sent_time(row) for row in sent_rows}
    watched_at_by_title: dict[tuple[int, str], datetime] = {}
    for tid, mt, watched in (
        session.query(PickRow.tmdb_id, PickRow.media_type, func.max(PickRow.watched_at))
        .filter(PickRow.watched_at.isnot(None))
        .group_by(PickRow.tmdb_id, PickRow.media_type)
        .all()
    ):
        watched_at_by_title[(tid, mt)] = watched
    paid_off = sum(
        1
        for key, sent_at in sent.items()
        if (watched := watched_at_by_title.get(key)) is not None and (sent_at is None or _as_utc(watched) >= sent_at)
    )
    return {
        "sent": len(sent),
        "pending": session.query(func.count(RequestCandidate.id)).filter_by(status="pending").scalar() or 0,
        "watched_after_sent": paid_off,
    }


#: Below this, a start is "opened and closed" rather than "gave it a go". Two minutes of a film is a
#: different signal from forty, and collapsing them loses the one that says "wrong pick entirely".
BOUNCE_PERCENT = 5


def resolve_outcomes(session: Session, since: datetime | None) -> dict[tuple[int, int, str], dict]:
    """One outcome per (person, title), resolved over ALL their rows — the single source for the split.

    A title has one pick row per run that delivered it, and those rows disagree by design: the stamps
    are bounded to rows delivered at or before the play, so a run firing afterwards leaves a row with a
    percentage and no credit. Independent "any row" scans over those rows produced two lies at once —
    one person-title counted as bounced AND dropped when two rows held different percentages, and a
    finished title reported as dropped when the credited row fell outside the window.

    So the outcome is decided once, in one place, from the whole set:

    * `finished` if ANY row has `finished_at` — regardless of window; finishing is not undone by time
    * otherwise `bounced`/`dropped` by the FURTHEST progress any row observed
    * otherwise `watching` — credited, but no live session ever said how far

    Windowed on the OBSERVATION: `watched_at` if there is one, else the EARLIEST delivery. Using the
    latest delivery meant an abandonment never aged out while the row kept re-delivering the title —
    a 60-day-old drop still counted in the 7-day window, beside a `watched` figure that had correctly
    aged it out.
    """
    rows = session.query(
        PickRow.user_id,
        PickRow.tmdb_id,
        PickRow.media_type,
        PickRow.watched_at,
        PickRow.finished_at,
        PickRow.max_percent,
        PickRow.created_at,
        PickRow.title,
        PickRow.collection_slug,
        PickRow.library,
    ).filter(or_(PickRow.watched_at.isnot(None), PickRow.max_percent.isnot(None)))

    out: dict[tuple[int, int, str], dict] = {}
    for user_id, tmdb_id, media_type, watched, finished, percent, created, title, slug, library in rows:
        key = (user_id, tmdb_id, media_type)
        entry = out.setdefault(
            key,
            {
                "title": title,
                "media_type": media_type,
                "row": slug,
                "library": library,
                "watched_at": None,
                "finished_at": None,
                "percent": None,
                "first_delivered": None,
            },
        )
        if watched and (entry["watched_at"] is None or watched < entry["watched_at"]):
            entry["watched_at"] = watched
            entry["row"], entry["library"] = slug, library
        if finished and (entry["finished_at"] is None or finished > entry["finished_at"]):
            entry["finished_at"] = finished
        if percent is not None and (entry["percent"] is None or percent > entry["percent"]):
            entry["percent"] = percent
        if entry["first_delivered"] is None or created < entry["first_delivered"]:
            entry["first_delivered"] = created

    # SHARED rows, folded into the same person-title outcome. They write no pick rows, so their
    # watches live in `shared_row_watches` (migration 0078) and were invisible to every figure here.
    # Folded in rather than counted separately BECAUSE this function's whole job is one outcome per
    # person-title: a title on both a personal row and a shared one is one thing they watched, and
    # two independent tallies would report it twice and could disagree about how it ended.
    for user_id, slug, tmdb_id, media_type, title, watched, finished, percent in session.query(
        SharedRowWatch.user_id,
        SharedRowWatch.collection_slug,
        SharedRowWatch.tmdb_id,
        SharedRowWatch.media_type,
        SharedRowWatch.title,
        SharedRowWatch.watched_at,
        SharedRowWatch.finished_at,
        SharedRowWatch.max_percent,
    ):
        key = (user_id, tmdb_id, media_type)
        entry = out.setdefault(
            key,
            {
                "title": title,
                "media_type": media_type,
                "row": slug,
                "library": "",
                "watched_at": None,
                "finished_at": None,
                "percent": None,
                # A shared row has no per-person delivery, so the credit itself is the only moment
                # this person can be said to have met the title. Left as the watch below.
                "first_delivered": None,
            },
        )
        if watched and (entry["watched_at"] is None or watched < entry["watched_at"]):
            entry["watched_at"] = watched
            # The library is cleared with the row, not kept. A shared row is not in the personal row's
            # library, and `engagement` renders `namer.label(row, library)` — so keeping the old value
            # printed a shared TV row under a Movies label.
            entry["row"], entry["library"] = slug, ""
        if finished and (entry["finished_at"] is None or finished > entry["finished_at"]):
            entry["finished_at"] = finished
        if percent is not None and (entry["percent"] is None or percent > entry["percent"]):
            entry["percent"] = percent
        if entry["first_delivered"] is None and watched:
            entry["first_delivered"] = watched

    resolved: dict[tuple[int, int, str], dict] = {}
    for key, entry in out.items():
        observed = entry["watched_at"] or entry["first_delivered"]
        if since is not None and observed is not None and _as_utc(observed) < since:
            continue
        if entry["finished_at"] is not None:
            entry["outcome"] = "finished"
        elif entry["percent"] is None:
            entry["outcome"] = "watching"
        else:
            entry["outcome"] = "bounced" if entry["percent"] < BOUNCE_PERCENT else "dropped"
        entry["observed_at"] = observed
        resolved[key] = entry
    return resolved


def _engagement_split(session: Session, since: datetime | None) -> tuple[int, int]:
    """(bounced, dropped) — started and abandoned, split by how far they got.

    Both come off :func:`resolve_outcomes`, so they are mutually exclusive by construction and cannot
    disagree with the detail page that reads the same function.
    """
    outcomes = [e["outcome"] for e in resolve_outcomes(session, since).values()]
    return outcomes.count("bounced"), outcomes.count("dropped")


def _recent_watches(session: Session, users: dict[int, User], namer: _RowNamer, since: datetime | None) -> list[dict]:
    """The recent-watches feed: one line per (person, title), like every other figure here.

    NOT one per pick row. A title re-recommended over several runs has one pick row per run, and the
    watch-sync stamps the same watched_at on every one of them, so the raw feed repeated a single
    watch once per delivery ("Jarrah watched Beckham" five times). Credit the newest delivery (its
    row/library label is the current one) at the latest time that person watched it.
    """
    # `finished` is aggregated over the SAME group as `watched`, not read off the credited pick row.
    # The two are stamped by different passes — `watched_at` when Plex first credits the title,
    # `finished_at` once it passes our own completion threshold, possibly on a later night and
    # possibly onto a different delivery of the same title — so taking it from `pick_id` alone would
    # report "started" for a series the person demonstrably finished.
    latest = (
        session.query(
            func.max(PickRow.id).label("pick_id"),
            func.max(PickRow.watched_at).label("watched"),
            func.max(PickRow.finished_at).label("finished"),
        )
        .filter(*_watched_in(since))
        .group_by(PickRow.user_id, PickRow.tmdb_id, PickRow.media_type)
        .order_by(func.max(PickRow.watched_at).desc())
        .limit(20)
        .subquery()
    )

    def line(user_id: int, tmdb_id: int, media_type: str, at, **fields) -> dict:
        user = users.get(user_id)
        return {
            # The identity travels WITH the line and is stripped at the end. Deduping on the rendered
            # username collapsed two different removed accounts (both "unknown") into one line, and
            # deduping on the title text split one person-title in two whenever the shared row's copy
            # of the title differed from the pick row's.
            "_key": (user_id, tmdb_id, media_type),
            #: Sort key. Stripped with every other `_`-prefixed field before the response is built.
            "_at": at,
            "username": user.username if user else "unknown",
            "display_name": user.display_name if user else "unknown",
            "media_type": media_type,
            **fields,
        }

    feed = [
        line(
            p.user_id,
            p.tmdb_id,
            p.media_type,
            watched,
            title=p.title,
            row=namer.label(p.collection_slug, p.library),
            library=p.library,
            seed_title=p.seed_title or "",
            watched_at=iso_utc(watched),
            finished_at=iso_utc(finished) if finished else None,
        )
        for p, watched, finished in session.query(PickRow, latest.c.watched, latest.c.finished)
        .join(latest, PickRow.id == latest.c.pick_id)
        .order_by(latest.c.watched.desc())
        .all()
    ]

    # SHARED rows belong in this feed too — "did someone watch something we recommended" is the
    # question it answers, and a shared row recommends. They write no pick rows, so they cannot come
    # out of the query above; they are merged in and the whole feed re-sorted and re-trimmed, so the
    # 20 shown are the 20 most recent overall rather than the 20 most recent PERSONAL ones plus extras.
    seen = {row["_key"] for row in feed}
    for w in (
        session.query(SharedRowWatch)
        .filter(*_shared_watched_in(since))
        .order_by(SharedRowWatch.watched_at.desc())
        .limit(20)
    ):
        key = (w.user_id, w.tmdb_id, w.media_type)
        # A title on both a personal and a shared row is ONE thing that person watched. The personal
        # line wins because it carries the library and the seed that produced it.
        if key in seen:
            continue
        seen.add(key)
        feed.append(
            line(
                w.user_id,
                w.tmdb_id,
                w.media_type,
                w.watched_at,
                title=w.title,
                row=namer.label(w.collection_slug, ""),
                library="",
                seed_title="",
                watched_at=iso_utc(w.watched_at),
                finished_at=iso_utc(w.finished_at) if w.finished_at else None,
            )
        )
    # Sorted on the DATETIME, not the rendered string. `datetime.isoformat()` omits `.ffffff` when
    # microseconds are zero, so the strings are variable width; string sort happens to be correct here
    # (the first differing character is `+` against `.`, and `'+' < '.'`) but only by accident, and
    # nothing would tell the next editor which property they had to preserve.
    feed.sort(key=lambda row: row["_at"], reverse=True)
    return [{k: v for k, v in row.items() if not k.startswith("_")} for row in feed[:20]]


def effectiveness(session: Session, window: str, *, next_watch_sync: str | None = None) -> dict:
    """The dashboard tracking report for one window.

    Args:
        session: An open session. Every read happens inside it — computing any of this after the
            session closed silently re-opened a transaction nothing then closed, leaking one
            connection per dashboard load against a pool of 5 + 10.
        window: One of :data:`WINDOWS`; anything else falls back to :data:`DEFAULT_WINDOW`.
        next_watch_sync: When the daily watch-sync next fires, read from the scheduler by the caller
            (this layer has no app state).

    Returns:
        Headline counts for the window with their change vs the previous equal period, the landing
        rate over a matured cohort, watch momentum, per-user and per-row breakdowns, the titles
        landing best, requests that paid off, and a recent-watches feed.
    """
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    days = WINDOWS[window]
    now = datetime.now(UTC)

    # The current period is [since, now); the previous is [prev_since, since). Both are None for
    # "all", which has no previous period and therefore no deltas.
    since = now - timedelta(days=days) if days is not None else None
    prev_since = now - timedelta(days=2 * days) if days is not None else None

    first_pick = iso_utc(session.query(func.min(PickRow.created_at)).scalar())
    per_user_raw = _counts(session, PickRow.user_id, _TITLE, since, SharedRowWatch.user_id, _SHARED_TITLE)
    # A row that targets >1 library is one Plex collection PER library, so it's tracked per
    # (row, library) — each library gets its own delivered/watched line, keyed (slug, section, library).
    per_row_raw = _counts(
        session,
        [PickRow.collection_slug, PickRow.section_key, PickRow.library],
        _PERSON_TITLE,
        since,
        # A shared row has no per-library split: it is ONE collection, so its line is keyed with empty
        # section and library, which `_RowNamer.label` renders from the row's own name.
        [SharedRowWatch.collection_slug, literal("").label("section_key"), literal("").label("library")],
        _SHARED_PERSON_TITLE,
    )

    watched_now = _watched_count(session, since)
    finished_now = _finished_count(session, since)
    watchers_now = _watchers_count(session, since)
    avg_now = _avg_days_to_watch(session, since)
    watched_prev = _watched_count(session, prev_since, since) if since else None
    watchers_prev = _watchers_count(session, prev_since, since) if since else None
    avg_prev = _avg_days_to_watch(session, prev_since, since) if since else None

    delivered_now = (
        session.query(func.count(func.distinct(_PERSON_TITLE))).filter(*_in_period(PickRow.created_at, since)).scalar()
        or 0
    )
    landing = _landing(session, now, days)

    # The trend ignores the window on purpose — see TREND_WEEKS. SHARED rows are folded in like
    # everywhere else that counts a WATCH: the chart sits directly under the Watched tile, and a bar
    # that omitted what the tile counted would read as the same number failing to add up.
    _week = func.strftime("%Y-%W", PickRow.watched_at)
    _shared_week = func.strftime("%Y-%W", SharedRowWatch.watched_at)
    trend_by_week = _grouped_union(
        session,
        [_week.label("week")],
        _PERSON_TITLE,
        [PickRow.watched_at.isnot(None)],
        [_shared_week.label("week")],
        _SHARED_PERSON_TITLE,
        [SharedRowWatch.watched_at.isnot(None)],
    )
    trend_rows = sorted(trend_by_week.items())[-TREND_WEEKS:]
    # Of the picks CREDITED in each week, how many have since been finished — bucketed by
    # `watched_at`, the same key as the bar above, for the reason `_finished_in` explains: it is what
    # makes the segment a true subset of the column it is drawn inside.
    #
    # Consequence worth knowing: a past week's dark segment GROWS when someone finishes an old
    # series. That is the truth about a cohort, not a bug — the bar is "what became of what landed
    # that week", and that answer genuinely changes.
    finished_by_week = _grouped_union(
        session,
        [_week.label("week")],
        _PERSON_TITLE,
        [PickRow.watched_at.isnot(None), PickRow.finished_at.isnot(None)],
        [_shared_week.label("week")],
        _SHARED_PERSON_TITLE,
        [SharedRowWatch.watched_at.isnot(None), SharedRowWatch.finished_at.isnot(None)],
    )

    store = SettingsStore(session)
    last_watch_sync = store.get("report.watch_synced_at")  # when the daily job last ran
    users = {u.id: u for u in session.query(User).all()}
    namer = _RowNamer(session, store.get("row.name_template") or DEFAULT_ROW_TEMPLATE)

    # Reach: who's actually covered. `users_enabled`/`rows_enabled` describe the server as it is
    # NOW, so they are deliberately not windowed — "3 of 11 people" only reads if 11 is current.
    users_enabled = sum(1 for u in users.values() if u.enabled)
    users_with_picks = (
        session.query(func.count(func.distinct(PickRow.user_id)))
        .filter(*_in_period(PickRow.created_at, since))
        .scalar()
        or 0
    )
    rows_enabled = session.query(func.count(Collection.id)).filter(Collection.enabled.is_(True)).scalar() or 0

    # Runs. `total` stays all-time (it is the odometer); `in_window` is what the delta is about.
    runs_total = session.query(func.count(Run.id)).scalar() or 0
    runs_in_window = session.query(func.count(Run.id)).filter(*_in_period(Run.started_at, since)).scalar() or 0
    runs_prev = (
        session.query(func.count(Run.id)).filter(*_in_period(Run.started_at, prev_since, since)).scalar() or 0
        if since
        else None
    )
    last_run = session.query(Run).filter(Run.status.in_(("ok", "error"))).order_by(Run.id.desc()).first()
    errors_last = (
        session.query(func.count(RunUser.user_id))
        .filter(RunUser.run_id == last_run.id, RunUser.status == "error")
        .scalar()
        if last_run
        else 0
    )

    requests = _requests_summary(session, since)

    # The titles landing best: most distinct watchers among titles watched in the window. Shared rows
    # count — a title everyone found through the shared row is exactly what "landing best" means.
    top_watchers = _grouped_union(
        session,
        [PickRow.tmdb_id, PickRow.media_type],
        PickRow.user_id,
        _watched_in(since),
        [SharedRowWatch.tmdb_id, SharedRowWatch.media_type],
        SharedRowWatch.user_id,
        _shared_watched_in(since),
    )
    top_keys = _rank_titles(top_watchers, 8)

    # Display titles for THOSE EIGHT only. Grouping the whole of `picks` to build a full title map ran
    # a GROUP BY over 158,737 rows on every report render, for eight strings.
    titles: dict[tuple[int, str], str] = {}
    wanted = [key for key, _ in top_keys]
    if wanted:
        ids = {tmdb_id for tmdb_id, _mt in wanted}
        for tmdb_id, media_type, title in (
            session.query(SharedRowWatch.tmdb_id, SharedRowWatch.media_type, func.max(SharedRowWatch.title))
            .filter(SharedRowWatch.tmdb_id.in_(ids))
            .group_by(SharedRowWatch.tmdb_id, SharedRowWatch.media_type)
        ):
            titles[(tmdb_id, media_type)] = title
        for tmdb_id, media_type, title in (
            session.query(PickRow.tmdb_id, PickRow.media_type, func.max(PickRow.title))
            .filter(PickRow.tmdb_id.in_(ids))
            .group_by(PickRow.tmdb_id, PickRow.media_type)
        ):
            # A pick row's title wins — it is the delivered one — but only when it HAS one. Overwriting
            # unconditionally blanked the name of any title whose pick rows carry an empty string,
            # leaving a nameless line in "Titles landing best".
            if title:
                titles[(tmdb_id, media_type)] = title
    top_rows = [(key[0], key[1], titles.get(key, ""), watchers) for key, watchers in top_keys]

    per_user = _breakdown(
        per_user_raw,
        lambda uid: (
            {
                "username": users[uid].username,
                "display_name": users[uid].display_name,  # nickname → Tautulli → username
                "slug": users[uid].slug,
            }
            if uid in users
            else None
        ),
    )
    per_row = _breakdown(
        per_row_raw,
        lambda key: {
            "slug": key[0] or DEFAULT_SLUG,
            "section_key": key[1],
            "library": key[2],
            "name": namer.label(key[0], key[2]),
            # History from a row that no longer exists. Flagged so the UI can collapse it out of the
            # way rather than showing it as another nameless copy of the default row.
            "deleted": namer.template(key[0]) is None,
        },
    )
    bounced_now, dropped_now = _engagement_split(session, since)
    recent = _recent_watches(session, users, namer, since)

    return {
        "window": window,
        "window_days": days,
        "since": iso_utc(since) if since else None,
        # The oldest pick on record. Without it the window selector looks broken on a young install:
        # every window covers all of the data, so the numbers are identical whichever you press, and
        # a control that visibly does nothing reads as a bug. The UI uses this to say why.
        "first_pick": first_pick,
        "overall": {
            "delivered": delivered_now,
            "watched": watched_now,
            "watched_prev": watched_prev,
            "watched_delta": _delta(watched_now, watched_prev),
            # Of the watched, the ones they saw out. `watched - finished` is the middle state — a
            # series they are into but have not finished — which has no column of its own because it
            # is a difference, not a third independent count.
            #
            # NO period-over-period delta, deliberately, unlike every other headline here. `watched`
            # is windowed on the watch event, which is complete the instant it happens, so comparing
            # two periods is fair. `finished` is not: this window's picks are counted as finished AS
            # OF NOW, while the previous window's have had a whole extra period to complete, and a
            # series takes weeks to finish. On a server behaving perfectly steadily — 10 picks a day,
            # half eventually finished, a 20-day lag — that asymmetry renders as
            # "finished 50, previous 150, down 100" for ever. A number that reports a permanent
            # decline on a system that is not changing is worse than no number: it is the one figure
            # on this page someone would act on. Measuring the change honestly needs a matured-cohort
            # comparison (what `landing` does), not a shifted window.
            "finished": finished_now,
            # The engagement split, which `watched` alone cannot draw. A pick nobody opened and a pick
            # someone played for three minutes and abandoned are both "not watched" to Plex's flag,
            # and they say opposite things about the recommendation: one never got their attention,
            # the other got it and lost it. `max_percent` comes from live sessions, so it is NULL for
            # anything we did not watch happen — those count as neither, rather than as 0%.
            "bounced": bounced_now,
            "dropped": dropped_now,
            "avg_days_to_watch": avg_now,
            "avg_days_to_watch_delta": _delta(avg_now, avg_prev),
            "landing": landing,
        },
        "watch_sync": {"last": last_watch_sync, "next": next_watch_sync},
        "coverage": {
            "users_enabled": users_enabled,
            "users_total": len(users),
            "users_with_picks": users_with_picks,
            "users_watched": watchers_now,
            "users_watched_delta": _delta(watchers_now, watchers_prev),
            "rows_enabled": rows_enabled,
        },
        "runs": {
            "total": runs_total,
            "in_window": runs_in_window,
            "in_window_delta": _delta(runs_in_window, runs_prev),
            "last_finished": iso_utc(last_run.finished_at) if last_run else None,
            "last_status": last_run.status if last_run else None,
            "errors_last": errors_last or 0,
        },
        "requests": requests,
        "trend": [{"week": week, "watched": n, "finished": finished_by_week.get(week, 0)} for week, n in trend_rows],
        "per_user": per_user,
        "per_row": per_row,
        "top_titles": [{"tmdb_id": tid, "media_type": mt, "title": ttl, "watchers": n} for tid, mt, ttl, n in top_rows],
        "recent": recent,
    }


def row_effectiveness(session: Session, slug: str, now: datetime | None = None) -> dict:
    """Is ONE row working? The numbers the row editor shows beside that row's settings.

    Deliberately not a slice of ``effectiveness``: that report is ~30 queries (its own docstring says
    so, and it runs in a worker thread because it stalled the event loop) and computing all of it to
    print three numbers on a settings page is the wrong trade. This is four queries against the same
    columns and the same definitions, so the two can never disagree about what a "hit" is.

    The rate comes from a MATURED cohort — picks delivered at least ``HIT_WINDOW_DAYS`` ago, so all
    of them have had their chance. A pick's chance actually ends when its row drops it, which is
    usually sooner; this stays the conservative outer bound. Everything younger is counted in the
    all-time totals but kept
    out of the rate, because a row delivered last night has a 0% rate for no reason other than time,
    and a settings page that told someone their new row was failing would send them to change
    settings that were never the problem. `matured` is None until such a cohort exists, and the UI is
    expected to say "too early to tell" rather than render a zero.

    Args:
        session: Open DB session.
        slug: The row's ``collection_slug``, as stamped on its picks.
        now: Clock injection point for tests; defaults to the real UTC now.

    Returns:
        Delivered/watched counts all-time and over the matured cohort, a per-library split of that
        cohort (a row spanning two libraries genuinely performs differently in each), and when the
        row first delivered anything — which is what tells "never run" apart from "ran last night".
    """
    now = now or datetime.now(UTC)
    mine = [PickRow.collection_slug == slug]

    def counts(extra: list) -> tuple[int, int, int]:
        delivered = session.query(func.count(func.distinct(_PERSON_TITLE))).filter(*mine, *extra).scalar() or 0
        watched = (
            session.query(func.count(func.distinct(_PERSON_TITLE)))
            .filter(PickRow.watched_at.isnot(None), *mine, *extra)
            .scalar()
            or 0
        )
        finished = (
            session.query(func.count(func.distinct(_PERSON_TITLE)))
            # See `_finished_in`: finished is drawn inside watched, so the subset is enforced here.
            .filter(PickRow.finished_at.isnot(None), PickRow.watched_at.isnot(None), *mine, *extra)
            .scalar()
            or 0
        )
        return delivered, watched, finished

    delivered_all, watched_all, finished_all = counts([])
    first = session.query(func.min(PickRow.created_at)).filter(*mine).scalar()
    last = session.query(func.max(PickRow.created_at)).filter(*mine).scalar()

    # Counted the SAME way `/api/runs?collection=<slug>` selects them, because the panel's Runs tile
    # links straight to that list — a tile that says 40 above a list of 11 is worse than no tile.
    # Both therefore count runs STILL ON RECORD: `runs.retention` prunes old runs and nulls the
    # picks' run_id (migration 0040, so picks outlive their run), and neither side pretends the
    # pruned ones are still there.
    built_in = session.query(PickRow.run_id).filter(*mine).distinct()
    runs = session.query(func.count(Run.id)).filter(Run.id.in_(built_in)).scalar() or 0

    matured_until = now - timedelta(days=HIT_WINDOW_DAYS)
    cohort = [PickRow.created_at < matured_until]
    cohort_delivered, cohort_watched, cohort_finished = counts(cohort)

    per_library: list[dict] = []
    if cohort_delivered:
        rows = (
            session.query(
                PickRow.library,
                func.count(func.distinct(_PERSON_TITLE)),
                # COUNT(DISTINCT ...) skips NULLs, so an unwatched pick contributes nothing —
                # which is what makes one GROUP BY answer both halves of the ratio per library.
                func.count(func.distinct(case((PickRow.watched_at.isnot(None), _PERSON_TITLE)))),
                func.count(
                    func.distinct(
                        case(((PickRow.finished_at.isnot(None)) & (PickRow.watched_at.isnot(None)), _PERSON_TITLE))
                    )
                ),
            )
            .filter(*mine, *cohort)
            .group_by(PickRow.library)
            .all()
        )
        per_library = [
            {"library": lib or "", "delivered": d, "watched": w, "finished": f, "rate": _rate(w, d)}
            for lib, d, w, f in sorted(rows, key=lambda r: -r[1])
        ]

    return {
        "delivered": delivered_all,
        "watched": watched_all,
        "finished": finished_all,
        "runs": runs,
        "first_delivered_at": iso_utc(first) if first else None,
        "last_delivered_at": iso_utc(last) if last else None,
        "matured_days": HIT_WINDOW_DAYS,
        # None, not a zeroed dict: "no cohort yet" and "a cohort that landed nothing" are different
        # answers and the panel says different things about them.
        "matured": (
            {
                "delivered": cohort_delivered,
                "watched": cohort_watched,
                "finished": cohort_finished,
                "rate": _rate(cohort_watched, cohort_delivered),
                # No `finished_rate` here. The dashboard's landing card has one and renders it; this
                # panel shows the finished COUNT in its Watched tile and a per-library percentage
                # below, so a third figure was computed, serialised and read by nothing.
                "cohort_to": iso_utc(matured_until),
            }
            if cohort_delivered
            else None
        ),
        "per_library": per_library,
    }


def engagement(session: Session, window: str) -> dict:
    """What people did with their picks — the detail behind the Dropped tile.

    Every outcome comes from :func:`resolve_outcomes`, the same function the headline split reads, so
    the two can never disagree. Two views of one set, because they answer different questions: `people`
    is "what did THIS person do with their row", which an owner opens when someone says the picks are
    no good; `losing` is "what does everyone do with THIS pick", which says a title is a bad
    recommendation rather than a bad night.
    """
    if window not in WINDOWS:
        window = DEFAULT_WINDOW
    days = WINDOWS[window]
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    users = {u.id: u for u in session.query(User).all()}
    namer = _RowNamer(session, SettingsStore(session).get("row.name_template") or DEFAULT_ROW_TEMPLATE)

    people: dict[int, list[dict]] = defaultdict(list)
    per_title: dict[tuple[int, str], dict] = {}
    abandoned: list[int] = []
    for (user_id, tmdb_id, media_type), entry in resolve_outcomes(session, since).items():
        if user_id not in users:
            continue
        people[user_id].append(
            {
                "title": entry["title"],
                "row": namer.label(entry["row"], entry["library"]),
                "media_type": media_type,
                "outcome": entry["outcome"],
                "percent": entry["percent"],
                "watched_at": iso_utc(entry["watched_at"]) if entry["watched_at"] else None,
                "finished_at": iso_utc(entry["finished_at"]) if entry["finished_at"] else None,
                "observed_at": iso_utc(entry["observed_at"]) if entry["observed_at"] else None,
            }
        )
        agg = per_title.setdefault(
            (tmdb_id, media_type),
            {"title": entry["title"], "media_type": media_type, "started": 0, "finished": 0, "percents": []},
        )
        if entry["outcome"] == "finished":
            agg["started"] += 1
            agg["finished"] += 1
        elif entry["percent"] is not None:
            agg["started"] += 1
            agg["percents"].append(entry["percent"])
            abandoned.append(entry["percent"])

    def median(values: list[int]) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    # Titles that LOSE people. Gated on TWO OBSERVED abandonments, not on `started >= 2`: `started`
    # used to include people whose progress is unknown, so one credited pre-tracking pick plus one
    # real drop rendered as "2 started · 0 finished · stops at 2%" — a pattern claimed from a single
    # data point, under a heading that says one person abandoning something is not a signal.
    losing = [
        {
            "title": agg["title"],
            "media_type": agg["media_type"],
            "started": agg["started"],
            "finished": agg["finished"],
            "stops_at": median(agg["percents"]),
        }
        for agg in per_title.values()
        if len(agg["percents"]) >= 2 and agg["finished"] * 2 <= agg["started"]
    ]
    losing.sort(key=lambda t: (-t["started"], t["stops_at"] or 0))

    buckets = [("0-10%", 0, 10), ("10-25%", 10, 25), ("25-50%", 25, 50), ("50-75%", 50, 75), ("75%+", 75, 101)]
    # Sorted so the OBSERVED outcomes lead, then truncated. Sorting finished-first and cutting at 40
    # removed exactly the rows this page exists to show: a person with 45 finished picks and 5 fresh
    # drops saw forty "finished" and no drops at all, under a header reading "40 picks".
    order = {"bounced": 0, "dropped": 1, "finished": 2, "watching": 3}
    out_people = []
    for uid, entries in sorted(people.items(), key=lambda kv: -len(kv[1])):
        entries.sort(key=lambda e: (order.get(e["outcome"], 9), e["observed_at"] or ""), reverse=False)
        out_people.append(
            {
                "username": users[uid].username,
                "display_name": users[uid].display_name,
                "picks": entries[:40],
                "total": len(entries),
            }
        )
    return {
        "window": window,
        "people": out_people,
        "losing": losing[:20],
        "stop_points": [
            {"label": label, "count": sum(1 for p in abandoned if lo <= p < hi)} for label, lo, hi in buckets
        ],
        # Whether any live playback has been OBSERVED at all. The panel's empty state used to gate on
        # `people` being empty, which never happens on a server with existing picks — so the owner got
        # a wall of "WATCHING · —" rows and five empty bars instead of the explanation.
        "observed": bool(abandoned) or any(p["percent"] is not None for e in out_people for p in e["picks"]),
    }
