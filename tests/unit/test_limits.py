"""Per-row length, year and rating limits (#138 phase 2)."""

from __future__ import annotations

from unittest.mock import MagicMock

from shortlist.engine.limits import RowLimits, apply_limits, runtime_minutes
from shortlist.engine.models import MediaType, RowSpec
from tests.conftest import make_candidate


def _tmdb(details: dict[int, dict | Exception]) -> MagicMock:
    tmdb = MagicMock()

    def fake(tmdb_id, media_type):
        out = details[tmdb_id]
        if isinstance(out, Exception):
            raise out
        return out

    tmdb.details.side_effect = fake
    return tmdb


def _ids(result) -> list[int]:
    return [c.tmdb_id for c in result.kept]


class TestRowLimits:
    def test_fingerprint_lists_only_set_parts_in_fixed_order(self):
        limits = RowLimits(max_runtime=120, min_year=1990, max_year=2010, min_rating=7.0)
        assert limits.fingerprint() == "rt<=120;y>=1990;y<=2010;r>=7.0"
        assert RowLimits(min_rating=6.5, max_runtime=90).fingerprint() == "rt<=90;r>=6.5"

    def test_active_is_false_when_nothing_is_set(self):
        assert not RowLimits().active
        assert RowLimits(min_year=2000).active

    def test_rowspec_builds_its_limits(self):
        spec = RowSpec(slug="x", name_template="", size=4, max_runtime=100, min_year=1980)
        assert spec.limits() == RowLimits(max_runtime=100, min_year=1980)
        assert not RowSpec(slug="x", name_template="", size=4).limits().active


class TestApplyLimits:
    def test_inactive_returns_the_same_list_and_never_calls_details(self):
        tmdb = MagicMock()
        pool = [make_candidate(1, "A", year=1999)]
        result = apply_limits(pool, RowLimits(), tmdb)
        assert result.kept is pool
        assert (result.dropped, result.unknown) == (0, 0)
        tmdb.details.assert_not_called()

    def test_year_bounds_are_inclusive(self):
        pool = [make_candidate(i, str(i), year=y) for i, y in [(1, 1989), (2, 1990), (3, 2010), (4, 2011)]]
        result = apply_limits(pool, RowLimits(min_year=1990, max_year=2010), MagicMock())
        assert _ids(result) == [2, 3]
        assert result.dropped == 2

    def test_unknown_year_is_kept_and_counted(self):
        pool = [make_candidate(1, "A", year=None), make_candidate(2, "B", year=1950)]
        result = apply_limits(pool, RowLimits(min_year=1990), MagicMock())
        assert _ids(result) == [1]
        assert result.unknown == 1

    def test_min_rating_drops_unrated_titles(self):
        pool = [
            make_candidate(1, "A", rating=0.0),
            make_candidate(2, "B", rating=6.9),
            make_candidate(3, "C", rating=7.0),
        ]
        result = apply_limits(pool, RowLimits(min_rating=7.0), MagicMock())
        assert _ids(result) == [3]
        assert result.dropped == 2

    def test_movie_runtime_uses_runtime(self):
        tmdb = _tmdb({1: {"runtime": 100}, 2: {"runtime": 130}, 3: {"runtime": 120}})
        pool = [make_candidate(i, str(i)) for i in (1, 2, 3)]
        result = apply_limits(pool, RowLimits(max_runtime=120), tmdb)
        assert _ids(result) == [1, 3]

    def test_tv_runtime_prefers_episode_run_time_then_last_episode(self):
        tmdb = _tmdb(
            {
                1: {"episode_run_time": [45, 50]},
                2: {"episode_run_time": [], "last_episode_to_air": {"runtime": 30}},
                3: {"episode_run_time": [90]},
                4: {"episode_run_time": []},
            }
        )
        pool = [make_candidate(i, str(i), media_type=MediaType.SHOW) for i in (1, 2, 3, 4)]
        result = apply_limits(pool, RowLimits(max_runtime=60), tmdb)
        assert _ids(result) == [1, 2, 4]
        assert result.unknown == 1

    def test_details_failure_keeps_that_title_and_judges_the_rest(self):
        tmdb = _tmdb({1: RuntimeError("boom"), 2: {"runtime": 200}, 3: {"runtime": 90}})
        pool = [make_candidate(i, str(i)) for i in (1, 2, 3)]
        result = apply_limits(pool, RowLimits(max_runtime=120), tmdb)
        assert _ids(result) == [1, 3]
        assert result.unknown == 1
        assert result.dropped == 1

    def test_empty_details_and_zero_runtime_are_unknown(self):
        tmdb = _tmdb({1: {}, 2: {"runtime": 0}})
        pool = [make_candidate(i, str(i)) for i in (1, 2)]
        result = apply_limits(pool, RowLimits(max_runtime=120), tmdb)
        assert _ids(result) == [1, 2]
        assert result.unknown == 2

    def test_details_is_not_fetched_for_titles_year_or_rating_already_dropped(self):
        tmdb = _tmdb({2: {"runtime": 90}, 3: {"runtime": 90}})
        pool = [
            make_candidate(1, "old", year=1950, rating=8.0),
            make_candidate(2, "ok", year=2000, rating=8.0),
            make_candidate(3, "unrated", year=2000, rating=8.5),
            make_candidate(4, "low", year=2000, rating=3.0),
        ]
        apply_limits(pool, RowLimits(max_runtime=120, min_year=1990, min_rating=7.0), tmdb)
        assert [c.args[0] for c in tmdb.details.call_args_list] == [2, 3]
        assert all(c.args[1] is MediaType.MOVIE for c in tmdb.details.call_args_list)

    def test_details_is_not_called_without_a_runtime_limit(self):
        tmdb = MagicMock()
        apply_limits([make_candidate(1, "A", year=2000)], RowLimits(min_year=1990), tmdb)
        tmdb.details.assert_not_called()


class TestRuntimeMinutes:
    def test_movie_reads_runtime(self):
        assert runtime_minutes({"runtime": 95}, "movie") == 95

    def test_tv_falls_back_to_last_episode(self):
        assert runtime_minutes({"last_episode_to_air": {"runtime": 42}}, "show") == 42

    def test_nothing_usable_is_none(self):
        assert runtime_minutes({}, "movie") is None
        assert runtime_minutes({"runtime": None}, "movie") is None
