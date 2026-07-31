"""Watch-history source: each user's complete watched set, read from the PMS AS them.

``ShareTokenWatchSource`` is the one source. plex.tv mints a per-user server token for every shared
invite; passed to the PMS it reads the library with that user's own ``viewCount``/``viewedLeafCount``
— so a mark-as-watched (which the playback-history API never returns, issue #12) is seen, and no PMS
database mount is needed. It supersedes the old Tautulli / Plex-history-API sources, which saw only
playback sessions and capped at ~200 rows.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Protocol

from loguru import logger

from shortlist.engine.clients.plex_pms import PlexClient
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.models import MediaType, Seed, UserProfile, UserType, WatchedItem

# Seed weight halves every ~6 weeks since the watch: a title seen today weighs ~2x one from 6 weeks
# ago, ~4x one from 3 months ago. Long enough that a season finished last month still seeds strongly,
# short enough that a years-old rewatch fades to near-zero.
RECENCY_HALF_LIFE_DAYS = 45.0


class HistorySource(Protocol):
    def fetch(
        self, user: UserProfile, *, min_completion: float, since: datetime | None = None
    ) -> list[WatchedItem]: ...


class ShareTokenWatchSource:
    """Each user's COMPLETE watched set, read from the PMS with that user's own server token.

    plex.tv mints a per-user server ``accessToken`` for every shared invite; passed to the PMS it
    reads the library AS that user — with their ``viewCount``/``viewedLeafCount``, which INCLUDE a
    mark-as-watched. The playback-history API never returns a mark (issue #12) and capped at ~200
    rows; this returns everything, marks and all, in one read per library, with no PMS database mount.

    Token per user (rule 9 — a live per-user credential, kept in memory for the run, never logged):
      * OWNER  — the owner is not shared to their own server, so read with the admin token.
      * SHARED / Home — plex.tv lists them in ``shared_servers`` with a token; one call covers the roster.
      * a MANAGED sub-account with no share invite — switch to it and exchange for a server token
        (the same path the privacy canary uses).
    """

    def __init__(self, plex: PlexClient, plextv: PlexTvClient, *, owner_token: str):
        self._plex = plex
        self._plextv = plextv
        self._owner_token = owner_token
        # {plex_account_id: server token} for the shared roster, fetched once and reused for the run.
        self._shared_tokens: dict[int, str] | None = None
        # fetch() runs per-user inside a ThreadPoolExecutor when run.concurrency > 1, all sharing this
        # one instance — the lock makes the roster fetch happen exactly once instead of N racing GETs
        # bursting plex.tv (rule 6: be polite to shared infra).
        self._tokens_lock = threading.Lock()

    def _tokens(self) -> dict[int, str]:
        with self._tokens_lock:
            if self._shared_tokens is None:
                self._shared_tokens = self._plextv.shared_server_tokens()
            return self._shared_tokens

    def _token_for(self, user: UserProfile) -> str | None:
        """The server token to read this user's watched state with, or None if none can be obtained."""
        if user.user_type is UserType.OWNER:
            return self._owner_token
        token = self._tokens().get(user.plex_account_id)
        if token is not None:
            return token
        # Not in the shared list: a managed Home profile with no invite of its own. Switch + exchange.
        try:
            return self._plextv.canary_server_token(user.plex_account_id)
        except Exception as e:
            logger.warning(
                "{}: no server token available ({}) — treating as no watch history", user.username, type(e).__name__
            )
            return None

    def fetch_section(
        self,
        user: UserProfile,
        section,
        media_type: MediaType,
        *,
        since: datetime | None = None,
    ) -> list[WatchedItem]:
        """One library's watched titles for this user — the unit the server's cache syncs.

        Separate from `fetch` because the cache tracks a cursor PER (person, library): a single
        section can legitimately be mid-way through a full re-read while its neighbour is already
        incremental, and one cursor spanning both would have to take the older of the two for ever.

        Raises rather than swallowing: the caller decides whether one unreadable library is fatal,
        and for the cache it must be — advancing a cursor past a read that failed would silently
        skip whatever it missed.
        """
        token = self._token_for(user)
        if token is None:
            return []
        return self._plex.watched_titles(section.key, media_type, token, since=since)

    def fetch(self, user: UserProfile, *, min_completion: float, since: datetime | None = None) -> list[WatchedItem]:
        """Watched titles across every movie/show library, as this user.

        ``min_completion`` needs no reconstruction here: ``unwatched=0`` already excludes a
        partially-watched movie (Plex counts a title watched only at ``viewCount>0``).

        ``since`` makes this an INCREMENTAL read — only titles viewed at or after that moment. It is
        a partial answer by construction: it cannot see a title that was un-watched, deleted, or whose
        ``lastViewedAt`` never moved, so a caller that keeps a cache MUST still do a periodic full
        read to reconcile. ``None`` (the default) is the complete read, and is what a direct engine
        run always does — the engine holds no state between runs to be incremental against.
        """
        token = self._token_for(user)
        if token is None:
            return []
        items: list[WatchedItem] = []
        for section in self._plex.sections():
            media_type = MediaType.MOVIE if section.type == "movie" else MediaType.SHOW
            try:
                items.extend(self._plex.watched_titles(section.key, media_type, token, since=since))
            except Exception as e:
                # One unreadable library degrades to "nothing watched there" (it may re-surface a title
                # they've seen), never a failed run — the same fail-soft stance the old sources took.
                logger.warning(
                    "{}: watched read failed for section {} ({})", user.username, section.key, type(e).__name__
                )
        logger.debug(
            "{}: {} watched titles via share token{}",
            user.username,
            len(items),
            f" since {since.isoformat()}" if since else "",
        )
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
    blocked: set[int] | None = None,
) -> list[Seed]:
    """Collapse history into weighted seeds: distinct titles, weighted purely by RECENCY.

    Weight is ``0.5 ** (recency_days / RECENCY_HALF_LIFE_DAYS)`` — an exponential decay off the
    person's most-recent watch, with NO frequency term. What someone reached for lately is the honest
    signal of what to recommend tonight; watch_count is deliberately excluded from the weight because
    an old favourite rewatched many times years ago (SFLIX/MooHouse: The Girl on the Train, 18x but
    ~8.7 years ago) would otherwise dominate the seeds over a title watched once yesterday. Because
    the weight is strictly monotonic in recency, the seed ORDER now matches the "recent watches" panel
    exactly. ``watch_count`` is still carried on each Seed for display ("watched 4x"), just not scored.

    Args:
        history: Watched titles, any order.
        resolve_tmdb_id: Callable (WatchedItem) -> int | None, used only when an item carries no
            ``tmdb_id`` of its own (adapters resolve via the library index or TMDB search). Items that
            resolve to None are skipped.
        max_seeds: Cap (the most recently watched titles win).
    """
    if not history:
        return []
    newest = max(item.watched_at for item in history)
    by_title: dict[tuple[str, MediaType], list[WatchedItem]] = {}
    for item in history:
        by_title.setdefault((item.title, item.media_type), []).append(item)

    seeds = []
    for (title, media_type), items in by_title.items():
        # The item's own tmdb_id (the share-token source inlines it from the PMS GUID) wins; only
        # fall back to the resolver for a source that didn't set one.
        tmdb_id = items[0].tmdb_id if items[0].tmdb_id is not None else resolve_tmdb_id(items[0])
        if tmdb_id is None:
            continue
        if blocked and tmdb_id in blocked:
            continue
        watch_count = sum(i.watch_count for i in items)
        recency_days = (newest - max(i.watched_at for i in items)).days
        seeds.append(
            Seed(
                tmdb_id=tmdb_id,
                title=title,
                media_type=media_type,
                weight=0.5 ** (recency_days / RECENCY_HALF_LIFE_DAYS),
                watch_count=watch_count,
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
    # Keyed on (tmdb_id, media_type) — the identity every other module uses — not object identity
    # (`id(s)`): correct either way today since `by_title` already yields one Seed per title, but a
    # stable key survives a future refactor that rebuilds equivalent Seeds rather than reusing them.
    reserved = {(s.tmdb_id, s.media_type) for s in movies[:per_type]} | {
        (s.tmdb_id, s.media_type) for s in shows[:per_type]
    }
    # Reserved seeds first, then the rest — but weight order is preserved WITHIN each group (both lists
    # are already weight-sorted), so a balanced watcher's ordering is unchanged; only a lopsided one's
    # minority-media seeds get promoted above the cutoff.
    ordered = [s for s in seeds if (s.tmdb_id, s.media_type) in reserved] + [
        s for s in seeds if (s.tmdb_id, s.media_type) not in reserved
    ]
    return ordered[:max_seeds]
