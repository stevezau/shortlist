"""A person's viewing history shaped for the AI's pick prompt (#152).

``taste_summary`` lists the 20 newest titles and nothing else, so someone's decade of favourites never
reaches the model and a title they hated reads the same as one they loved. A `TasteProfile` keeps the
newest watches, adds what they come back to (rewatched, finished, watched at length) and, where their
Plex ratings can be believed, what they rated high and low.

Pure: takes history, returns data and text. Where the text goes is the curator's business.
"""

from collections.abc import Callable, Set
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from shortlist.engine.history import RatingsPolicy, distinct_recent
from shortlist.engine.models import MediaType, Seed, WatchedItem

_HIGH_RATING = 8.0  # 4 stars of 5
_FAVOURITE_PLAYS = 2  # a movie played this often counts as a favourite
_FAVOURITE_EPISODES = 20  # a show watched this deep counts as a favourite even if unfinished
_DROPPED_BELOW_EPISODES = 3  # a show left unfinished before this many episodes was tried and dropped
_RENDERED_FAVOURITES = 6  # the fewest favourites a wide profile lists, whatever number are searched
_WEEK_S = 7 * 86400
_TITLE_MAX = 80

_INTRO = (
    "What this person has watched. Watching a title does not mean they liked it; their own ratings, where given, do."
)
_NO_HISTORY = "- (no history yet — recommend broadly popular titles)"


class TastePrompt(NamedTuple):
    """The history text a web prompt carries, and what kind it is.

    ``wide`` is a full `TasteProfile`, which the built-in guidance then calls "this person's viewing
    history"; otherwise it is the plain list of recent titles the guidance has always described.
    """

    text: str
    wide: bool


@dataclass(frozen=True)
class TasteProfile:
    """What the pick prompt says about one person. Every list holds ONE entry per (title, media type)."""

    recent: list[WatchedItem] = field(default_factory=list)  # newest first
    favourites: list[WatchedItem] = field(default_factory=list)  # strongest first, movies and shows interleaved
    rated_high: list[WatchedItem] = field(default_factory=list)  # trusted human rating >= 4 stars
    rated_low: list[WatchedItem] = field(default_factory=list)  # trusted human rating at or below the dislike bar
    older: list[WatchedItem] = field(default_factory=list)  # an even sample of the rest of their history, newest first

    def search_candidates(self) -> list[WatchedItem]:
        """Titles worth a web search of their own: loved ones first, then the long-time favourites."""
        seen: set[tuple[str, MediaType]] = set()
        out: list[WatchedItem] = []
        for item in [*self.rated_high, *self.favourites]:
            key = (item.title, item.media_type)
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    def older_candidates(self) -> list[WatchedItem]:
        """Titles worth a web search of their own from further back in their history."""
        return list(self.older)

    def render(self) -> str:
        """The taste text a pick prompt carries. Empty sections are left out."""
        sections: list[str] = []
        if self.recent:
            sections.append(_section("Watched recently (newest first):", [_watched_line(i) for i in self.recent]))
        if self.favourites:
            sections.append(
                _section(
                    "Long-time favourites (rewatched, finished, or watched at length):",
                    [_watched_line(i) for i in self.favourites],
                )
            )
        if self.older:
            sections.append(_section("Also watched over the years (a sample):", [_watched_line(i) for i in self.older]))
        rated = [f"- Rated highly: {_rated_label(i)}" for i in self.rated_high]
        rated += [f"- Rated low: {_rated_label(i)}" for i in self.rated_low]
        if rated:
            sections.append(_section("Their own Plex ratings:", rated))
        if not sections:
            sections.append(_NO_HISTORY)
        return "\n\n".join([_INTRO, *sections])


def build_taste(
    history: list[WatchedItem],
    *,
    blocked: set[int],
    ratings: RatingsPolicy | None,
    recent_limit: int = 12,
    favourite_limit: int = 12,
    rated_limit: int = 6,
    older_limit: int = 0,
    now: datetime | None = None,
    lookback_years: int = 0,
    searched: Set[tuple[int, MediaType]] = frozenset(),
) -> TasteProfile:
    """Shape ``history`` into a `TasteProfile`.

    Args:
        history: The person's watched titles; a title held in two libraries is merged into one.
        blocked: TMDB ids of titles they asked never to seed from ("Don't seed"). Dropped everywhere.
        ratings: Their ratings policy over the WHOLE history. Ratings count only when it is enabled and
            trusted (tool-written ratings are not opinions), and then only whole-number ones.
        recent_limit: Most titles under "recently".
        favourite_limit: Most titles under "favourites".
        rated_limit: Most titles across the two rated lists; low-rated ones get the room first.
        older_limit: Most titles under "older watches", sampled evenly across their history and rotating weekly.
        now: The clock the sample rotates by and the look-back counts from; the current time when omitted.
        lookback_years: Older watches must be newer than this many years; 0 means any time.
        searched: (TMDB id, media) of titles the row already searches as its recent group. Never a favourite
            or an older watch: a row's seeds are not always the newest titles, and a repeat wastes a slot.
    """
    titles = [t for t in _merge_titles(history) if t.tmdb_id is None or t.tmdb_id not in blocked]
    counts_ratings = _counts_ratings(ratings)
    low: list[WatchedItem] = []
    high: list[WatchedItem] = []
    rest: list[WatchedItem] = []
    for item in titles:
        if counts_ratings and item.is_human_rating and item.user_rating <= ratings.threshold:
            low.append(item)
        elif counts_ratings and item.is_human_rating and item.user_rating >= _HIGH_RATING:
            high.append(item)
        else:
            rest.append(item)
    low.sort(key=lambda i: i.watched_at, reverse=True)
    high.sort(key=lambda i: (i.user_rating, i.watched_at), reverse=True)
    low_n = min(len(low), max(rated_limit - len(high), rated_limit // 2))
    high_n = min(len(high), rated_limit - low_n)

    rest.sort(key=lambda i: i.watched_at, reverse=True)
    recent = rest[:recent_limit]
    after_recent = [i for i in rest[len(recent) :] if (i.tmdb_id, i.media_type) not in searched]
    all_favourites = _favourites(after_recent)
    older: list[WatchedItem] = []
    if older_limit > 0:
        now = now or datetime.now(UTC)
        taken = {id(i) for i in all_favourites}
        pool = [i for i in after_recent if id(i) not in taken and not _dropped_show(i)]
        if lookback_years > 0:
            cutoff = now - timedelta(days=lookback_years * 365)
            pool = [i for i in pool if _aware(i.watched_at) >= cutoff]
        older = _spread_sample(pool, older_limit, int(now.timestamp() // _WEEK_S))
    return TasteProfile(
        recent=recent,
        favourites=all_favourites[:favourite_limit],
        older=older,
        rated_high=high[:high_n],
        rated_low=low[:low_n],
    )


def _counts_ratings(ratings: RatingsPolicy | None) -> bool:
    """Whether this person's Plex ratings are opinions we can act on (given, switched on, and not tool-written)."""
    return ratings is not None and ratings.enabled and ratings.trusted


def recent_list_text(items: list[WatchedItem]) -> str:
    """The recent-titles list a prompt carries; `curator.base.taste_summary` shares it so the two can't drift."""
    lines = [f"- {w.title}" + (f" ({w.year})" if w.year else "") for w in items]
    return "Recently watched (most recent first):\n" + "\n".join(lines)


def recent_taste(
    history: list[WatchedItem], *, blocked: set[int], ratings: RatingsPolicy | None, limit: int = 20
) -> TastePrompt:
    """The newest distinct titles, as `taste_summary` lists them, minus what this person has ruled out.

    Left out: titles on their "Don't seed" list, and (where their ratings can be believed) titles they
    rated at or below the dislike threshold. A web prompt that named them would invite more of the same.
    A dislike covers every library's copy of the title, as it does for seeds (`ratings.blocked`): Plex rates
    each copy separately, so an unrated 4K copy must not bring back a title the HD copy was rated 1 star.
    """
    counts = _counts_ratings(ratings)
    kept = [
        item
        for item in history
        if (item.tmdb_id is None or item.tmdb_id not in blocked)
        and not (counts and item.is_human_rating and item.user_rating <= ratings.threshold)
        and not (counts and (item.tmdb_id, item.media_type) in ratings.blocked)
    ]
    return TastePrompt(recent_list_text(distinct_recent(kept, limit)), False)


class HistoryMix(NamedTuple):
    """What a row's AI web search is told of a person's history, and the titles it searches beyond the recent."""

    taste: TastePrompt | None
    favourite_seeds: list[Seed]
    older_seeds: list[Seed]


def history_mix(
    history: list[WatchedItem],
    *,
    blocked: set[int],
    ratings: RatingsPolicy | None,
    resolve: Callable[[WatchedItem], int | None],
    favourites: int,
    older: int,
    now: datetime | None = None,
    lookback_years: int = 0,
    searched: Set[tuple[int, MediaType]] = frozenset(),
) -> HistoryMix:
    """The rendered taste text for the pick prompt, and the favourite and older titles to search for.

    The one place a row (`rows.RowPolicy.taste_for`), the history-mix endpoint and the replay all turn a
    history into the wide profile. A row builds it from its own libraries and media; the replay from the
    whole history. A title that resolves to no TMDB id is left out of the seeds.

    "Don't seed" holds TMDB ids, and a watch on a legacy agent carries none until it is resolved, so a
    title the profile picked is resolved and, if blocked, taken out and the profile rebuilt, until none is.
    """
    hidden: set[tuple[str, MediaType]] = set()
    while True:
        kept = [i for i in history if (i.title, i.media_type) not in hidden]
        profile = build_taste(
            kept,
            blocked=blocked,
            ratings=ratings,
            favourite_limit=max(favourites, _RENDERED_FAVOURITES),
            older_limit=older,
            now=now,
            lookback_years=lookback_years,
            searched=searched,
        )
        named = [*profile.recent, *profile.favourites, *profile.older, *profile.rated_high, *profile.rated_low]
        newly = {(i.title, i.media_type) for i in named if i.tmdb_id is None and resolve(i) in blocked}
        if not newly:
            break
        hidden |= newly
    return HistoryMix(
        TastePrompt(profile.render(), True),
        _seeds(profile.search_candidates(), resolve),
        _seeds(profile.older_candidates(), resolve),
    )


def _seeds(items: list[WatchedItem], resolve: Callable[[WatchedItem], int | None]) -> list[Seed]:
    seeds: list[Seed] = []
    for item in items:
        tmdb_id = item.tmdb_id if item.tmdb_id is not None else resolve(item)
        if tmdb_id is not None:
            seeds.append(
                Seed(tmdb_id=tmdb_id, title=item.title, media_type=item.media_type, watch_count=item.watch_count)
            )
    return seeds


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _dropped_show(item: WatchedItem) -> bool:
    """A show they stopped watching early: not finished, and fewer than three episodes."""
    return (
        item.media_type is MediaType.SHOW and not item.is_finished and _episodes_watched(item) < _DROPPED_BELOW_EPISODES
    )


def _spread_sample(items: list[WatchedItem], n: int, week: int) -> list[WatchedItem]:
    """``n`` titles spread evenly across ``items``' history, a different pick from each stretch each week.

    Oldest to newest, the list is cut into ``n`` stretches of near-equal size and one title is taken from
    each, so the sample covers the whole span rather than the latest corner of it. The title taken from a
    stretch moves along by one every week, so over a few weeks a long history is covered rather than
    sampled by the same ten titles forever. Returned newest first.
    """
    if len(items) <= n:
        return sorted(items, key=lambda i: i.watched_at, reverse=True)
    ordered = sorted(items, key=lambda i: i.watched_at)
    picked: list[WatchedItem] = []
    for index in range(n):
        chunk = ordered[index * len(ordered) // n : (index + 1) * len(ordered) // n]
        if chunk:
            picked.append(chunk[(week + index) % len(chunk)])
    return sorted(picked, key=lambda i: i.watched_at, reverse=True)


def _merge_titles(history: list[WatchedItem]) -> list[WatchedItem]:
    """One item per (title, media type), newest watch first.

    The newest copy wins the date; a movie's plays add up across libraries; a show keeps the copy
    that got furthest, since each library holds its own progress.
    """
    groups: dict[tuple[str, MediaType], list[WatchedItem]] = {}
    for item in sorted(history, key=lambda w: w.watched_at, reverse=True):
        groups.setdefault((item.title, item.media_type), []).append(item)
    return [_merge_group(copies) for copies in groups.values()]


def _merge_group(copies: list[WatchedItem]) -> WatchedItem:
    newest = copies[0]
    fields: dict = {
        "tmdb_id": next((c.tmdb_id for c in copies if c.tmdb_id is not None), None),
        "year": next((c.year for c in copies if c.year is not None), None),
        "user_rating": next((c.user_rating for c in copies if c.user_rating is not None), None),
    }
    if newest.media_type is MediaType.MOVIE:
        fields["watch_count"] = sum(c.watch_count for c in copies)
    else:
        furthest = max(copies, key=lambda c: (c.viewed_leaf_count or 0, c.watch_count))
        fields["watch_count"] = furthest.watch_count
        fields["viewed_leaf_count"] = furthest.viewed_leaf_count
        fields["leaf_count"] = furthest.leaf_count
    return replace(newest, **fields)


def _episodes_watched(show: WatchedItem) -> int:
    return show.viewed_leaf_count if show.viewed_leaf_count is not None else show.watch_count


def _favourites(items: list[WatchedItem]) -> list[WatchedItem]:
    """Titles they come back to, strongest first, movies and shows taking turns so neither scale dominates."""
    movies = [i for i in items if i.media_type is MediaType.MOVIE and i.watch_count >= _FAVOURITE_PLAYS]
    shows = [
        i
        for i in items
        if i.media_type is MediaType.SHOW and (i.is_finished or _episodes_watched(i) >= _FAVOURITE_EPISODES)
    ]
    movies.sort(key=lambda i: (i.watch_count, i.watched_at), reverse=True)
    shows.sort(key=lambda i: (i.is_finished, _episodes_watched(i), i.watched_at), reverse=True)
    out: list[WatchedItem] = []
    for position in range(max(len(movies), len(shows))):
        out.extend(group[position] for group in (movies, shows) if position < len(group))
    return out


def _section(heading: str, lines: list[str]) -> str:
    return "\n".join([heading, *lines])


def _name(item: WatchedItem) -> str:
    title = item.title[:_TITLE_MAX]
    return f"{title} ({item.year})" if item.year else title


def _watched_line(item: WatchedItem) -> str:
    return f"- {_name(item)} - {_detail(item)}"


def _detail(item: WatchedItem) -> str:
    if item.media_type is MediaType.MOVIE:
        if item.watch_count == 2:
            return "film, watched twice"
        if item.watch_count > 2:
            return f"film, watched {item.watch_count} times"
        return "film"
    if item.is_finished:
        return "show, finished"
    seen = _episodes_watched(item)
    noun = "episode" if seen == 1 else "episodes"
    if item.leaf_count:
        return f"show, {seen} of {item.leaf_count} episodes so far"
    return f"show, {seen} {noun}"


def _rated_label(item: WatchedItem) -> str:
    stars = item.user_rating / 2
    return f"{_name(item)}, {stars:g} {'star' if stars == 1 else 'stars'}"
