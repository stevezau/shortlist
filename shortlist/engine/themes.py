"""Themes (#138): a named, dateless set of titles with hard rules, loaded through the season path.

A theme is the membership half of a custom season (tags, genres, collections, picks) plus limits on
runtime, year, rating and votes. Pure: takes clients, returns titles.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from loguru import logger

from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.limits import apply_limits
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
_MOVIE_GENRE_IDS: dict[str, int] = {
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
    genre_id = _MOVIE_GENRE_IDS.get(lowered)
    return f"id:{genre_id}" if genre_id is not None else f"name:{lowered}"


def _genre_ids(names: tuple[str, ...]) -> tuple[int, ...]:
    ids = []
    for name in names:
        genre_id = _MOVIE_GENRE_IDS.get(name.strip().lower())
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
) -> ThemeTitles:
    """Read a theme's titles through the season path and keep only those inside its rules.

    Rules apply to every title, so ``ids`` is exactly what the theme allows; ``in_library`` is the part of
    that the libraries hold. A pick TMDB no longer has is skipped.

    Raises:
        Exception: Whatever TMDB or Plex raised, so callers keep tonight's row rather than rebuild from half
            a list.
    """
    season = theme_as_season(spec)
    reads = _read_sources(tmdb, plex, season, tmdb.discover_all)
    items = reads.all_titles()

    picked = set(season.picks)
    candidates = [_candidate(media_type, item) for (_id, media_type), item in items.items()]
    if spec.min_votes is not None:
        # Picks are the owner's or the AI's deliberate choice, so a vote floor never drops one (as in seasons).
        candidates = [c for c in candidates if c.vote_count >= spec.min_votes or (c.tmdb_id, c.media_type) in picked]
    logger.debug("theme {}: applying rules to {} titles", spec.slug, len(candidates))
    kept = apply_limits(candidates, spec.rules, tmdb)  # type: ignore[arg-type]
    allowed = {(c.tmdb_id, c.media_type) for c in kept.kept}

    ids: dict[MediaType, set[int]] = {MediaType.MOVIE: set(), MediaType.SHOW: set()}
    in_library: dict[MediaType, list[dict]] = {MediaType.MOVIE: [], MediaType.SHOW: []}
    for (tmdb_id, media_type), item in items.items():
        if (tmdb_id, media_type) not in allowed:
            continue
        ids[media_type].add(tmdb_id)
        if tmdb_id in library_index.get(media_type, {}):
            in_library[media_type].append(item)

    reasons = {
        (pick.media, pick.tmdb_id): pick.reason
        for pick in spec.picks
        if pick.reason and pick.tmdb_id in ids[pick.media]
    }
    titles = SeasonTitles(
        ids={media_type: frozenset(found) for media_type, found in ids.items()},
        in_library=in_library,
        missing_collections=tuple(ref.title for ref, found in reads.collections if found is None),
    )
    return ThemeTitles(titles=titles, reasons=reasons)
