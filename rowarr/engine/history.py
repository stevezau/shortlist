"""Watch-history sources: Tautulli (preferred) and Plex's own history API (fallback)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from loguru import logger

from rowarr.engine.clients.plex import PlexClient
from rowarr.engine.clients.tautulli import TautulliClient
from rowarr.engine.models import MediaType, Seed, UserProfile, WatchedItem


class HistorySource(Protocol):
    def fetch(self, user: UserProfile, *, min_completion: float) -> list[WatchedItem]: ...


class FallbackHistorySource:
    """Per-user fallback between sources.

    Tautulli only logs sessions it observed live, so a user can have years of history on the
    PMS that Tautulli never saw (seen in the Phase 1 pilot: 11k PMS rows vs 1 Tautulli row).
    If the primary source yields fewer than `min_items`, the fallback is consulted and the
    richer result wins.
    """

    def __init__(self, primary: HistorySource, fallback: HistorySource, *, min_items: int = 10):
        self._primary = primary
        self._fallback = fallback
        self._min_items = min_items

    def fetch(self, user: UserProfile, *, min_completion: float) -> list[WatchedItem]:
        try:
            items = self._primary.fetch(user, min_completion=min_completion)
        except Exception as e:
            logger.warning("{}: primary history source failed ({}); using fallback", user.username, e)
            return self._fallback.fetch(user, min_completion=min_completion)
        if len(items) >= self._min_items:
            return items
        fallback_items = self._fallback.fetch(user, min_completion=min_completion)
        if len(fallback_items) > len(items):
            logger.info(
                "{}: primary history thin ({} items) — using fallback ({} items)",
                user.username,
                len(items),
                len(fallback_items),
            )
            return fallback_items
        return items


class TautulliSource:
    """Deeper, more reliable history via Tautulli's get_history."""

    def __init__(self, client: TautulliClient):
        self._client = client

    def fetch(self, user: UserProfile, *, min_completion: float) -> list[WatchedItem]:
        rows = self._client.get_history(user.plex_account_id)
        items = []
        for row in rows:
            completion = int(row.get("percent_complete") or 0) / 100
            if completion < min_completion:
                continue
            media_type = MediaType.SHOW if row.get("media_type") == "episode" else MediaType.MOVIE
            title = row.get("grandparent_title") or row.get("title") or ""
            if not title:
                continue
            items.append(
                WatchedItem(
                    title=title,
                    media_type=media_type,
                    watched_at=datetime.fromtimestamp(int(row.get("date") or 0), tz=UTC),
                    year=int(row["year"]) if row.get("year") else None,
                    rating_key=int(row["grandparent_rating_key"] or row["rating_key"])
                    if row.get("grandparent_rating_key") or row.get("rating_key")
                    else None,
                    completion=completion,
                )
            )
        logger.debug("{}: {} meaningful watches from Tautulli", user.username, len(items))
        return items


class PlexHistorySource:
    """Zero-config fallback: PMS /status/sessions/history/all per accountID with the owner token."""

    def __init__(self, client: PlexClient):
        self._client = client

    def fetch(self, user: UserProfile, *, min_completion: float) -> list[WatchedItem]:
        # PMS history rows carry no completion percentage; presence in history is the signal.
        items = []
        for entry in self._client.history_for_account(user.plex_account_id):
            media_type = MediaType.SHOW if entry.type == "episode" else MediaType.MOVIE
            title = getattr(entry, "grandparentTitle", None) or getattr(entry, "title", "")
            if not title:
                continue
            viewed_at = getattr(entry, "viewedAt", None)
            items.append(
                WatchedItem(
                    title=title,
                    media_type=media_type,
                    watched_at=viewed_at.replace(tzinfo=UTC)
                    if viewed_at and viewed_at.tzinfo is None
                    else (viewed_at or datetime.now(UTC)),
                    rating_key=int(entry.grandparentRatingKey)
                    if getattr(entry, "grandparentRatingKey", None)
                    else (int(entry.ratingKey) if getattr(entry, "ratingKey", None) else None),
                )
            )
        logger.debug("{}: {} watches from Plex history API", user.username, len(items))
        return items


def derive_seeds(
    history: list[WatchedItem],
    resolve_tmdb_id,
    *,
    max_seeds: int = 30,
) -> list[Seed]:
    """Collapse history into weighted seeds: distinct titles, frequency x recency weighted.

    Args:
        history: Meaningful watches, any order.
        resolve_tmdb_id: Callable (WatchedItem) -> int | None; adapters resolve via the
            library index or TMDB search. Items that resolve to None are skipped.
        max_seeds: Cap (most-recent/most-watched titles win).
    """
    if not history:
        return []
    newest = max(item.watched_at for item in history)
    by_title: dict[tuple[str, MediaType], list[WatchedItem]] = {}
    for item in history:
        by_title.setdefault((item.title, item.media_type), []).append(item)

    seeds = []
    for (title, media_type), items in by_title.items():
        tmdb_id = resolve_tmdb_id(items[0])
        if tmdb_id is None:
            continue
        recency_days = (newest - max(i.watched_at for i in items)).days
        recency_weight = max(0.25, 1.0 - recency_days / 90)  # linear decay over ~3 months
        seeds.append(Seed(tmdb_id=tmdb_id, title=title, media_type=media_type, weight=len(items) * recency_weight))
    seeds.sort(key=lambda s: s.weight, reverse=True)
    return seeds[:max_seeds]
