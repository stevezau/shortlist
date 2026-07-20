"""History-source matrix: tautulli / plex, plus seed derivation."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from shortlist.engine.history import FallbackHistorySource, PlexHistorySource, TautulliSource, derive_seeds
from shortlist.engine.models import MediaType
from tests.conftest import make_profile, make_watched


def tautulli_row(**kw) -> dict:
    base = {
        "media_type": "movie",
        "title": "Heat",
        "grandparent_title": None,
        "percent_complete": 100,
        "date": 1752000000,
        "year": 1995,
        "rating_key": 42,
        "grandparent_rating_key": None,
        "parent_media_index": None,
        "media_index": None,
    }
    return {**base, **kw}


class TestTautulliSource:
    def test_maps_movie_rows(self, mock_tautulli):
        mock_tautulli.get_history.return_value = [tautulli_row()]
        items = TautulliSource(mock_tautulli).fetch(make_profile(), min_completion=0.7)
        assert len(items) == 1
        assert items[0].title == "Heat"
        assert items[0].media_type is MediaType.MOVIE
        assert items[0].completion == 1.0
        # A movie carries no episode detail.
        assert (items[0].season, items[0].episode, items[0].episode_title) == (None, None, None)
        mock_tautulli.get_history.assert_called_once_with(100, since_ts=None)

    def test_episode_rows_collapse_to_show_title(self, mock_tautulli):
        mock_tautulli.get_history.return_value = [
            tautulli_row(
                media_type="episode",
                title="Pilot",
                grandparent_title="Suits",
                grandparent_rating_key=7,
                parent_media_index="2",
                media_index="5",
            ),
        ]
        items = TautulliSource(mock_tautulli).fetch(make_profile(), min_completion=0.7)
        assert items[0].title == "Suits"
        assert items[0].media_type is MediaType.SHOW
        assert items[0].rating_key == 7
        # The episode detail is carried for display (show title stays the seed).
        assert (items[0].season, items[0].episode) == (2, 5)
        assert items[0].episode_title == "Pilot"

    def test_incomplete_watches_filtered_by_threshold(self, mock_tautulli):
        mock_tautulli.get_history.return_value = [tautulli_row(percent_complete=20)]
        assert TautulliSource(mock_tautulli).fetch(make_profile(), min_completion=0.7) == []


class TestPlexHistorySource:
    def test_maps_entries_from_plex_history_api(self, mock_plex):
        entry = SimpleNamespace(
            type="episode",
            grandparentTitle="Suits",
            title="Pilot",
            parentIndex="2",
            index="5",
            viewedAt=datetime(2026, 7, 1),
            grandparentRatingKey="7",
            ratingKey="99",
        )
        mock_plex._server.history.return_value = [entry]
        items = PlexHistorySource(mock_plex).fetch(make_profile(account_id=555000100), min_completion=0.7)
        assert items[0].title == "Suits"
        assert items[0].rating_key == 7
        # PMS parentIndex/index → season/episode, entry.title → episode name.
        assert (items[0].season, items[0].episode) == (2, 5)
        assert items[0].episode_title == "Pilot"
        call = mock_plex._server.history.call_args
        assert call.kwargs["accountID"] == 555000100


class TestFallbackHistorySource:
    """Phase 1 pilot lesson: Tautulli had 1 row for a user with 11k rows of PMS history."""

    def _source(self, primary_items, fallback_items, min_items=10):
        primary, fallback = MagicMock(), MagicMock()
        primary.fetch.return_value = primary_items
        fallback.fetch.return_value = fallback_items
        return FallbackHistorySource(primary, fallback, min_items=min_items), primary, fallback

    def test_rich_primary_wins_without_touching_fallback(self):
        items = [make_watched(f"m{i}") for i in range(10)]
        source, _, fallback = self._source(items, [])
        assert source.fetch(make_profile(), min_completion=0.7) == items
        fallback.fetch.assert_not_called()

    def test_thin_primary_falls_back_to_richer_source(self):
        thin = [make_watched("only one")]
        rich = [make_watched(f"m{i}") for i in range(20)]
        source, _, _ = self._source(thin, rich)
        assert source.fetch(make_profile(), min_completion=0.7) == rich

    def test_thin_primary_kept_when_fallback_is_no_better(self):
        thin = [make_watched("only one")]
        source, _, _ = self._source(thin, [])
        assert source.fetch(make_profile(), min_completion=0.7) == thin

    def test_primary_error_uses_fallback(self):
        source, primary, _ = self._source([], [make_watched("m")])
        primary.fetch.side_effect = RuntimeError("tautulli down")
        assert len(source.fetch(make_profile(), min_completion=0.7)) == 1


class TestDeriveSeeds:
    def test_frequency_and_recency_weighting(self):
        history = [
            make_watched("Old Favorite", days_ago=80),
            make_watched("Binged Show", days_ago=2, media_type=MediaType.SHOW),
            make_watched("Binged Show", days_ago=3, media_type=MediaType.SHOW),
            make_watched("Binged Show", days_ago=4, media_type=MediaType.SHOW),
        ]
        ids = {("Binged Show", MediaType.SHOW): 1, ("Old Favorite", MediaType.MOVIE): 2}
        seeds = derive_seeds(history, lambda w: ids.get((w.title, w.media_type)))
        assert seeds[0].title == "Binged Show"
        assert seeds[0].weight > seeds[1].weight

    def test_unresolvable_titles_are_skipped(self):
        seeds = derive_seeds([make_watched("Unknown")], lambda w: None)
        assert seeds == []

    def test_max_seeds_cap(self):
        history = [make_watched(f"Movie {i}", days_ago=i) for i in range(10)]
        ids = {f"Movie {i}": i + 1 for i in range(10)}
        seeds = derive_seeds(history, lambda w: ids[w.title], max_seeds=4)
        assert len(seeds) == 4

    def test_reserves_seed_budget_for_the_minority_media_type(self):
        # A TV-heavy watcher: 20 recent shows + 3 older movies. The movies must still seed, or a
        # media=both row's Movies half starves (SFLIX/MooHouse: 58 of her last 60 watches were TV).
        history = [make_watched(f"Show {i}", days_ago=i, media_type=MediaType.SHOW) for i in range(20)]
        history += [make_watched(f"Movie {i}", days_ago=40 + i, media_type=MediaType.MOVIE) for i in range(3)]
        ids = {f"Show {i}": i + 1 for i in range(20)}
        ids |= {f"Movie {i}": 100 + i for i in range(3)}
        seeds = derive_seeds(history, lambda w: ids[w.title], max_seeds=10)
        assert len(seeds) == 10
        # All 3 movies survive the cap despite ranking below every show by weight — without the
        # per-media reserve the top 10 would be all shows and the movie row would get no candidates.
        assert sum(1 for s in seeds if s.media_type is MediaType.MOVIE) == 3
