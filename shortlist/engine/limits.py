"""Per-row limits on length, release year and rating (#138 phase 2).

Pure: takes candidates and a TMDB client, returns what survives. A title whose year, rating or runtime
cannot be determined is KEPT and counted as unknown — a limit must not silently empty a row because TMDB
hiccuped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.models import Candidate, MediaType, RowLimits

__all__ = ["LimitResult", "RowLimits", "apply_limits", "runtime_minutes"]


_MAX_CONSECUTIVE_FAILURES = 5


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

    @property
    def open(self) -> bool:
        return self._streak >= _MAX_CONSECUTIVE_FAILURES

    def get(self, c: Candidate) -> dict | None:
        if self.open:
            self.skipped += 1
            return None
        try:
            details = self._tmdb.details(c.tmdb_id, c.media_type)
        except Exception:
            self._streak += 1
            self.failures += 1
            logger.debug("limits: details failed for {} {}", c.media_type, c.tmdb_id)
            return None
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


def _outside_year(c: Candidate, limits: RowLimits) -> bool:
    if c.year is None:
        return False
    if limits.min_year is not None and c.year < limits.min_year:
        return True
    return limits.max_year is not None and c.year > limits.max_year
