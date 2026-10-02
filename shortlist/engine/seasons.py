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
import hashlib
import json
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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
    found: dict[MediaType, dict[int, dict]] = {MediaType.MOVIE: {}, MediaType.SHOW: {}}
    for media_type, params in _queries(season):
        for item in tmdb.discover_all(media_type, params):
            if "with_keywords" in params and _left_out(season, media_type, item):
                continue
            found[media_type].setdefault(int(item["id"]), item)

    missing: list[str] = []
    members: list[tuple[int, MediaType]] = []
    for ref in season.collections:
        titles = plex.collection_members(ref.section_key, ref.title)
        if titles is None:
            missing.append(ref.title)
            logger.warning(
                "{}: collection “{}” isn't in your library tonight — using the season's other sources",
                season.name,
                ref.title,
            )
            continue
        members += [(title.tmdb_id, title.media_type) for title in titles]

    picks = set(season.picks)
    by_id = [title for title in dict.fromkeys([*season.picks, *members]) if title[0] not in found[title[1]]]
    if len(by_id) > MAX_PLEX_SOURCED:
        logger.warning(
            "{}: {} titles from its collections and picks; only the first {} are used",
            season.name,
            len(by_id),
            MAX_PLEX_SOURCED,
        )
        by_id = by_id[:MAX_PLEX_SOURCED]
    for (tmdb_id, media_type), item in zip(by_id, _read_items(tmdb, by_id), strict=True):
        if item is None or ((tmdb_id, media_type) not in picks and _left_out(season, media_type, item)):
            continue
        found[media_type].setdefault(tmdb_id, item)

    return SeasonTitles(
        ids={media_type: frozenset(items) for media_type, items in found.items()},
        in_library={
            media_type: [item for tmdb_id, item in items.items() if tmdb_id in library_index.get(media_type, {})]
            for media_type, items in found.items()
        },
        missing_collections=tuple(missing),
    )
