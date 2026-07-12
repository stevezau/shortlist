"""Candidate discovery: TMDB similar/recommended pooling, tagged with the seeds that produced them."""

from __future__ import annotations

from loguru import logger

from rowarr.engine.clients.tmdb import TmdbClient
from rowarr.engine.models import Candidate, MediaType, Seed


def gather_candidates(tmdb: TmdbClient, seeds: list[Seed]) -> list[Candidate]:
    """Pool TMDB suggestions across all seeds, deduped by tmdb_id with seed provenance kept."""
    pool: dict[tuple[int, MediaType], Candidate] = {}
    genre_maps: dict[MediaType, dict[int, str]] = {}
    for seed in seeds:
        if seed.media_type not in genre_maps:
            genre_maps[seed.media_type] = tmdb.genre_names(seed.media_type)
        genres = genre_maps[seed.media_type]
        for item in tmdb.suggestions(seed.tmdb_id, seed.media_type):
            key = (item["id"], seed.media_type)
            if key not in pool:
                date = item.get("release_date") or item.get("first_air_date") or ""
                pool[key] = Candidate(
                    tmdb_id=item["id"],
                    title=item.get("title") or item.get("name") or "",
                    media_type=seed.media_type,
                    year=int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
                    genres=[genres[g] for g in item.get("genre_ids", []) if g in genres],
                    rating=float(item.get("vote_average") or 0.0),
                )
            pool[key].seeds.append(seed)
    logger.debug("candidate pool: {} unique titles from {} seeds", len(pool), len(seeds))
    return list(pool.values())


def filter_candidates(
    candidates: list[Candidate],
    library_index: dict[MediaType, dict[int, int]],
    *,
    watched_tmdb_ids: set[tuple[int, MediaType]],
    excluded_genres: set[str],
    recent_pick_ids: set[tuple[int, MediaType]],
) -> list[Candidate]:
    """Intersect with the library and drop watched/excluded/stale titles.

    Titles are identified by (tmdb_id, media_type), never by id alone: TMDB ids are unique only
    WITHIN a namespace, so movie 550 and TV 550 are different titles. Keying on the bare id makes
    watching a film silently blacklist the show that happens to share its number.

    Args:
        candidates: The pooled TMDB candidates.
        library_index: media_type -> {tmdb_id -> ratingKey} built once per run.
        watched_tmdb_ids: (tmdb_id, media_type) this user has already watched.
        excluded_genres: Per-user genre exclusions (case-insensitive).
        recent_pick_ids: (tmdb_id, media_type) recommended within the last N runs (staleness guard).
    """
    excluded = {g.lower() for g in excluded_genres}
    kept = []
    for c in candidates:
        rating_key = library_index.get(c.media_type, {}).get(c.tmdb_id)
        if rating_key is None:
            continue
        if (c.tmdb_id, c.media_type) in watched_tmdb_ids or (c.tmdb_id, c.media_type) in recent_pick_ids:
            continue
        if excluded and any(g.lower() in excluded for g in c.genres):
            continue
        c.rating_key = rating_key
        kept.append(c)
    return kept
