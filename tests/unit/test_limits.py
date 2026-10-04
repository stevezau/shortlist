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

    def test_a_zero_min_rating_means_no_limit(self):
        assert not RowLimits(min_rating=0).active
        assert not RowLimits(min_rating=0.0).active
        assert RowLimits(min_rating=0.1).active
        assert RowLimits(min_rating=0.0, min_year=2000).fingerprint() == RowLimits(min_year=2000).fingerprint()
        assert RowLimits(min_rating=0.0).fingerprint() == RowLimits().fingerprint() == ""

    def test_a_zero_min_rating_leaves_the_pool_untouched(self):
        pool = [make_candidate(1, "A", rating=0.0)]
        tmdb = MagicMock()
        result = apply_limits(pool, RowLimits(min_rating=0.0), tmdb)
        assert result.kept is pool
        tmdb.details.assert_not_called()

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

    def test_unknown_counts_titles_not_fields(self):
        tmdb = _tmdb({1: {}})
        pool = [make_candidate(1, "A", year=None, vote_count=5)]
        result = apply_limits(pool, RowLimits(min_year=1990, max_year=2000, max_runtime=100), tmdb)
        assert _ids(result) == [1]
        assert result.unknown == 1

    def test_min_rating_drops_unrated_titles(self):
        pool = [
            make_candidate(1, "A", rating=0.0, vote_count=5),
            make_candidate(2, "B", rating=6.9, vote_count=5),
            make_candidate(3, "C", rating=7.0, vote_count=5),
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
            make_candidate(1, "old", year=1950, rating=8.0, vote_count=9),
            make_candidate(2, "ok", year=2000, rating=8.0, vote_count=9),
            make_candidate(3, "unrated", year=2000, rating=8.5, vote_count=9),
            make_candidate(4, "low", year=2000, rating=3.0, vote_count=9),
        ]
        apply_limits(pool, RowLimits(max_runtime=120, min_year=1990, min_rating=7.0), tmdb)
        assert [c.args[0] for c in tmdb.details.call_args_list] == [2, 3]
        assert all(c.args[1] is MediaType.MOVIE for c in tmdb.details.call_args_list)

    def test_details_is_not_called_without_a_runtime_limit(self):
        tmdb = MagicMock()
        apply_limits([make_candidate(1, "A", year=2000)], RowLimits(min_year=1990), tmdb)
        tmdb.details.assert_not_called()


class TestRatingLookup:
    """A title with no votes (Trakt candidates arrive 0.0/0) is rated from TMDB details, never read as 0."""

    @staticmethod
    def _run(min_rating: float, candidate, details):
        tmdb = _tmdb({candidate.tmdb_id: details})
        return apply_limits([candidate], RowLimits(min_rating=min_rating), tmdb), tmdb

    def test_a_rated_candidate_needs_no_details_call(self):
        result, tmdb = self._run(6.0, make_candidate(1, "A", rating=7.0, vote_count=50), {})
        assert _ids(result) == [1]
        tmdb.details.assert_not_called()

    def test_an_unvoted_candidate_is_rated_from_details_and_kept(self):
        c = make_candidate(1, "A", rating=0.0, vote_count=0)
        result, tmdb = self._run(6.0, c, {"vote_average": 7.2, "vote_count": 90})
        assert _ids(result) == [1]
        assert result.unknown == 0
        tmdb.details.assert_called_once_with(1, MediaType.MOVIE)

    def test_an_unvoted_candidate_is_dropped_when_details_rate_it_too_low(self):
        c = make_candidate(1, "A", rating=0.0, vote_count=0)
        result, tmdb = self._run(8.0, c, {"vote_average": 7.2, "vote_count": 90})
        assert _ids(result) == []
        assert result.dropped == 1
        tmdb.details.assert_called_once_with(1, MediaType.MOVIE)

    def test_a_title_with_no_votes_even_in_details_is_unrated_and_dropped(self):
        c = make_candidate(1, "A", rating=0.0, vote_count=0)
        result, _ = self._run(6.0, c, {"vote_average": 0.0, "vote_count": 0})
        assert _ids(result) == []
        assert result.dropped == 1

    def test_a_details_failure_keeps_the_title_as_unknown(self):
        c = make_candidate(1, "A", rating=0.0, vote_count=0)
        result, tmdb = self._run(6.0, c, RuntimeError("boom"))
        assert _ids(result) == [1]
        assert result.unknown == 1
        tmdb.details.assert_called_once_with(1, MediaType.MOVIE)

    def test_one_details_call_serves_both_rating_and_runtime(self):
        c = make_candidate(1, "A", rating=0.0, vote_count=0)
        tmdb = _tmdb({1: {"vote_average": 7.0, "vote_count": 10, "runtime": 90}})
        result = apply_limits([c], RowLimits(min_rating=6.0, max_runtime=100), tmdb)
        assert _ids(result) == [1]
        assert tmdb.details.call_count == 1


class TestDetailsCircuitBreaker:
    def test_five_consecutive_failures_stop_further_calls_and_keep_the_rest(self):
        tmdb = MagicMock()
        tmdb.details.side_effect = RuntimeError("timeout")
        pool = [make_candidate(i, str(i)) for i in range(1, 11)]
        result = apply_limits(pool, RowLimits(max_runtime=120), tmdb)
        assert tmdb.details.call_count == 5
        assert _ids(result) == list(range(1, 11))
        assert result.unknown == 10

    def test_a_success_resets_the_streak(self):
        details = {i: RuntimeError("x") for i in range(1, 11)}
        details[5] = {"runtime": 90}
        tmdb = _tmdb(details)
        pool = [make_candidate(i, str(i)) for i in range(1, 11)]
        apply_limits(pool, RowLimits(max_runtime=120), tmdb)
        assert tmdb.details.call_count == 10

    def test_one_warning_with_counts_is_logged(self):
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            tmdb = MagicMock()
            tmdb.details.side_effect = RuntimeError("timeout")
            apply_limits([make_candidate(i, str(i)) for i in range(1, 9)], RowLimits(max_runtime=120), tmdb)
        finally:
            logger.remove(sink)
        assert len(messages) == 1
        assert "5 TMDB details lookups failed" in messages[0]
        assert "8 titles kept as unknown" in messages[0]


class TestRuntimeMinutes:
    def test_movie_reads_runtime(self):
        assert runtime_minutes({"runtime": 95}, "movie") == 95

    def test_tv_falls_back_to_last_episode(self):
        assert runtime_minutes({"last_episode_to_air": {"runtime": 42}}, "show") == 42

    def test_nothing_usable_is_none(self):
        assert runtime_minutes({}, "movie") is None
        assert runtime_minutes({"runtime": None}, "movie") is None


class TestPassesYearAndRating:
    """The free half of the limits, for titles a row will never fetch details for."""

    def test_judges_year_and_rating_and_keeps_what_it_cannot_judge(self):
        from shortlist.engine.limits import passes_year_and_rating

        limits = RowLimits(max_year=2000, min_rating=7.0)
        assert passes_year_and_rating(make_candidate(1, "ok", rating=8.0, year=1999, vote_count=50), limits)
        assert not passes_year_and_rating(make_candidate(2, "new", rating=8.0, year=2024, vote_count=50), limits)
        assert not passes_year_and_rating(make_candidate(3, "bad", rating=5.0, year=1999, vote_count=50), limits)
        assert passes_year_and_rating(make_candidate(4, "no year", rating=8.0, year=None, vote_count=50), limits)
        assert passes_year_and_rating(make_candidate(5, "no votes", rating=0.0, year=1999, vote_count=0), limits)

    def test_ignores_runtime(self):
        from shortlist.engine.limits import passes_year_and_rating

        assert passes_year_and_rating(make_candidate(1, "long", year=1999), RowLimits(max_runtime=60))


class TestSkippedCountUnderThreads:
    def test_every_skipped_lookup_is_counted_when_threads_race(self):
        from concurrent.futures import ThreadPoolExecutor

        from shortlist.engine.limits import _MAX_CONSECUTIVE_FAILURES, _DetailsFetcher

        fetcher = _DetailsFetcher(MagicMock())
        fetcher._streak = _MAX_CONSECUTIVE_FAILURES  # the breaker is open: every get is skipped
        candidate = make_candidate(1, "1")

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda _: fetcher.get(candidate), range(4000)))

        assert fetcher.skipped == 4000
