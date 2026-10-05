"""Seasonal rows (discussion #124): the season catalogue and which season a row is in on a given day.

A seasonal row follows the calendar — Halloween films in October, Christmas films in December — and is
hidden between seasons. Everything here is pure date arithmetic; the server resolves it against its own
clock when it builds a row's spec, exactly as it resolves a day schedule (`rows.row_is_shown`), so the
engine never reads a clock.

A season's WINDOW runs from ``lead_days`` before its day to ``after_days`` after it. The row is SHOWN
inside a window, and BUILT inside it or on the night before it opens (`build_on`): that pre-build night
fills the row while it is still hidden, so the midnight flip that shows it puts a fresh row on screen
rather than last season's.

The CATALOGUE is every season a row may follow: the built-ins below, then the owner's own (issue #137).
Every function that reads it takes it as an argument with no default — the server builds it once per
request, job or run — because a silent default of the built-ins would hide every row that follows a
custom season.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import TYPE_CHECKING, Literal, Protocol

from loguru import logger

from shortlist.engine.clients.tmdb import DISCOVER_MIN_VOTES
from shortlist.engine.models import MediaType, RowSeason

if TYPE_CHECKING:
    from shortlist.engine.clients.plex_pms import LibraryTitle

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_ORDINALS = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}
#: Any year sorts a catalogue into calendar order; a fixed one keeps `normalise_slugs` free of a clock.
_ORDER_YEAR = 2026
MAX_EASTER_OFFSET = 63


def easter_sunday(year: int) -> date:
    """Western Easter Sunday by the anonymous Gregorian algorithm (Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@dataclass(frozen=True)
class DateRule:
    """When a season falls in a given year (issue #137): a fixed day, the nth (or last) weekday of a
    month, or a number of days from Easter. ``weekday`` is Python's: Monday is 0."""

    kind: Literal["fixed", "nth", "easter"]
    month: int = 1
    day: int = 1
    nth: int = 1  # 1-4, or -1 for the last one
    weekday: int = 0
    offset: int = 0

    def anchor(self, year: int) -> date:
        """The season's day in ``year``."""
        if self.kind == "fixed":
            return date(year, self.month, self.day)
        if self.kind == "nth":
            if self.nth == -1:
                last = date(year, self.month + 1, 1) - timedelta(days=1) if self.month < 12 else date(year, 12, 31)
                return last - timedelta(days=(last.weekday() - self.weekday) % 7)
            first = date(year, self.month, 1)
            return first + timedelta(days=(self.weekday - first.weekday()) % 7 + 7 * (self.nth - 1))
        return easter_sunday(year) + timedelta(days=self.offset)

    def normalised(self) -> DateRule:
        """This rule with the fields its kind ignores at their defaults, so two rules that name the same days
        are equal: a fixed rule ignores nth, weekday and offset; an nth rule day and offset; an Easter rule
        everything but its offset."""
        if self.kind == "fixed":
            return replace(self, nth=1, weekday=0, offset=0)
        if self.kind == "nth":
            return replace(self, day=1, offset=0)
        if self.kind == "easter":
            return replace(self, month=1, day=1, nth=1, weekday=0)
        return self

    def label(self) -> str:
        """The rule in plain English: "17 March", "4th Thursday of November", "21 days before Easter"."""
        if self.kind == "fixed":
            return f"{self.day} {_MONTHS[self.month - 1]}"
        if self.kind == "nth":
            which = "Last" if self.nth == -1 else _ORDINALS[self.nth]
            return f"{which} {_WEEKDAYS[self.weekday]} of {_MONTHS[self.month - 1]}"
        if self.offset == 0:
            return "Easter Sunday"
        days = abs(self.offset)
        return f"{days} day{'s' if days != 1 else ''} {'before' if self.offset < 0 else 'after'} Easter"

    def validate(self) -> None:
        """Refuse a rule that cannot name a day every year.

        Raises:
            ValueError: worded for the owner, saying what to pick instead.
        """
        if self.kind not in ("fixed", "nth", "easter"):
            raise ValueError("That's an unknown kind of date.")
        if self.kind in ("fixed", "nth") and not 1 <= self.month <= 12:
            raise ValueError("Pick a month.")
        if self.kind == "fixed":
            if (self.month, self.day) == (2, 29):
                raise ValueError("29 February isn't every year — pick 28 February or 1 March.")
            # 2025 is not a leap year, so February counts 28: the 29th was refused above.
            days = (date(2025, self.month + 1, 1) - timedelta(days=1)).day if self.month < 12 else 31
            if not 1 <= self.day <= days:
                raise ValueError(f"{_MONTHS[self.month - 1]} has {days} days.")
        if self.kind == "nth":
            if self.nth not in (1, 2, 3, 4, -1):
                raise ValueError("Pick the 1st to 4th, or last, weekday of the month.")
            if not 0 <= self.weekday <= 6:
                raise ValueError("Pick a weekday.")
        if self.kind == "easter" and abs(self.offset) > MAX_EASTER_OFFSET:
            raise ValueError(f"Keep it within {MAX_EASTER_OFFSET} days of Easter.")


@dataclass(frozen=True)
class CollectionRef:
    """A Plex collection a season reads its films from, by library and TITLE — never ratingKey: Kometa
    deletes its seasonal collections out of season and recreates them under a new key (D5)."""

    section_key: str
    title: str


@dataclass(frozen=True)
class Season:
    """One entry in the catalogue: when it is, and which TMDB titles belong to it.

    ``keywords`` are TMDB keyword ids, OR'd together, for films and shows alike. ``movie_genres`` widens
    the FILM list with whole genres; TMDB's TV genre list has neither Horror nor Romance, so shows are
    keywords only.
    """

    slug: str
    name: str
    emoji: str
    rule: DateRule
    description: str
    keywords: tuple[int, ...] = ()
    movie_genres: tuple[int, ...] = ()
    #: Films a KEYWORD found are dropped when they carry one of these genres and none of ``movie_genres``.
    #: TMDB tags any film with a Halloween scene, so a rom-com or a drama set on the night reads as noise
    #: in a spooky row.
    keyword_excluded_genres: tuple[int, ...] = ()
    collections: tuple[CollectionRef, ...] = ()
    picks: tuple[tuple[int, MediaType], ...] = ()
    #: A custom season's own timing (D8). None = the row's, which is what every built-in uses.
    lead_days: int | None = None
    after_days: int | None = None
    builtin: bool = False
    #: Changes when the season's SOURCES change, so its rows rebuild (D11). Empty for built-ins.
    content_hash: str = ""


Catalogue = Mapping[str, Season]


# Keyword ids measured on TMDB 2026-09-15 (`/search/keyword`, then `/discover` against a real library).
# A wrong id is silent: it matches real, unrelated titles. The related keywords were chosen from what the
# library's untagged seasonal films actually carried ("christmas romance", "christmas spirit"...).
#
# The genres are there because keywords alone tag 63 Halloween and 20 Valentine's films on a 10k-film
# library, too few for rows that differ person to person; with Horror / Romance it is 670 and ~960.
BUILTIN_SEASONS: dict[str, Season] = {
    season.slug: season
    for season in (
        Season(
            slug="valentines",
            name="Valentine's Day",
            emoji="💘",
            rule=DateRule("fixed", month=2, day=14),
            description="Valentine's films and romance",
            keywords=(160404, 376604),  # valentine's day, happy valentine's day
            movie_genres=(10749,),  # Romance
            builtin=True,
        ),
        Season(
            slug="halloween",
            name="Halloween",
            emoji="🎃",
            rule=DateRule("fixed", month=10, day=31),
            description="Halloween films and horror",
            # halloween, trick or treating, halloween night, halloween party, halloween costume
            keywords=(3335, 180193, 232795, 9694, 182794),
            movie_genres=(27,),  # Horror
            # Romance, Drama. Measured on a real library: of 23 non-horror Halloween keyword films, these
            # two genres cut exactly the misses — When We First Met, War Pony, Ed Wood, Brick, In America
            # (and, arguably, Donnie Darko) — and keep Hocus Pocus, Casper, Beetlejuice and E.T.
            keyword_excluded_genres=(10749, 18),
            builtin=True,
        ),
        Season(
            slug="christmas",
            name="Christmas",
            emoji="🎄",
            rule=DateRule("fixed", month=12, day=25),
            description="Christmas films",
            # christmas, christmas romance, christmas spirit, christmas special, christmas music, christmas
            # tree, christmas story, santa claus, christmas eve
            keywords=(207317, 272698, 193048, 255088, 186933, 5570, 196450, 1991, 260365),
            builtin=True,
        ),
    )
}

#: How early a seasonal row may start showing, and how long it may stay up afterwards, in days.
MAX_LEAD_DAYS = 90
MAX_AFTER_DAYS = 30


@dataclass(frozen=True)
class SeasonWindow:
    """One year's run of a season: its day (``anchor``) and the first and last days the row shows it."""

    season: Season
    anchor: date
    starts: date
    ends: date


def normalise_slugs(slugs: list[str], *, catalogue: Catalogue) -> list[str]:
    """De-duplicated, in calendar order. Raises ValueError naming anything the catalogue does not have."""
    unknown = sorted({slug for slug in slugs if slug not in catalogue})
    if unknown:
        raise ValueError(f"unknown season(s) {unknown} — choose from {sorted(catalogue)}")
    wanted = set(slugs)
    return sorted(
        (slug for slug in catalogue if slug in wanted),
        key=lambda slug: (catalogue[slug].rule.anchor(_ORDER_YEAR), slug),
    )


def _windows(
    slugs: list[str], lead_days: int, after_days: int, around: date, catalogue: Catalogue
) -> list[SeasonWindow]:
    """Every window of these seasons anchored in the year before, of, and after ``around``.

    Three years because a window can cross New Year either way: Valentine's with a long lead opens in
    December, and Christmas with days after closes in January. Unknown slugs are skipped rather than
    raised: a season retired from the catalogue must leave an old row hidden, not crash every run.

    ``lead_days`` and ``after_days`` are the row's, and apply to a season that has no timing of its own —
    every built-in. A custom season carries its own (#137 D8): short holidays sit close together, and a
    row's 30-day lead would show "St Patrick's picks" from mid-February.
    """
    windows = []
    for slug in slugs:
        season = catalogue.get(slug)
        if season is None:
            continue
        lead = lead_days if season.lead_days is None else season.lead_days
        after = after_days if season.after_days is None else season.after_days
        for year in (around.year - 1, around.year, around.year + 1):
            anchor = season.rule.anchor(year)
            windows.append(
                SeasonWindow(
                    season=season,
                    anchor=anchor,
                    starts=anchor - timedelta(days=lead),
                    ends=anchor + timedelta(days=after),
                )
            )
    return windows


def shown_on(
    slugs: list[str], lead_days: int, after_days: int, day: date, *, catalogue: Catalogue
) -> SeasonWindow | None:
    """The season a row with these settings shows on ``day``, or None between seasons.

    When windows overlap (a long lead meeting days after), a season whose day is still to come — or is
    today — beats one already past, and among those the nearest wins: Halloween lingering into November
    gives way to the Christmas that has started early, but holds its own day.
    """
    open_windows = [w for w in _windows(slugs, lead_days, after_days, day, catalogue) if w.starts <= day <= w.ends]
    if not open_windows:
        return None
    return min(open_windows, key=lambda w: (w.anchor < day, abs((w.anchor - day).days)))


def build_on(
    slugs: list[str], lead_days: int, after_days: int, day: date, *, catalogue: Catalogue
) -> SeasonWindow | None:
    """The season a row builds for on ``day``: the one it shows, else the one it shows tomorrow.

    Never tomorrow's when today already shows one — at a hand-over (Halloween today, Christmas tomorrow)
    the row keeps Halloween on its own day and the next run switches it.
    """
    return shown_on(slugs, lead_days, after_days, day, catalogue=catalogue) or shown_on(
        slugs, lead_days, after_days, day + timedelta(days=1), catalogue=catalogue
    )


def last_shown_day(slugs: list[str], lead_days: int, after_days: int, day: date, *, catalogue: Catalogue) -> date:
    """The last day the season shown on ``day`` stays on screen — its window's end, or the day before a
    following season takes the row over. ``day`` itself when nothing is shown."""
    showing = shown_on(slugs, lead_days, after_days, day, catalogue=catalogue)
    if showing is None:
        return day
    last = day
    while last < showing.ends:
        tomorrow = shown_on(slugs, lead_days, after_days, last + timedelta(days=1), catalogue=catalogue)
        if tomorrow is None or tomorrow.anchor != showing.anchor or tomorrow.season != showing.season:
            break
        last += timedelta(days=1)
    return last


def row_season_on(
    slugs: list[str], lead_days: int, after_days: int, day: date, *, catalogue: Catalogue
) -> RowSeason | None:
    """What a seasonal row's spec carries on ``day``: the season it builds for, or None when dormant."""
    window = build_on(slugs, lead_days, after_days, day, catalogue=catalogue)
    if window is None:
        return None
    return RowSeason(
        slug=window.season.slug,
        name=window.season.name,
        emoji=window.season.emoji,
        anchor=window.anchor,
        content_hash=window.season.content_hash,
        starts=window.starts,
        ends=window.ends,
    )


def next_after(
    slugs: list[str], lead_days: int, after_days: int, day: date, *, catalogue: Catalogue
) -> SeasonWindow | None:
    """The next window to OPEN after ``day`` — what an out-of-season row is waiting for."""
    upcoming = [w for w in _windows(slugs, lead_days, after_days, day, catalogue) if w.starts > day]
    return min(upcoming, key=lambda w: w.starts, default=None)


def next_anchors(season: Season, day: date, count: int = 2) -> list[date]:
    """The season's next ``count`` days on or after ``day`` — what the editor's year strip draws."""
    found: list[date] = []
    year = day.year
    while len(found) < count:
        anchor = season.rule.anchor(year)
        if anchor >= day:
            found.append(anchor)
        year += 1
    return found


#: At most this many titles a season names by id — hand picks first, then Plex collection members — are
#: read from TMDB for one season, one detail read each (#137). The largest collection measured held 1,187.
MAX_PLEX_SOURCED = 1000
#: Detail reads at once for those titles.
_DETAIL_WORKERS = 4


class _ListReader(Protocol):
    def discover_all(self, media_type: MediaType, params: dict) -> list[dict]: ...

    def list_item(self, tmdb_id: int, media_type: MediaType) -> dict | None: ...


class _CollectionReader(Protocol):
    def collection_members(self, section_key: str, title: str) -> list[LibraryTitle] | None: ...


@dataclass(frozen=True)
class SeasonTitles:
    """A season's titles for one run.

    ``ids`` is every title in the season, whether or not the server has it — what a row's pool is filtered
    to, so a missing Christmas film similar to someone's watches can still be requested. ``in_library`` is
    the subset the server's libraries hold, as TMDB list items — what the season source adds to a pool.
    Adding anything else would turn the whole list into request demand.
    """

    ids: dict[MediaType, frozenset[int]]
    in_library: dict[MediaType, list[dict]]
    #: Collections the season names that were not in their library tonight. Kometa creates its seasonal
    #: collections in season only (#137 D5), so this is a normal night, not a failure.
    missing_collections: tuple[str, ...] = ()

    def contains(self, tmdb_id: int, media_type: MediaType) -> bool:
        """Whether a title is in this season. By media type: TMDB ids are unique only within one."""
        return tmdb_id in self.ids.get(media_type, frozenset())


def season_content_hash(season: Season) -> str:
    """A short fingerprint of where a season's films come from — never its name, emoji or timing.

    A row's recipe carries it (#137 D11), so editing a source rebuilds the rows that follow the season while
    a rename only renames them. Blind to order, since the same sources may be saved in any order. 16 hex
    characters, to fit the recipe column.
    """
    sources = [
        sorted(season.keywords),
        sorted(season.movie_genres),
        sorted(season.keyword_excluded_genres),
        sorted((ref.section_key, ref.title) for ref in season.collections),
        sorted((tmdb_id, media_type.value) for tmdb_id, media_type in season.picks),
    ]
    return hashlib.blake2b(json.dumps(sources).encode(), digest_size=8).hexdigest()


def _queries(season: Season) -> list[tuple[MediaType, dict]]:
    """The season's TMDB list queries. No tag query for a season without tags: an empty ``with_keywords`` is
    no filter at all to TMDB, and would read every title it holds."""
    queries: list[tuple[MediaType, dict]] = []
    if season.keywords:
        keywords = "|".join(str(keyword) for keyword in season.keywords)
        queries += [(MediaType.MOVIE, {"with_keywords": keywords}), (MediaType.SHOW, {"with_keywords": keywords})]
    if season.movie_genres:
        genres = "|".join(str(genre) for genre in season.movie_genres)
        queries.append((MediaType.MOVIE, {"with_genres": genres, "vote_count.gte": DISCOVER_MIN_VOTES}))
    return queries


def _left_out(season: Season, media_type: MediaType, item: dict) -> bool:
    """Whether a film carries one of the season's left-out genres and none of its own. A horror drama is
    still horror, so only a film with none of the season's own genres is dropped."""
    genres = set(item.get("genre_ids") or [])
    return (
        media_type is MediaType.MOVIE
        and bool(genres & set(season.keyword_excluded_genres))
        and not genres & set(season.movie_genres)
    )


def _read_items(tmdb: _ListReader, titles: list[tuple[int, MediaType]]) -> list[dict | None]:
    """``tmdb.list_item`` for each title, ``_DETAIL_WORKERS`` at once, in order. Raises on the first failure."""
    if not titles:
        return []
    # In copies of the caller's context, as `TmdbClient.discover_all` reads its pages, so a retry warning
    # stays with the run that caused it.
    contexts = [contextvars.copy_context() for _ in titles]
    with ThreadPoolExecutor(max_workers=_DETAIL_WORKERS) as pool:
        return list(pool.map(lambda context, title: context.run(tmdb.list_item, *title), contexts, titles))


@dataclass(frozen=True)
class _SourceReads:
    """What each of a season's sources gave, before any library is consulted. Read once, so `load_titles` and
    `preview` count from the very same reads."""

    #: Tag films and shows, with the season's left-out genres already dropped.
    tagged: dict[tuple[int, MediaType], dict]
    genre: dict[tuple[int, MediaType], dict]
    #: Each collection the season names, with its members, or None when it is not in its library tonight.
    collections: tuple[tuple[CollectionRef, list[LibraryTitle] | None], ...]
    #: Picks and collection members that no list above gave, read one by one; left-out genres dropped, except
    #: from a hand pick.
    by_id: dict[tuple[int, MediaType], dict]

    def all_titles(self) -> dict[tuple[int, MediaType], dict]:
        """Every title in the season, as the first source to give it has it."""
        merged: dict[tuple[int, MediaType], dict] = {}
        for source in (self.tagged, self.genre, self.by_id):
            for title, item in source.items():
                merged.setdefault(title, item)
        return merged


def _read_sources(
    tmdb: _ListReader,
    plex: _CollectionReader,
    season: Season,
    discover: Callable[[MediaType, dict], list[dict]],
    *,
    missing_level: str = "WARNING",
) -> _SourceReads:
    """Read every source of ``season`` (see `load_titles`). ``discover`` reads one TMDB list; ``missing_level`` is
    the log level for a collection that is not in its library."""
    tagged: dict[tuple[int, MediaType], dict] = {}
    genre: dict[tuple[int, MediaType], dict] = {}
    for media_type, params in _queries(season):
        is_tag = "with_keywords" in params
        for item in discover(media_type, params):
            if is_tag and _left_out(season, media_type, item):
                continue
            (tagged if is_tag else genre).setdefault((int(item["id"]), media_type), item)

    collections: list[tuple[CollectionRef, list[LibraryTitle] | None]] = []
    for ref in season.collections:
        titles = plex.collection_members(ref.section_key, ref.title)
        if titles is None:
            logger.log(
                missing_level,
                "{}: collection “{}” isn't in your library tonight — using the season's other sources",
                season.name,
                ref.title,
            )
        collections.append((ref, titles))
    members = [(title.tmdb_id, title.media_type) for _ref, titles in collections for title in titles or ()]

    picks = set(season.picks)
    listed = tagged.keys() | genre.keys()
    wanted = [title for title in dict.fromkeys([*season.picks, *members]) if title not in listed]
    if len(wanted) > MAX_PLEX_SOURCED:
        logger.warning(
            "{}: {} titles from its collections and picks; only the first {} are used",
            season.name,
            len(wanted),
            MAX_PLEX_SOURCED,
        )
        wanted = wanted[:MAX_PLEX_SOURCED]
    by_id: dict[tuple[int, MediaType], dict] = {}
    for title, item in zip(wanted, _read_items(tmdb, wanted), strict=True):
        if item is None or (title not in picks and _left_out(season, title[1], item)):
            continue
        by_id[title] = item
    return _SourceReads(tagged=tagged, genre=genre, collections=tuple(collections), by_id=by_id)


def load_titles(
    tmdb: _ListReader,
    plex: _CollectionReader,
    season: Season,
    library_index: dict[MediaType, dict[int, int]],
) -> SeasonTitles:
    """Read a season's titles and split out the ones the libraries hold.

    The union of its sources (#137 D3):

    * its TMDB tags, OR'd together, for films and shows alike. No vote floor: a made-for-TV Christmas film is
      exactly the season and often has a handful of votes;
    * its TMDB genre, films only, at ``DISCOVER_MIN_VOTES``;
    * the members of the Plex collections it names. One that is not there tonight is reported in
      ``missing_collections``, and the season builds from the rest;
    * the owner's hand picks.

    Titles the last two name by id are read from TMDB, picks first, at most ``MAX_PLEX_SOURCED`` of them;
    one TMDB no longer has is skipped. ``keyword_excluded_genres`` drops a tag or collection film, never a
    hand pick. Raises when TMDB or Plex fails, so the rows that need this season keep what they have tonight
    rather than rebuild from half a list.
    """
    reads = _read_sources(tmdb, plex, season, tmdb.discover_all)
    found: dict[MediaType, dict[int, dict]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for (tmdb_id, media_type), item in reads.all_titles().items():
        found[media_type][tmdb_id] = item

    return SeasonTitles(
        ids={media_type: frozenset(items) for media_type, items in found.items()},
        in_library={
            media_type: [item for tmdb_id, item in items.items() if tmdb_id in library_index.get(media_type, {})]
            for media_type, items in found.items()
        },
        missing_collections=tuple(ref.title for ref, titles in reads.collections if titles is None),
    )


class _PagedListReader(_ListReader, Protocol):
    def discover_all(self, media_type: MediaType, params: dict, *, workers: int = ...) -> list[dict]: ...


#: Titles the editor's sample shows.
PREVIEW_SAMPLE = 10


@dataclass(frozen=True)
class CollectionCount:
    """What one Plex collection gives a season, for the editor."""

    title: str
    section_key: str
    #: False when the library has no collection of that title right now.
    found: bool
    #: Its titles the season would use: in a library, with a TMDB entry, and not of a left-out genre.
    in_library: int


@dataclass(frozen=True)
class SeasonPreview:
    """What the season editor shows while the owner builds a season (#137 D10). Every count is of titles in
    the libraries, which is all a season ever adds to a row."""

    #: The season's next day on or after today; None while its date rule is invalid.
    next_date: date | None
    #: Why the date rule is invalid, worded for the owner; None when it is fine.
    rule_error: str | None
    #: What `load_titles` puts in the season's ``in_library``.
    total: int
    #: ``total`` split by type: a row of both fills each library from its own type, so each half must fill it.
    movies: int
    shows: int
    #: The total split by the first source, in this order, that gives each title: they add up to ``total``.
    from_tags: int
    from_genre: int
    from_collections: int
    from_picks: int
    #: Tag id -> what that tag alone gives.
    per_tag: dict[int, int]
    per_collection: tuple[CollectionCount, ...]
    #: Up to ``PREVIEW_SAMPLE`` titles, the most voted on TMDB first.
    sample: tuple[str, ...]


def preview(
    tmdb: _PagedListReader,
    plex: _CollectionReader,
    season: Season,
    library_index: dict[MediaType, dict[int, int]],
    *,
    today: date,
    workers: int = 1,
) -> SeasonPreview:
    """Count a draft season's titles the way a run would, and say where they come from.

    The engine reads no clock, so ``today`` is the caller's. An invalid date rule still counts the films — the
    owner may be half-way through choosing a date — and says what is wrong with it instead of a next date.

    Args:
        tmdb: Reads TMDB lists and single titles.
        plex: Reads the season's Plex collections.
        season: The draft. Its slug, name and timing are not read.
        library_index: ``tmdb_id -> ratingKey`` per media type, over the libraries the row it is counted for
            builds in: every count is of what that row can draw (#137 I-1).
        today: The day the next date is counted from.
        workers: Pages of one TMDB list read at once.

    Returns:
        The counts, the next date and a sample.

    Raises:
        Exception: Whatever TMDB or Plex raised. A preview from part of the sources would understate them.
    """
    try:
        season.rule.validate()
    except ValueError as e:
        next_date, rule_error = None, str(e)
    else:
        next_date, rule_error = next_anchors(season, today, 1)[0], None

    def held(title: tuple[int, MediaType]) -> bool:
        return title[0] in library_index.get(title[1], {})

    discover = functools.partial(tmdb.discover_all, workers=workers)
    # A draft naming a collection Kometa only makes in season is normal while the owner edits; a WARNING per
    # preview would bury the ones a nightly run logs, which owners do read.
    reads = _read_sources(tmdb, plex, season, discover, missing_level="DEBUG")
    in_library = {title: item for title, item in reads.all_titles().items() if held(title)}

    from_tags = reads.tagged.keys() & in_library.keys()
    from_genre = (reads.genre.keys() & in_library.keys()) - from_tags
    per_collection = []
    members: set[tuple[int, MediaType]] = set()
    for ref, titles in reads.collections:
        gives = {(title.tmdb_id, title.media_type) for title in titles or ()} & in_library.keys()
        members |= gives
        per_collection.append(
            CollectionCount(
                title=ref.title, section_key=ref.section_key, found=titles is not None, in_library=len(gives)
            )
        )
    from_collections = members - from_tags - from_genre
    from_picks = (set(season.picks) & in_library.keys()) - from_tags - from_genre - from_collections

    def per_tag(tag: int) -> int:
        alone = replace(season, keywords=(tag,), movie_genres=())
        found = {
            (int(item["id"]), media_type)
            for media_type, params in _queries(alone)
            for item in discover(media_type, params)
            if not _left_out(season, media_type, item)
        }
        return sum(1 for title in found if held(title))

    def name(item: dict) -> str:
        return str(item.get("title") or item.get("name") or "")

    most_voted = sorted(in_library.values(), key=lambda item: (-(item.get("vote_count") or 0), name(item)))
    return SeasonPreview(
        next_date=next_date,
        rule_error=rule_error,
        total=len(in_library),
        movies=sum(1 for _tmdb_id, media_type in in_library if media_type is MediaType.MOVIE),
        shows=sum(1 for _tmdb_id, media_type in in_library if media_type is MediaType.SHOW),
        from_tags=len(from_tags),
        from_genre=len(from_genre),
        from_collections=len(from_collections),
        from_picks=len(from_picks),
        per_tag={tag: per_tag(tag) for tag in dict.fromkeys(season.keywords)},
        per_collection=tuple(per_collection),
        sample=tuple(name(item) for item in most_voted[:PREVIEW_SAMPLE]),
    )


@dataclass(frozen=True)
class Preset:
    """A ready-made season the editor offers (#137 D9). Adding one opens the editor filled in from it; nothing
    is saved until the owner saves. ``season.slug`` is the preset's key: a saved season gets its own slug."""

    key: str
    #: What the editor calls the preset, region included: "Mother's Day (US, CA, AU, NZ)". The season's own
    #: NAME carries no region, because a row's title renders it ("💐 Mother's Day picks").
    label: str
    season: Season
    #: What the editor says beside it — what to add when TMDB's tags fall short. Empty when they don't.
    note: str


#: The TMDB name of every tag a preset uses, as `/search/keyword` gave it on 2 Oct 2026. The editor shows a
#: tag by its name, and a preset opens without a TMDB read.
PRESET_TAG_NAMES: dict[int, str] = {
    613: "new year's eve",
    252123: "new year",
    235503: "independence day",
    159743: "fourth of july",
    282190: "4th of july",
    190024: "american revolution",
    2407: "fireworks",
    4543: "thanksgiving",
    209352: "st. patrick's day",
    10310: "leprechaun",
    14985: "ireland",
    299594: "irish",
    4729: "dublin, ireland",
    9921: "easter",
    9923: "easter bunny",
    173983: "mother's day",
    195439: "father's day",
}

_FEW_TAGGED = "TMDB tags only a handful of films for this day — add a collection or your own picks."
#: TMDB's Father's Day tag holds 3 films (2 Oct 2026), none of them in the 10,030-film library measured.
_FEW_FATHERS_DAY = "TMDB tags very few films as Father's Day — add a collection or your own picks."


def _preset(
    key: str,
    label: str,
    name: str,
    emoji: str,
    rule: DateRule,
    *,
    lead: int,
    after: int = 0,
    keywords: tuple[int, ...] = (),
    keyword_excluded_genres: tuple[int, ...] = (),
    note: str = "",
) -> Preset:
    season = Season(
        slug=key,
        name=name,
        emoji=emoji,
        rule=rule,
        description="",
        keywords=keywords,
        keyword_excluded_genres=keyword_excluded_genres,
        lead_days=lead,
        after_days=after,
    )
    return Preset(key=key, label=label, season=season, note=note)


# The spec's table (`.claude/docs/issue-137-custom-seasons.md`, "Presets"): tag ids verified against TMDB on
# 2 Oct 2026, and measured against a 10,030-film library. A wrong id is silent: it matches real, unrelated films.
PRESETS: tuple[Preset, ...] = (
    _preset(
        "new_years_eve",
        "New Year's Eve",
        "New Year's Eve",
        "🎆",
        DateRule("fixed", month=12, day=31),
        lead=7,
        after=1,
        keywords=(613, 252123),  # new year's eve, new year
    ),
    _preset(
        "fourth_of_july",
        "4th of July",
        "4th of July",
        "🎇",
        DateRule("fixed", month=7, day=4),
        lead=7,
        # independence day, fourth of july, 4th of july, american revolution, fireworks
        keywords=(235503, 159743, 282190, 190024, 2407),
    ),
    _preset(
        "thanksgiving_us",
        "Thanksgiving (US)",
        "Thanksgiving",
        "🦃",
        DateRule("nth", month=11, nth=4, weekday=3),
        lead=14,
        keywords=(4543,),  # thanksgiving
    ),
    _preset(
        "thanksgiving_ca",
        "Thanksgiving (Canada)",
        "Thanksgiving",
        "🍁",
        DateRule("nth", month=10, nth=2, weekday=0),
        lead=7,
        keywords=(4543,),  # thanksgiving
    ),
    _preset(
        "st_patricks_day",
        "St Patrick's Day",
        "St Patrick's Day",
        "☘️",
        DateRule("fixed", month=3, day=17),
        lead=7,
        # st. patrick's day, leprechaun, ireland, irish, dublin, ireland
        keywords=(209352, 10310, 14985, 299594, 4729),
        keyword_excluded_genres=(27,),  # Horror
        note="Leaves out Horror, which would otherwise bring in the Leprechaun slashers.",
    ),
    _preset(
        "easter",
        "Easter",
        "Easter",
        "🐣",
        DateRule("easter"),
        lead=14,
        keywords=(9921, 9923),  # easter, easter bunny
    ),
    _preset(
        "mothers_day",
        "Mother's Day (US, CA, AU, NZ)",
        "Mother's Day",
        "💐",
        DateRule("nth", month=5, nth=2, weekday=6),
        lead=7,
        keywords=(173983,),  # mother's day
        note=_FEW_TAGGED,
    ),
    _preset(
        "mothering_sunday",
        "Mothering Sunday (UK, IE)",
        "Mothering Sunday",
        "💐",
        DateRule("easter", offset=-21),
        lead=7,
        keywords=(173983,),  # mother's day
        note=_FEW_TAGGED,
    ),
    _preset(
        "fathers_day",
        "Father's Day (US, UK, CA, IE)",
        "Father's Day",
        "👔",
        DateRule("nth", month=6, nth=3, weekday=6),
        lead=7,
        keywords=(195439,),  # father's day
        note=_FEW_FATHERS_DAY,
    ),
    _preset(
        "fathers_day_au_nz",
        "Father's Day (AU, NZ)",
        "Father's Day",
        "👔",
        DateRule("nth", month=9, nth=1, weekday=6),
        lead=7,
        keywords=(195439,),  # father's day
        note=_FEW_FATHERS_DAY,
    ),
)
