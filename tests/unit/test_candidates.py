from rowarr.engine.candidates import filter_candidates, gather_candidates
from rowarr.engine.models import MediaType, Seed
from tests.conftest import make_candidate


def seed(tmdb_id: int, title: str = "Seed") -> Seed:
    return Seed(tmdb_id=tmdb_id, title=title, media_type=MediaType.MOVIE, weight=1.0)


class TestGatherCandidates:
    def test_pools_and_tags_with_all_suggesting_seeds(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 42, "title": "Shared Pick", "genre_ids": [18], "vote_average": 8.0, "release_date": "2020-01-01"},
            {"id": 42 + tid, "title": f"Only {tid}", "genre_ids": [], "vote_average": 6.0},
        ]
        pool = gather_candidates(mock_tmdb, [seed(1), seed(2)])
        shared = next(c for c in pool if c.tmdb_id == 42)
        assert shared.seed_frequency == 2
        assert shared.genres == ["Drama"]
        assert shared.year == 2020
        assert len(pool) == 3

    def test_genre_map_fetched_once_per_media_type(self, mock_tmdb):
        gather_candidates(mock_tmdb, [seed(1), seed(2), seed(3)])
        assert mock_tmdb.genre_names.call_count == 1


class TestFilterCandidates:
    def _index(self):
        return {MediaType.MOVIE: {10: 1010, 20: 1020, 30: 1030}, MediaType.SHOW: {}}

    def test_keeps_only_library_matches_and_sets_rating_key(self):
        cands = [make_candidate(10, "In"), make_candidate(99, "Out")]
        kept = filter_candidates(
            cands, self._index(), watched_tmdb_ids=set(), excluded_genres=set(), recent_pick_ids=set()
        )
        assert [c.tmdb_id for c in kept] == [10]
        assert kept[0].rating_key == 1010

    def test_drops_watched_excluded_genre_and_stale(self):
        cands = [
            make_candidate(10, "Watched"),
            make_candidate(20, "Horror pick", genres=["Horror"]),
            make_candidate(30, "Stale"),
        ]
        kept = filter_candidates(
            cands,
            self._index(),
            watched_tmdb_ids={(10, MediaType.MOVIE)},
            excluded_genres={"horror"},
            recent_pick_ids={(30, MediaType.MOVIE)},
        )
        assert kept == []

    def test_a_watched_movie_does_not_suppress_the_show_that_shares_its_id(self):
        """TMDB ids are unique only within a namespace: movie 550 and TV 550 are different
        titles. Keying the guards on the bare id silently drops valid recommendations."""
        show = make_candidate(550, "Some Show", media_type=MediaType.SHOW)
        index = {MediaType.MOVIE: {550: 1550}, MediaType.SHOW: {550: 2550}}

        kept = filter_candidates(
            [show],
            index,
            watched_tmdb_ids={(550, MediaType.MOVIE)},  # they watched the FILM
            excluded_genres=set(),
            recent_pick_ids={(550, MediaType.MOVIE)},  # and it was recently picked
        )

        assert [c.title for c in kept] == ["Some Show"]
