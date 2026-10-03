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


def apply_limits(candidates: list[Candidate], limits: RowLimits, tmdb: TmdbClient) -> LimitResult:
    """Drop candidates outside ``limits``. Returns ``candidates`` itself, untouched, when none are set.

    Year and rating come from data the candidate already carries. Runtime costs a (cached) details call,
    made only when ``max_runtime`` is set and only for titles that passed the cheaper checks.
    """
    if not limits.active:
        return LimitResult(kept=candidates)
    result = LimitResult(kept=[])
    for c in candidates:
        if _outside_year_or_rating(c, limits):
            result.dropped += 1
            result.dropped_candidates.append(c)
            continue
        if c.year is None and (limits.min_year is not None or limits.max_year is not None):
            result.unknown += 1
        if limits.max_runtime is not None:
            minutes = _runtime_of(c, tmdb)
            if minutes is None:
                result.unknown += 1
            elif minutes > limits.max_runtime:
                result.dropped += 1
                result.dropped_candidates.append(c)
                continue
        result.kept.append(c)
    return result


def _outside_year_or_rating(c: Candidate, limits: RowLimits) -> bool:
    if c.year is not None:
        if limits.min_year is not None and c.year < limits.min_year:
            return True
        if limits.max_year is not None and c.year > limits.max_year:
            return True
    return limits.min_rating is not None and c.rating < limits.min_rating


def _runtime_of(c: Candidate, tmdb: TmdbClient) -> int | None:
    try:
        return runtime_minutes(tmdb.details(c.tmdb_id, c.media_type), c.media_type)
    except Exception:
        logger.debug("limits: no runtime for {} {} (details failed)", c.media_type, c.tmdb_id)
        return None
