"""Watch-history source: each user's complete watched set, read from the PMS AS them.

``ShareTokenWatchSource`` is the one source. plex.tv mints a per-user server token for every shared
invite; passed to the PMS it reads the library with that user's own ``viewCount``/``viewedLeafCount``
— so a mark-as-watched (which the playback-history API never returns, issue #12) is seen, and no PMS
database mount is needed. It supersedes the old Tautulli / Plex-history-API sources, which saw only
playback sessions and capped at ~200 rows.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from loguru import logger

from shortlist.engine.clients.plex_pms import PlexClient, SectionNotShared
from shortlist.engine.clients.plextv import PlexTvClient
from shortlist.engine.models import MediaType, Seed, UserProfile, UserType, WatchedItem, is_human_rating

# Seed weight halves every ~6 weeks since the watch: a title seen today weighs ~2x one from 6 weeks
# ago, ~4x one from 3 months ago. Long enough that a season finished last month still seeds strongly,
# short enough that a years-old rewatch fades to near-zero.
RECENCY_HALF_LIFE_DAYS = 45.0


class NoWatchToken(RuntimeError):
    """No server token could be obtained to read a user's watched state with.

    Distinct from "they have watched nothing", and the distinction is load-bearing: the watched-title
    cache DELETES what a read does not return, so a token failure reported as an empty set wipes the
    very history it was meant to refresh.
    """


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

        That includes a missing token, which used to return `[]` here in flat contradiction of the
        line above. The cache treats what a read returns as the truth for the window it covers and
        deletes the rest, so "plex.tv would not mint a token just now" arriving as "they have watched
        nothing" wiped a person's cached history and stamped the sync a success.

        Returns:
            A `WatchedRead` — the titles PLUS whether the read provably covered its window. The
            cache deletes cached titles the read did not return, so it needs to know the difference
            between "not watched any more" and "we did not read that far".

        Raises:
            NoWatchToken: No server token could be obtained for this user.
        """
        token = self._token_for(user)
        if token is None:
            raise NoWatchToken(f"no server token for {user.username}")
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
                items.extend(self._plex.watched_titles(section.key, media_type, token, since=since).items)
            except SectionNotShared:
                # Not shared with them, so "nothing watched there" is the right answer, not a
                # degraded one. DEBUG, not WARNING: `sections()` is the OWNER's library list, so
                # every library a person isn't given is expected to land here on every single read.
                logger.debug("{}: section {} not shared — skipped", user.username, section.key)
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


def _cycle_window(seeds: list[Seed], window: int, offset: int) -> list[Seed]:
    """Move one of the ``window`` most recent seeds to the front, advancing one step per offset.

    A CYCLE rather than a random pick, deliberately. Random repeats: choosing from three watches at
    random gives the same one two nights running about a third of the time, and a row that names its
    seed then looks exactly as stuck as the bug this feature exists to relieve (issue #57 — a person
    watching plenty of new things still saw "Because you watched Little Brother" for weeks).

    The step is taken over the window ordered by TMDB ID, not by recency. Ordering by recency looks
    like the obvious choice and is wrong: that list's head shifts by one every time the person watches
    something new, and the offset advances by one a night, so for anyone watching nightly the two
    cancel exactly — the same seed leads for `window` nights running and the watches that entered in
    between never lead at all. Measured before this was keyed on identity: at window 3, a person
    finishing a film a night saw C, C, C, F, F, F while their newest watch went D, E, F, G, H. That is
    a WORSE row than not cycling, handed to the heaviest watchers, who are the ones most likely to
    turn it on. An ID order is arbitrary but stable, so the cancellation cannot arise: when their
    window is unchanged it cycles cleanly through every member, and when it churns the churn itself is
    the variety.

    The rest of the window is kept BEHIND the chosen seed in weight order rather than dropped, so a
    row whose budget is wider than one still fills from the same recent watches it always did.
    """
    w = min(window, len(seeds))
    if w <= 1:
        return seeds
    chosen = sorted(seeds[:w], key=lambda s: s.tmdb_id)[offset % w]
    i = next(n for n, s in enumerate(seeds) if s.tmdb_id == chosen.tmdb_id)
    return [seeds[i], *seeds[:i], *seeds[i + 1 :]]


#: Below this share of a person's ratings being whole numbers, none of their ratings are believed.
#:
#: The per-value check (`WatchedItem.is_human_rating`) rejects a fractional rating outright, but a
#: tool writing thousands of scores lands some of them on whole numbers by chance — the owner's
#: server had 1,455 tool-written ratings of which ~9% were whole, and those look exactly like an
#: opinion. So a SECOND, account-level check: if most of what an account carries could not have been
#: typed by a person, the whole account is treated as tool-managed and none of it is used.
#:
#: 0.8 leaves room for a real rater whose library also carries a few stray tool-written values,
#: while the case this exists to catch sits at 0.093 — two orders of margin, not a fine line.
_HUMAN_RATING_FLOOR = 0.8

#: Below this many ratings, the account-level check above abstains and per-value screening stands
#: alone. A single fractional rating is 0% whole, which would otherwise condemn an account for one
#: stray value — and real raters are sparse (14 of 49 people on a live server had rated anything at
#: all, a median of 2 titles each), so the common case must not be the failing one.
_MIN_RATINGS_TO_JUDGE = 5


def ratings_are_trustworthy(ratings: Iterable[float]) -> bool:
    """Whether a person's Plex ratings look like OPINIONS rather than a tool's output.

    Takes the ratings themselves rather than the items carrying them, so the server can ask the
    question straight from a `SELECT user_rating` without rebuilding history — the page and the run
    must answer it identically, and the surest way to guarantee that is one function over one input.

    See `_HUMAN_RATING_FLOOR`. An account with no ratings is trivially trustworthy: there is nothing
    to disbelieve, and the seed filter is a no-op for them either way.

    Args:
        ratings: Every 0..10 rating this person has, in any order. Nones are ignored.
    """
    rated = [r for r in ratings if r is not None]
    if len(rated) < _MIN_RATINGS_TO_JUDGE:
        return True
    return sum(1 for r in rated if is_human_rating(r)) / len(rated) >= _HUMAN_RATING_FLOOR


def disliked_seed_keys(history: list[WatchedItem], threshold: float | None) -> set[tuple[int, MediaType]]:
    """Titles this person rated at or below `threshold` in Plex — the ones to stop seeding from.

    Returns empty for `threshold=None` (the feature off), for an account whose ratings failed
    `ratings_are_trustworthy`, and for the ~70% of people who have never rated anything. Only a
    rating that could have come from a person counts: see `is_human_rating`.

    Keyed on ``(tmdb_id, media_type)``, never the id alone. TMDB numbers movies and shows in separate
    namespaces, so 1399 is both a film and Game of Thrones — and a bare-id exclusion built from one
    person's movie rating silently deleted the identically-numbered SHOW from their seeds, a title
    they had never rated and could not have. The same mismatch is called out on
    `db/models.py`'s PickRow, and this is the auto-populated version of it: nobody typed the id, so
    nobody would ever look at the row and spot it.

    IMPORTANT — call this over the person's WHOLE history, once, not over a row's slice. Trust is an
    account-level judgement (`ratings_are_trustworthy` abstains below `_MIN_RATINGS_TO_JUDGE`), so
    computing it per row lets a media- or library-scoped row see too few ratings to judge, abstain,
    and act on values the full-history verdict rejects. That is how a Kometa-managed movie library
    and a hand-rated TV library end up disagreeing with each other about the same person.

    Args:
        history: The person's COMPLETE watched titles, carrying their own `user_rating`.
        threshold: 0..10 rating at or below which a title stops seeding, or None to ignore ratings.

    Returns:
        ``(tmdb_id, media_type)`` pairs to exclude from seeding. Excluded from SEEDING only — a title
        rated low is still watched, and must stay in history so the already-watched rules keep
        working on it.
    """
    if threshold is None or not ratings_are_trustworthy(item.user_rating for item in history):
        return set()
    return {
        (item.tmdb_id, item.media_type)
        for item in history
        if item.tmdb_id is not None and item.is_human_rating and item.user_rating <= threshold
    }


@dataclass(frozen=True)
class RatingsPolicy:
    """What this person's Plex ratings did to their seeds on one run — and, when nothing, why.

    Exists because "nothing was dropped" has three causes that are indistinguishable from the outcome
    alone: the setting is off, they rated nothing low, or their ratings are tool-written and were
    disbelieved wholesale. The third is a silent no-op that looks exactly like a healthy run, so the
    trace has to be able to say which one happened.
    """

    threshold: float | None  # rating at or below which a title stops seeding; None = feature off
    trusted: bool  # False = tool-written ratings (`ratings_are_trustworthy`), so none of them counted
    blocked: set[tuple[int, MediaType]] = field(default_factory=set)  # what actually stopped seeding
    rated: int = 0  # how many of their watches carry any rating at all — "on, but they've rated nothing"
    # ...of which these could have been typed by a person. The two differ on an account only PARTLY
    # written by a tool: `ratings_are_trustworthy` tolerates a fifth of them being fractional, so the
    # account stays trusted while `is_human_rating` still skips each fractional value one by one.
    # Counting off `rated` there would report "none of their 10 ratings are 1 star or lower" over a
    # set containing an uncounted 0.75 — the same silent no-op this whole summary exists to expose.
    rated_human: int = 0

    @property
    def enabled(self) -> bool:
        return self.threshold is not None


def ratings_policy(history: list[WatchedItem], threshold: float | None) -> RatingsPolicy:
    """Decide, ONCE over the whole history, what ratings do to this person's seeds.

    One call site per user, for the reason spelled out on `disliked_seed_keys`: trust is an
    account-level judgement, so anything deriving it from a slice can reach a different verdict than
    the run did. The run filters with `blocked` and the trace explains itself from the same object,
    which is what stops the explanation and the behaviour from drifting apart.

    Args:
        history: The person's COMPLETE watched titles, carrying their own `user_rating`.
        threshold: 0..10 rating at or below which a title stops seeding, or None when the feature is off.
    """
    return RatingsPolicy(
        threshold=threshold,
        trusted=ratings_are_trustworthy(item.user_rating for item in history),
        blocked=disliked_seed_keys(history, threshold),
        rated=sum(1 for item in history if item.user_rating is not None),
        rated_human=sum(1 for item in history if item.is_human_rating),
    )


def derive_seeds(
    history: list[WatchedItem],
    resolve_tmdb_id,
    *,
    max_seeds: int = 30,
    blocked: set[int] | None = None,
    window: int = 1,
    cycle_offset: int = 0,
    disliked: set[tuple[int, MediaType]] | None = None,
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
        window: How many of the most recent watches this row may be built from, of which
            ``cycle_offset`` selects one per media type. 1 (the default) always takes the most recent,
            which is every caller's behaviour before seed cycling existed.
        cycle_offset: Which of the window to lead with — the run's day plus a stable per-(row, user)
            phase, so one person's row advances a step a day and two people's rows advance out of step.
        disliked: ``(tmdb_id, media_type)`` pairs this person rated low, from `disliked_seed_keys`
            over their WHOLE history. Taken pre-computed rather than derived here from a threshold,
            because this function is handed a row's SLICE of history — deriving it here judged each
            row's slice separately, and a slice too small to judge abstains and acts on ratings the
            full-history verdict rejects. Callers building a SHARED row pass nothing: one person's
            rating must not reshape a row everyone sees (see `EngineConfig.dislike_threshold`).
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
        # Checked HERE rather than unioned into `blocked`, because only here is the media type in
        # hand. `blocked` is owner-typed and id-only; a rating exclusion is auto-derived and must
        # carry the type, or a disliked movie deletes the show sharing its TMDB number.
        if disliked and (tmdb_id, media_type) in disliked:
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
    if window > 1:
        # Cycle within each media type, not across the flat list. A `media=both` row seeds one movie
        # and one show, so rotating the flat list would spend the whole window on whichever type this
        # person watches more of and leave the other half pinned to its newest title for ever.
        movies = _cycle_window(movies, window, cycle_offset)
        shows = _cycle_window(shows, window, cycle_offset)
        # Rebuild the flat list to LEAD with what the cycle chose. The balancing below re-derives
        # `ordered` by filtering this list, so leaving it in pure weight order would quietly undo the
        # rotation and hand back the newest watch again.
        # Weight order, NOT (movies, shows) order. `max_seeds=1` keeps only the first of these, so
        # listing movies unconditionally first handed every one-seed movies-and-TV row to a film —
        # a TV watcher who turned cycling on had their row start naming a film from a month ago.
        leads = sorted((group[0] for group in (movies, shows) if group), key=lambda s: s.weight, reverse=True)
        lead_keys = {(s.tmdb_id, s.media_type) for s in leads}
        seeds = leads + [s for s in seeds if (s.tmdb_id, s.media_type) not in lead_keys]
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
