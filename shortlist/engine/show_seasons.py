"""Which seasons of a show a request names — the rules both request routes share.

Pure: no I/O and no server imports. "First/last N seasons" is the same question whether the request
goes to Sonarr directly or through Overseerr, so the answer lives here once.
"""

from __future__ import annotations

from datetime import date


def pick_season_numbers(numbers: list[int], mode: str, count: int) -> list[int]:
    """The season numbers ``mode`` selects out of ``numbers``, ascending.

    Args:
        numbers: Every season number the show has; season 0 (specials) is dropped.
        mode: ``firstSeason`` takes the lowest ``count``, ``lastSeason`` the highest. Any other mode
            selects nothing — it has no season list to speak of.
        count: How many seasons; below 1 is treated as 1.

    Returns:
        The chosen season numbers, ascending.
    """
    real = sorted({n for n in numbers if n > 0})
    count = max(1, count)
    if mode == "firstSeason":
        return real[:count]
    if mode == "lastSeason":
        return real[-count:]
    return []


def tmdb_request_seasons(details: dict, mode: str, count: int, today: date) -> list[int]:
    """The seasons to request for a show, from its TMDB ``/tv/{id}`` details.

    Candidates are seasons with episodes and a number above 0 — the filter Seerr applies itself to
    ``"all"``. For ``lastSeason`` a season TMDB has announced but not started airing is skipped, so
    "the newest season" means the newest one a viewer can watch; if nothing has aired yet, every
    candidate stays in play rather than requesting nothing.

    Returns:
        The season numbers, ascending; empty when the show lists no usable season.
    """
    candidates = [
        s
        for s in details.get("seasons") or []
        if (s.get("season_number") or 0) > 0 and (s.get("episode_count") or 0) > 0
    ]
    if mode == "lastSeason":
        aired = [s for s in candidates if _has_aired(s.get("air_date"), today)]
        candidates = aired or candidates
    return pick_season_numbers([s["season_number"] for s in candidates], mode, count)


def _has_aired(air_date: str | None, today: date) -> bool:
    """True when TMDB's ``YYYY-MM-DD`` air date is set and not in the future."""
    if not air_date:
        return False
    try:
        return date.fromisoformat(air_date) <= today
    except ValueError:
        return False


def describe_seasons(numbers: list[int]) -> str:
    """A short human label: ``season 5``, ``seasons 1 to 3`` (en dash) or, for a gap, ``seasons 1, 3``."""
    if len(numbers) == 1:
        return f"season {numbers[0]}"
    if numbers == list(range(numbers[0], numbers[-1] + 1)):
        return f"seasons {numbers[0]}\u2013{numbers[-1]}"
    return "seasons " + ", ".join(str(n) for n in numbers)
