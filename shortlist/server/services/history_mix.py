"""What one row's AI web search is told of one person's history this week (#152)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from shortlist.engine.history import derive_seeds, ratings_policy
from shortlist.engine.models import MediaType, RowSpec, Seed, UserProfile
from shortlist.engine.rows import _media_filter, seed_cycle_offset, wide_taste_applies
from shortlist.engine.taste import HistoryMix, history_mix
from shortlist.server.db.models import Collection, CollectionUserOverride, User
from shortlist.server.services.context_builder import ContextBuilder, _dislike_threshold
from shortlist.server.settings_store import SettingsStore


class NotFoundError(LookupError):
    """The row or the person asked about does not exist."""


def _item(seed: Seed, years: dict[tuple[str, MediaType], int | None]) -> dict:
    return {
        "title": seed.title,
        "year": years.get((seed.title, seed.media_type)),
        "media_type": seed.media_type.value,
        "tmdb_id": seed.tmdb_id,
    }


def _count(override: CollectionUserOverride | None, row: Collection, store: SettingsStore, name: str) -> int:
    """A count the way a run resolves it: this person's override, then the row's own, then the server's."""
    for source in (override, row):
        value = getattr(source, name, None) if source is not None else None
        if value is not None:
            return int(value)
    return int(store.get(f"recommendations.{name}"))


def row_history_mix(
    session: Session,
    secrets,
    *,
    profile_for: Callable[[Session, int], UserProfile],
    user_id: int,
    collection_id: int,
    now: datetime | None = None,
) -> dict:
    """The recent, favourite and older titles a row's web search would use for this person this week.

    Built from a live history read through the same calls a run makes (`derive_seeds`, `history_mix`) and
    the same inputs: counts resolve person, then row, then server; a "Don't seed" or disliked title is left
    out; a title sits in one group only. A row that would not widen (no AI web search, a single-title seed
    budget, or a shared row) reports no favourites or older titles. The row's libraries are not applied
    (that needs the PMS library index), so a row pinned to some libraries may use fewer of these.

    Raises:
        NotFoundError: The row or the person does not exist.
        RuntimeError: Plex is not configured, or the history could not be read.
    """
    row = session.get(Collection, collection_id)
    user = session.get(User, user_id)
    if row is None:
        raise NotFoundError("row not found")
    if user is None:
        raise NotFoundError("user not found")
    store = SettingsStore(session, secrets)
    override = session.get(CollectionUserOverride, (collection_id, user_id))
    recent_count = _count(override, row, store, "recent_count") or int(store.get("recommendations.recent_count") or 10)
    favourites = _count(override, row, store, "favourite_count")
    older = _count(override, row, store, "older_count")

    spec = RowSpec(
        slug=row.slug,
        name_template=row.name,
        size=row.size,
        media=row.media,
        shared=row.build == "shared",
        candidate_sources=list(row.candidate_sources or []),
        max_seeds=row.max_seeds,
        seasons=list(row.seasons or []),
        theme=ContextBuilder.theme_spec(session, row),
    )
    default_sources = list(store.get("candidates.sources") or ["tmdb_similar", "tmdb_discover"])
    default_max_seeds = int(store.get("recommendations.max_seeds") or 30)
    wide = not spec.shared and wide_taste_applies(
        spec,
        favourite_count=favourites,
        older_count=older,
        default_sources=default_sources,
        default_max_seeds=default_max_seeds,
    )
    if not wide:
        favourites = older = 0

    profile = profile_for(session, user_id)
    history = _media_filter(profile.history, row.media)
    threshold = _dislike_threshold(store) if store.get("recommendations.use_plex_ratings") else None
    ratings = ratings_policy(profile.history, threshold)
    window = int(row.seed_window or 1)
    moment = now or datetime.now(UTC)
    seeds = derive_seeds(
        history,
        lambda item: item.tmdb_id,
        max_seeds=spec.max_seeds if spec.max_seeds is not None else default_max_seeds,
        blocked=profile.blocked_seeds,
        window=window,
        cycle_offset=seed_cycle_offset(row.slug, user.slug, moment.toordinal()) if window > 1 else 0,
        disliked=ratings.blocked,
    )[:recent_count]
    mix = HistoryMix(None, [], [])
    if wide:
        mix = history_mix(
            history,
            blocked=profile.blocked_seeds,
            ratings=ratings,
            resolve=lambda item: item.tmdb_id,
            favourites=favourites,
            older=older,
            now=moment,
            lookback_years=int(store.get("recommendations.older_lookback_years")),
            searched=frozenset((s.tmdb_id, s.media_type) for s in seeds),
        )
    years = {(w.title, w.media_type): w.year for w in history}
    return {
        "recent": [_item(s, years) for s in seeds],
        "favourites": [_item(s, years) for s in mix.favourite_seeds[:favourites]],
        "older": [_item(s, years) for s in mix.older_seeds[:older]],
        "favourite_count": favourites,
        "older_count": older,
    }
