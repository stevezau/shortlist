"""Themes (#138): a named, dateless set of titles with hard rules, loaded through the season path.

A theme is the membership half of a custom season (tags, genres, collections, picks) plus limits on
runtime, year, rating and votes. Pure: takes clients, returns titles.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.limits import apply_limits, apply_runtime_limit
from shortlist.engine.models import Candidate, MediaType, RowLimits
from shortlist.engine.seasons import (
    CollectionRef,
    DateRule,
    Season,
    SeasonTitles,
    _CollectionReader,
    _ListReader,
    _read_sources,
)

__all__ = [
    "GENRE_IDS_BY_NAME",
    "ThemeCollection",
    "ThemePick",
    "ThemeSpec",
    "ThemeTitles",
    "load_theme",
    "theme_as_season",
    "theme_content_hash",
]

# TMDB's movie genre list, which it has not changed in years. Themes name genres in words (the AI writes
# them); `Season.movie_genres` wants ids. Shows are never genre-queried (seasons.py), so no TV list.
GENRE_IDS_BY_NAME: dict[str, int] = {
    "action": 28,
    "adventure": 12,
    "animation": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "fantasy": 14,
    "history": 36,
    "horror": 27,
    "music": 10402,
    "mystery": 9648,
    "romance": 10749,
    "science fiction": 878,
    "sci-fi": 878,
    "scifi": 878,
    "tv movie": 10770,
    "thriller": 53,
    "war": 10752,
    "western": 37,
}
MOVIE_GENRE_IDS = frozenset(GENRE_IDS_BY_NAME.values())


@dataclass(frozen=True)
class ThemePick:
    """One title a theme names by id. ``origin`` is ``ai`` or ``owner``; ``reason`` is shown on the row."""

    tmdb_id: int
    media: MediaType
    origin: str
    reason: str | None


@dataclass(frozen=True)
class ThemeCollection:
    """A Plex collection a theme reads, by library section key and title (never ratingKey)."""

    section_key: str
    title: str


@dataclass(frozen=True)
class ThemeSpec:
    """Everything that defines a theme. ``genres`` and ``excluded_genres`` are TMDB genre names."""

    slug: str
    name: str
    emoji: str | None
    media: tuple[MediaType, ...]
    tags: tuple[int, ...]
    genres: tuple[str, ...]
    excluded_genres: tuple[str, ...]
    collections: tuple[ThemeCollection, ...]
    picks: tuple[ThemePick, ...]
    rules: RowLimits
    min_votes: int | None


@dataclass(frozen=True)
class ThemeTitles:
    """A theme's titles for one run, with the reason the AI gave for each pick that carries one."""

    titles: SeasonTitles
    reasons: dict[tuple[MediaType, int], str]
    #: The theme's OWN titles that the libraries hold and its rules allow: its picks of any origin and its
    #: collections' members, as opposed to the tag and genre matches that fill out a row.
    own: frozenset[tuple[MediaType, int]] = frozenset()
    #: False when a collection the theme names was not readable tonight, so ``own`` is missing its members and
    #: cannot say which library holds none of them.
    own_complete: bool = True
    #: How many of the theme's titles the libraries hold, before its rules are applied.
    held: int = 0
    #: How many titles passed the cheap rules and so needed a running-time check, and how many got one. They
    #: differ only when ``load_theme`` was given ``max_details``; the rest were kept as "runtime unknown".
    runtime_total: int = 0
    runtime_checked: int = 0


def theme_content_hash(spec: ThemeSpec) -> str:
    """A fingerprint of what a theme contains, blind to order, name, emoji and pick reasons.

    The row's recipe carries it, so changing the contents rebuilds the row while a rename only renames it.
    """
    content = [
        sorted(spec.tags),
        sorted(_genre_key(name) for name in spec.genres),
        sorted(_genre_key(name) for name in spec.excluded_genres),
        sorted((ref.section_key, ref.title) for ref in spec.collections),
        sorted((pick.tmdb_id, pick.media.value) for pick in spec.picks),
        spec.rules.fingerprint(),
        spec.min_votes,
        sorted(media.value for media in spec.media),
    ]
    return hashlib.sha1(json.dumps(content).encode()).hexdigest()


def _genre_key(name: str) -> str:
    """A genre's hash key: its TMDB id when the name is known, so aliases and case hash alike, else its
    lowered name. Always a string, and prefixed so a name can never collide with an id."""
    lowered = name.strip().lower()
    genre_id = GENRE_IDS_BY_NAME.get(lowered)
    return f"id:{genre_id}" if genre_id is not None else f"name:{lowered}"


def _genre_ids(names: tuple[str, ...]) -> tuple[int, ...]:
    ids = []
    for name in names:
        genre_id = GENRE_IDS_BY_NAME.get(name.strip().lower())
        if genre_id is None:
            logger.warning("theme: unknown genre “{}” ignored", name)
            continue
        ids.append(genre_id)
    return tuple(ids)


def theme_as_season(spec: ThemeSpec) -> Season:
    """The theme as a dateless `Season`, in the form `load_titles` and `load_theme` read."""
    return Season(
        slug=spec.slug,
        name=spec.name,
        emoji=spec.emoji or "",
        # Never read: nothing in the load path consults a season's date, but the field is required.
        rule=DateRule("fixed"),
        description="",
        keywords=spec.tags,
        movie_genres=_genre_ids(spec.genres),
        keyword_excluded_genres=_genre_ids(spec.excluded_genres),
        collections=tuple(CollectionRef(section_key=ref.section_key, title=ref.title) for ref in spec.collections),
        picks=tuple((pick.tmdb_id, pick.media) for pick in spec.picks),
    )


def _year(item: dict) -> int | None:
    stamp = str(item.get("release_date") or item.get("first_air_date") or "")
    return int(stamp[:4]) if stamp[:4].isdigit() else None


def _candidate(media_type: MediaType, item: dict) -> Candidate:
    return Candidate(
        tmdb_id=int(item["id"]),
        title=str(item.get("title") or item.get("name") or ""),
        media_type=media_type,
        year=_year(item),
        rating=float(item.get("vote_average") or 0.0),
        vote_count=int(item.get("vote_count") or 0),
    )


def load_theme(
    tmdb: _ListReader | TmdbClient,
    plex: _CollectionReader,
    spec: ThemeSpec,
    library_index: dict[MediaType, dict[int, int]],
    max_details: int | None = None,
) -> ThemeTitles:
    """Read a theme's titles through the season path and keep the ones the libraries hold, inside its rules.

    An AI row is library-only (nothing is ever requested), so the rules run on the titles the libraries
    hold and no others: a runtime limit costs one TMDB details call per title ON THE SERVER, not one per
    title TMDB lists. ``ids`` is therefore the allowed titles that are on the server, and ``in_library``
    carries their items. ``held`` counts the server's titles before the rules, for the preview's counts.
    A pick TMDB no longer has is skipped, and so is a title of a kind the theme does not cover.

    The rules that need only list data (year, rating, votes, kind) run first, so the running-time check sees
    just the survivors, and it looks titles up several at a time. ``max_details`` bounds those lookups for a
    caller that cannot wait (the preview): the theme's named titles are checked first, then the most voted,
    and the rest are kept as "runtime unknown" — the nightly run, which passes none, checks them all.

    Raises:
        Exception: Whatever TMDB or Plex raised, so callers keep tonight's row rather than rebuild from half
            a list.
    """
    season = theme_as_season(spec)
    reads = _read_sources(tmdb, plex, season, tmdb.discover_all)
    items = {
        (tmdb_id, media_type): item
        for (tmdb_id, media_type), item in reads.all_titles().items()
        if media_type in spec.media and tmdb_id in library_index.get(media_type, {})
    }

    picked = set(season.picks)
    candidates = [_candidate(media_type, item) for (_id, media_type), item in items.items()]
    if spec.min_votes is not None:
        # Picks are the owner's or the AI's deliberate choice, so a vote floor never drops one (as in seasons).
        candidates = [c for c in candidates if c.vote_count >= spec.min_votes or (c.tmdb_id, c.media_type) in picked]
    held = len(items)
    logger.debug("theme {}: applying rules to {} titles on the server", spec.slug, len(candidates))
    cheap = apply_limits(candidates, replace(spec.rules, max_runtime=None), tmdb)  # type: ignore[arg-type]
    survivors = cheap.kept
    runtime_total = runtime_checked = 0
    if spec.rules.max_runtime is not None:
        runtime_total = runtime_checked = len(survivors)
        to_check = survivors
        if max_details is not None and len(survivors) > max_details:
            by_priority = sorted(survivors, key=lambda c: ((c.tmdb_id, c.media_type) not in picked, -c.vote_count))
            to_check = by_priority[: max(0, max_details)]
            runtime_checked = len(to_check)
        checked = apply_runtime_limit(to_check, spec.rules.max_runtime, tmdb)  # type: ignore[arg-type]
        too_long = {(c.tmdb_id, c.media_type) for c in checked.dropped_candidates}
        survivors = [c for c in survivors if (c.tmdb_id, c.media_type) not in too_long]
    allowed = {(c.tmdb_id, c.media_type) for c in survivors}

    ids: dict[MediaType, set[int]] = {MediaType.MOVIE: set(), MediaType.SHOW: set()}
    in_library: dict[MediaType, list[dict]] = {MediaType.MOVIE: [], MediaType.SHOW: []}
    for (tmdb_id, media_type), item in items.items():
        if (tmdb_id, media_type) not in allowed:
            continue
        ids[media_type].add(tmdb_id)
        in_library[media_type].append(item)

    reasons = {
        (pick.media, pick.tmdb_id): pick.reason
        for pick in spec.picks
        if pick.reason and pick.tmdb_id in ids[pick.media]
    }
    named = set(season.picks) | {
        (title.tmdb_id, title.media_type) for _ref, found in reads.collections for title in found or ()
    }
    own = frozenset((media_type, tmdb_id) for tmdb_id, media_type in named if tmdb_id in ids.get(media_type, ()))
    titles = SeasonTitles(
        ids={media_type: frozenset(found) for media_type, found in ids.items()},
        in_library=in_library,
        missing_collections=tuple(ref.title for ref, found in reads.collections if found is None),
    )
    return ThemeTitles(
        titles=titles,
        reasons=reasons,
        own=own,
        own_complete=not titles.missing_collections,
        held=held,
        runtime_total=runtime_total,
        runtime_checked=runtime_checked,
    )
