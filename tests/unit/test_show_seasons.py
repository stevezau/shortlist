"""Which seasons of a show a request names: the pure picking rules shared by both routes."""

from __future__ import annotations

from datetime import date

from shortlist.engine.show_seasons import describe_seasons, pick_season_numbers, tmdb_request_seasons

TODAY = date(2026, 10, 11)


def _season(number: int, episodes: int = 10, air_date: str | None = "2020-01-01") -> dict:
    return {"season_number": number, "episode_count": episodes, "air_date": air_date}


class TestPickSeasonNumbers:
    def test_first_takes_the_lowest(self):
        assert pick_season_numbers([3, 1, 2, 4], "firstSeason", 2) == [1, 2]

    def test_last_takes_the_highest_sorted_ascending(self):
        assert pick_season_numbers([1, 2, 3, 4], "lastSeason", 2) == [3, 4]

    def test_count_beyond_the_show_takes_everything(self):
        assert pick_season_numbers([1, 2], "firstSeason", 9) == [1, 2]

    def test_specials_are_never_picked(self):
        assert pick_season_numbers([0, 1, 2], "firstSeason", 2) == [1, 2]

    def test_a_count_below_one_is_one(self):
        assert pick_season_numbers([1, 2, 3], "lastSeason", 0) == [3]

    def test_other_modes_pick_nothing(self):
        assert pick_season_numbers([1, 2], "all", 1) == []


class TestTmdbRequestSeasons:
    def test_specials_and_empty_seasons_are_excluded(self):
        details = {"seasons": [_season(0), _season(1), _season(2, episodes=0), _season(3)]}
        assert tmdb_request_seasons(details, "firstSeason", 5, TODAY) == [1, 3]

    def test_last_skips_a_season_that_has_not_started_airing(self):
        details = {"seasons": [_season(1), _season(2), _season(3, air_date="2027-01-01"), _season(4, air_date=None)]}
        assert tmdb_request_seasons(details, "lastSeason", 1, TODAY) == [2]

    def test_last_falls_back_to_all_candidates_when_none_have_aired(self):
        details = {"seasons": [_season(1, air_date="2027-01-01"), _season(2, air_date=None)]}
        assert tmdb_request_seasons(details, "lastSeason", 1, TODAY) == [2]

    def test_first_ignores_air_dates(self):
        details = {"seasons": [_season(1, air_date="2027-01-01"), _season(2)]}
        assert tmdb_request_seasons(details, "firstSeason", 1, TODAY) == [1]

    def test_no_seasons_is_empty(self):
        assert tmdb_request_seasons({}, "firstSeason", 1, TODAY) == []
        assert tmdb_request_seasons({"seasons": [_season(0)]}, "lastSeason", 1, TODAY) == []


class TestDescribeSeasons:
    def test_one_season(self):
        assert describe_seasons([5]) == "season 5"

    def test_a_contiguous_run_is_a_range(self):
        assert describe_seasons([1, 2, 3]) == "seasons 1\u20133"

    def test_a_gap_is_listed(self):
        assert describe_seasons([1, 3]) == "seasons 1, 3"
