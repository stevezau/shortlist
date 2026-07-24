"""Watch-history sources: Tautulli (preferred) and Plex's own history API (fallback)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from loguru import logger

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.tautulli import TautulliClient
from shortlist.engine.models import MediaType, Seed, UserProfile, UserType, WatchedItem


def _as_int(value: object) -> int | None:
    """Parse a possibly-str/None index (season/episode) to int, or None if absent/unparseable."""
    if value in (None, ""):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class HistorySource(Protocol):
    def fetch(
        self, user: UserProfile, *, min_completion: float, since: datetime | None = None
    ) -> list[WatchedItem]: ...


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

    def fetch(self, user: UserProfile, *, min_completion: float, since: datetime | None = None) -> list[WatchedItem]:
        try:
            items = self._primary.fetch(user, min_completion=min_completion, since=since)
        except Exception as e:
            logger.warning("{}: primary history source failed ({}); using fallback", user.username, e)
            return self._fallback.fetch(user, min_completion=min_completion, since=since)
        if len(items) >= self._min_items:
            return items
        fallback_items = self._fallback.fetch(user, min_completion=min_completion, since=since)
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

    def fetch(self, user: UserProfile, *, min_completion: float, since: datetime | None = None) -> list[WatchedItem]:
        since_ts = int(since.timestamp()) if since is not None else None
        rows = self._client.get_history(user.plex_account_id, since_ts=since_ts)
        items = []
        for row in rows:
            completion = int(row.get("percent_complete") or 0) / 100
            if completion < min_completion:
                continue
            is_episode = row.get("media_type") == "episode"
            media_type = MediaType.SHOW if is_episode else MediaType.MOVIE
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
                    # Tautulli keys season/episode as parent_media_index/media_index; the row's own
                    # `title` is the episode name (the show is grandparent_title, used above).
                    season=_as_int(row.get("parent_media_index")) if is_episode else None,
                    episode=_as_int(row.get("media_index")) if is_episode else None,
                    episode_title=(row.get("title") or None) if is_episode else None,
                )
            )
        logger.debug("{}: {} meaningful watches from Tautulli", user.username, len(items))
        return items


class PlexHistorySource:
    """Zero-config fallback: PMS /status/sessions/history/all per accountID with the owner token."""

    def __init__(self, client: PlexClient, *, roster_account_ids: frozenset[int] = frozenset()):
        self._client = client
        # Every plex.tv account id Shortlist knows. Used only to resolve the OWNER's local PMS
        # account: anything in here demonstrably belongs to someone else, so it can never be it.
        self._roster_account_ids = roster_account_ids

    def fetch(self, user: UserProfile, *, min_completion: float, since: datetime | None = None) -> list[WatchedItem]:
        # PMS history rows carry no completion percentage; presence in history is the signal.
        items = []
        # Only the owner is asked for: every other account is in PMS's table under the id we already
        # hold, so this spends a `/accounts` read on the one person it can be wrong for.
        account_id = (
            self._client.system_account_id(
                user.plex_account_id,
                user.username,
                exclude_ids=self._roster_account_ids - {user.plex_account_id},
            )
            if user.user_type is UserType.OWNER
            else user.plex_account_id
        )
        for entry in self._client.history_for_account(account_id, since=since):
            is_episode = entry.type == "episode"
            media_type = MediaType.SHOW if is_episode else MediaType.MOVIE
            title = getattr(entry, "grandparentTitle", None) or getattr(entry, "title", "")
            if not title:
                continue
            viewed_at = getattr(entry, "viewedAt", None)
            if viewed_at is None:
                # No timestamp -> can't position it in history, and a `now()` fallback would get a
                # fresh time on every pull and duplicate in the watch-history store. Skip it.
                continue
            items.append(
                WatchedItem(
                    title=title,
                    media_type=media_type,
                    watched_at=viewed_at.replace(tzinfo=UTC) if viewed_at.tzinfo is None else viewed_at,
                    rating_key=int(entry.grandparentRatingKey)
                    if getattr(entry, "grandparentRatingKey", None)
                    else (int(entry.ratingKey) if getattr(entry, "ratingKey", None) else None),
                    # PMS keys season/episode as parentIndex/index; the entry's own `title` is the
                    # episode name (the show is grandparentTitle, used above).
                    season=_as_int(getattr(entry, "parentIndex", None)) if is_episode else None,
                    episode=_as_int(getattr(entry, "index", None)) if is_episode else None,
                    episode_title=(getattr(entry, "title", None) or None) if is_episode else None,
                )
            )
        logger.debug("{}: {} watches from Plex history API", user.username, len(items))
        return items


def distinct_recent(history: list[WatchedItem], limit: int) -> list[WatchedItem]:
    """The most-recent DISTINCT titles, newest first — episodes of a show collapse to the one show.

    A binge counts once: 20 episodes of the same show yield a single entry, so it doesn't crowd out
    everything else and the caller sees real variety. Looks back through the WHOLE history to fill
    ``limit`` distinct titles (a person who only ever watched one show still returns just that one —
    we can't invent watches). The kept item per title is its most recent watch.

    Args:
        history: Meaningful watches, any order.
        limit: Max distinct titles to return.
    """
    seen: set[tuple[str, MediaType]] = set()
    out: list[WatchedItem] = []
    for item in sorted(history, key=lambda w: w.watched_at, reverse=True):
        key = (item.title, item.media_type)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


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
        seeds.append(
            Seed(
                tmdb_id=tmdb_id,
                title=title,
                media_type=media_type,
                weight=len(items) * recency_weight,
                watch_count=len(items),
                recency_days=recency_days,
            )
        )
    seeds.sort(key=lambda s: s.weight, reverse=True)

    # Guarantee each media type the person watches a share of the seed budget. Otherwise the global
    # top-N by weight can be entirely one type — a TV-heavy watcher's 30 seeds are all shows, so the
    # movie half of a `media=both` row gets no candidates and never builds (SFLIX/MooHouse: 58 of her
    # last 60 watches were TV, so her Movies row stayed empty despite 598 movie watches; 2026-07-20).
    movies = [s for s in seeds if s.media_type is MediaType.MOVIE]
    shows = [s for s in seeds if s.media_type is MediaType.SHOW]
    if not (movies and shows):
        return seeds[:max_seeds]  # single media type — nothing to balance
    per_type = max(1, max_seeds // 3)  # each present type keeps >= a third of the budget (if it has that many)
    reserved = {id(s) for s in movies[:per_type]} | {id(s) for s in shows[:per_type]}
    # Reserved seeds first, then the rest — but weight order is preserved WITHIN each group (both lists
    # are already weight-sorted), so a balanced watcher's ordering is unchanged; only a lopsided one's
    # minority-media seeds get promoted above the cutoff.
    ordered = [s for s in seeds if id(s) in reserved] + [s for s in seeds if id(s) not in reserved]
    return ordered[:max_seeds]
