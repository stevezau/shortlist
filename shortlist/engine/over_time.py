"""Night-to-night controls on an AI row (#138): no repeats within N days, and keep-out rows.

Pure functions over a history the adapter supplies; nothing here reads the database or Plex.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

from shortlist.engine.models import Candidate, OverTime, TitleKey


class PickHistory(Protocol):
    """What rows showed on earlier real runs, read by the adapter from its own records."""

    def first_shown_since(self, user_slug: str, row_slug: str, since: date) -> set[TitleKey]:
        """Titles whose FIRST real-run appearance for (user, row) is on or after ``since``."""
        ...

    def latest(self, user_slug: str, row_slug: str) -> set[TitleKey]:
        """The picks of the newest real run that wrote (user, row); empty when there is none."""
        ...


@dataclass(frozen=True)
class ExclusionResult:
    kept: list[Candidate]
    dropped: int
    skipped: bool  # True when exclusions were ignored to avoid emptying the row


def excluded_titles(
    over_time: OverTime,
    *,
    user_slug: str,
    row_slug: str,
    today: date,
    history: PickHistory | None,
    built_this_run: Mapping[tuple[str, str], set[TitleKey]],
    spare: set[TitleKey],
) -> set[TitleKey]:
    """Cooldown titles plus avoid-rows titles, minus ``spare`` (the titles kept in this row tonight)."""
    excluded: set[TitleKey] = set()
    if over_time.repeat_cooldown_days is not None and history is not None:
        since = today - timedelta(days=over_time.repeat_cooldown_days)
        excluded |= history.first_shown_since(user_slug, row_slug, since)
    for avoid_slug in over_time.avoid_rows:
        if avoid_slug == row_slug:
            continue
        built = built_this_run.get((user_slug, avoid_slug))
        if built is not None:
            excluded |= built
        elif history is not None:
            excluded |= history.latest(user_slug, avoid_slug)
    return excluded - spare


def apply_exclusions(candidates: list[Candidate], excluded: set[TitleKey]) -> ExclusionResult:
    """Drop excluded candidates, keeping order. If that would leave nothing, ignore the exclusions."""
    if not excluded:
        return ExclusionResult(kept=candidates, dropped=0, skipped=False)
    kept = [c for c in candidates if (c.media_type, c.tmdb_id) not in excluded]
    if not kept:
        return ExclusionResult(kept=candidates, dropped=0, skipped=bool(candidates))
    return ExclusionResult(kept=kept, dropped=len(candidates) - len(kept), skipped=False)
