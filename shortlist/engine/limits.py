"""Per-row limits on length, release year and rating (#138 phase 2).

Pure: takes candidates and a TMDB client, returns what survives. A title whose year, rating or runtime
cannot be determined is KEPT and counted as unknown — a limit must not silently empty a row because TMDB
hiccuped.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import Candidate, MediaType, RowLimits

__all__ = [
    "LimitResult",
    "RowLimits",
    "apply_limits",
    "apply_runtime_limit",
    "passes_year_and_rating",
    "runtime_minutes",
]


_MAX_CONSECUTIVE_FAILURES = 5
#: Details lookups in flight at once for a pool of titles; the TMDB client's own retry/backoff still applies.
DETAILS_WORKERS = 8


@dataclass
class LimitResult:
    kept: list[Candidate]
    dropped: int = 0
    unknown: int = 0
    dropped_candidates: list[Candidate] = field(default_factory=list)


def runtime_minutes(details: dict, media_type: str) -> int | None:
    """A title's length in minutes from a TMDB details payload, or None when it carries none.

    A movie reads ``runtime``. A show reads the first ``episode_run_time``, else the last aired
    episode's ``runtime``. 0 and missing both mean unknown, never "zero minutes".
    """
    if media_type == MediaType.MOVIE:
        minutes = details.get("runtime")
    else:
        episode_times = details.get("episode_run_time") or []
        minutes = episode_times[0] if episode_times else (details.get("last_episode_to_air") or {}).get("runtime")
    return minutes if isinstance(minutes, int) and minutes > 0 else None


class _DetailsFetcher:
    """TMDB details with a circuit breaker: after ``_MAX_CONSECUTIVE_FAILURES`` failures in a row, stop asking.

    Failures are not cached and a stalled TMDB can take ~90s each, so a pool of hundreds would otherwise
    stall the run for hours. Titles that would have needed details count as unknown (kept).
    """

    def __init__(self, tmdb: TmdbClient) -> None:
        self._tmdb = tmdb
        self._streak = 0
        self.failures = 0
        self.skipped = 0
        self._lock = threading.Lock()  # `apply_runtime_limit` calls `get` from several threads

    @property
    def open(self) -> bool:
        return self._streak >= _MAX_CONSECUTIVE_FAILURES

    def get(self, c: Candidate) -> dict | None:
        if self.open:
            with self._lock:
                self.skipped += 1
            return None
        try:
            details = self._tmdb.details(c.tmdb_id, c.media_type)
        except Exception:
            with self._lock:
                self._streak += 1
                self.failures += 1
            logger.debug("limits: details failed for {} {}", c.media_type, c.tmdb_id)
            return None
        with self._lock:
            self._streak = 0
        return details


def apply_limits(candidates: list[Candidate], limits: RowLimits, tmdb: TmdbClient) -> LimitResult:
    """Drop candidates outside ``limits``. Returns ``candidates`` itself, untouched, when none are set.

    Year comes from data the candidate already carries. Rating does too, except for a title with no votes
    (Trakt candidates arrive 0.0/0): those look up TMDB details, and stay unknown if that fails. Runtime
    needs details whenever ``max_runtime`` is set. Details are cached by the client, and one title is
    fetched once per call even when both rating and runtime need it.
    """
    if not limits.active:
        return LimitResult(kept=candidates)
    result = LimitResult(kept=[])
    fetcher = _DetailsFetcher(tmdb)
    for c in candidates:
        unknown = c.year is None and (limits.min_year is not None or limits.max_year is not None)
        dropped = _outside_year(c, limits)
        details: dict | None = None
        fetched = False
        if not dropped and limits.rating_limited and limits.min_rating is not None:
            rating = c.rating
            if c.vote_count == 0:
                details, fetched = fetcher.get(c), True
                if details is None:
                    rating = None
                else:
                    voted = bool(details.get("vote_count"))
                    rating = float(details.get("vote_average") or 0.0) if voted else 0.0
            if rating is None:
                unknown = True
            elif rating < limits.min_rating:
                dropped = True
        if not dropped and limits.max_runtime is not None:
            if not fetched:
                details = fetcher.get(c)
            minutes = runtime_minutes(details, c.media_type) if details is not None else None
            if minutes is None:
                unknown = True
            elif minutes > limits.max_runtime:
                dropped = True
        if dropped:
            result.dropped += 1
            result.dropped_candidates.append(c)
            continue
        result.unknown += unknown
        result.kept.append(c)
    if fetcher.failures:
        logger.warning(
            "limits: {} TMDB details lookups failed{}; {} titles kept as unknown",
            fetcher.failures,
            f", then stopped asking after {_MAX_CONSECUTIVE_FAILURES} in a row" if fetcher.open else "",
            result.unknown,
        )
    return result


def apply_runtime_limit(
    candidates: list[Candidate], max_runtime: int, tmdb: TmdbClient, *, workers: int = DETAILS_WORKERS
) -> LimitResult:
    """Drop candidates longer than ``max_runtime`` minutes, looking titles up ``workers`` at a time.

    The same rule as ``apply_limits``' runtime check, for a pool of hundreds: results keep ``candidates``'
    order whatever order the lookups finish in, and the circuit breaker is shared, so after repeated
    failures the remaining titles are kept as unknown without being asked for.
    """
    fetcher = _DetailsFetcher(tmdb)
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="runtime-check") as pool:
        fetched = list(pool.map(fetcher.get, candidates))
    result = LimitResult(kept=[])
    for c, details in zip(candidates, fetched, strict=True):
        minutes = runtime_minutes(details, c.media_type) if details is not None else None
        if minutes is not None and minutes > max_runtime:
            result.dropped += 1
            result.dropped_candidates.append(c)
            continue
        result.unknown += minutes is None
        result.kept.append(c)
    if fetcher.failures:
        logger.warning(
            "limits: {} TMDB details lookups failed{}; {} titles kept as unknown",
            fetcher.failures,
            f", then stopped asking after {_MAX_CONSECUTIVE_FAILURES} in a row" if fetcher.open else "",
            result.unknown,
        )
    return result


def passes_year_and_rating(c: Candidate, limits: RowLimits) -> bool:
    """Whether ``c`` is inside the year and rating limits, judged only from what it already carries.

    The free half of ``apply_limits``, with the same unknown-is-kept rule: no year, no rating, or a title
    with no votes (rating 0.0, which ``apply_limits`` would look up) passes. Runtime is not judged: it
    needs a TMDB lookup per title, and a missing title is not worth one.
    """
    if _outside_year(c, limits):
        return False
    if limits.rating_limited and limits.min_rating is not None and c.vote_count != 0 and c.rating is not None:
        return c.rating >= limits.min_rating
    return True


def _outside_year(c: Candidate, limits: RowLimits) -> bool:
    if c.year is None:
        return False
    if limits.min_year is not None and c.year < limits.min_year:
        return True
    return limits.max_year is not None and c.year > limits.max_year
