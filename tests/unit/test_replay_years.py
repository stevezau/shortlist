"""The replay reports release years: of the pool it ranked, of each row title, and summarised (#174)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from shortlist.engine.eval.replay import holdout_cases, replay_case, year_stats
from shortlist.engine.models import EngineConfig, MediaType, UserProfile, UserType, WatchedItem

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def _watched(title: str, days_ago: int, tmdb_id: int) -> WatchedItem:
    return WatchedItem(
        title=title, media_type=MediaType.MOVIE, watched_at=NOW - timedelta(days=days_ago), tmdb_id=tmdb_id
    )


def _replay(row_size: int):
    """Four similar titles, released 1975, 1995, 2015 and undated, all in the library."""
    dates = {7001: "1975-05-01", 7002: "1995-05-01", 7003: "2015-05-01", 7004: ""}
    tmdb = SimpleNamespace()
    tmdb.suggestions = lambda tid, mt: [
        ({"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 7.0, "release_date": d}, 0.8)
        for i, d in dates.items()
    ]
    tmdb.genre_names = lambda mt: {}
    tmdb.genre_ids_for = lambda tid, mt: []
    user = UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)
    history = [_watched("Held", 0, 1), _watched("Earlier", 5, 2)]
    config = EngineConfig(row_size=row_size, candidates_pre_rank=20, max_seeds=2, candidate_sources=["tmdb_similar"])
    return replay_case(
        holdout_cases(user, history)[0],
        config,
        config_label="x",
        tmdb=tmdb,
        library_index={MediaType.MOVIE: dict.fromkeys(dates, 1), MediaType.SHOW: {}},
        resolve_tmdb_id=lambda item: item.tmdb_id,
    )


def test_the_pool_years_are_those_of_every_ranked_candidate_not_just_the_row():
    outcome = _replay(row_size=1)
    assert len(outcome.final_row) == 1
    assert sorted(outcome.pool_years, key=lambda y: (y is None, y)) == [1975, 1995, 2015, None]


def test_a_row_title_carries_its_release_year():
    outcome = _replay(row_size=4)
    assert {t.tmdb_id: t.year for t in outcome.final_row} == {7001: 1975, 7002: 1995, 7003: 2015, 7004: None}


def test_year_stats_summarises_dated_and_undated_titles():
    stats = year_stats([1975, 1985, 1995, 2005, 2015, None, None, None])
    assert stats.count == 8
    assert stats.median == 1995
    assert stats.pct_before_2000 == 3 / 8
    assert stats.pct_before_1990 == 2 / 8
    assert stats.pct_undated == 3 / 8


def test_year_stats_of_nothing_is_empty_not_an_error():
    stats = year_stats([])
    assert (stats.count, stats.median, stats.pct_before_2000, stats.pct_undated) == (0, None, 0.0, 0.0)
