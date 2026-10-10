"""Which rows tell the AI web search about a person's whole history (#152), and which keep sending what they did."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shortlist.engine.models import MediaType, RowSpec, WatchedItem
from tests.unit.test_row_guidance import _NativeCurator, make_policy

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _history() -> list[WatchedItem]:
    return [
        WatchedItem(
            title=f"Film {n}",
            media_type=MediaType.MOVIE,
            watched_at=NOW - timedelta(days=n),
            tmdb_id=100 + n,
            year=2000 + n,
            watch_count=3 if n == 15 else 1,
        )
        for n in range(20)
    ]


def _policy(*, sources=("llm_web",), taste_mode="wide", favourite_count=4, **cfg):
    policy = make_policy(
        sources=list(sources),
        cfg_overrides={
            "taste_mode": taste_mode,
            "favourite_count": favourite_count,
            "web_search_provider": "native",
            **cfg,
        },
    )
    policy.user.history = _history()
    return policy


def _row(**kw) -> RowSpec:
    return RowSpec(slug="r", name_template="R", size=5, **kw)


class _TasteCurator(_NativeCurator):
    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    def recommend_web(self, profile, seeds, k, *, guidance=None, **kwargs):
        self.calls.append(kwargs)
        return []


class TestWhoGetsATasteProfile:
    def test_recent_mode_sends_the_recent_list_not_the_wide_profile(self):
        taste, favourites = _policy(taste_mode="recent").taste_for(_row())
        assert taste.wide is False and taste.text.startswith("Recently watched (most recent first):")
        assert favourites == []

    def test_a_single_seed_row_sends_the_recent_list_even_in_wide_mode(self):
        taste, favourites = _policy().taste_for(_row(max_seeds=1))
        assert taste.wide is False and favourites == []

    def test_a_row_without_ai_web_search_sends_none(self):
        taste, favourites = _policy(sources=("tmdb_similar",)).taste_for(_row())
        assert taste is None and favourites == []

    def test_the_recent_list_leaves_out_a_dont_seed_title(self):
        policy = _policy(taste_mode="recent")
        policy.user.blocked_seeds = {100}
        text = policy.taste_for(_row())[0].text
        assert "Film 0 " not in text and "Film 1 " in text

    def test_a_library_pinned_row_lists_only_its_own_titles(self):
        policy = _policy(taste_mode="recent")
        history = policy.user.history
        for item in history[:3]:
            object.__setattr__(item, "rating_key", 1000 + item.tmdb_id)
        for item in history[3:]:
            object.__setattr__(item, "rating_key", 2000 + item.tmdb_id)
        policy.ctx.section_index = {"1": {item.tmdb_id: item.rating_key for item in history[:3]}, "2": {}}
        text = policy.taste_for(_row(library_keys=["1"]))[0].text
        assert text.count("\n- ") == 3
        assert "Film 0 " in text and "Film 3 " not in text

    def test_wide_mode_renders_the_profile_and_resolves_the_favourites(self):
        taste, favourites = _policy().taste_for(_row())
        assert taste.wide is True
        assert taste.text.startswith("What this person has watched.")
        assert "Film 15 (2015) - film, watched 3 times" in taste.text
        assert [(f.tmdb_id, f.title) for f in favourites] == [(115, "Film 15")]

    def test_it_is_built_once_per_media_and_libraries(self):
        policy = _policy()
        assert policy.taste_for(_row()) is policy.taste_for(_row())

    def test_the_recent_list_does_not_change_the_pool_key(self):
        with_web = _policy(taste_mode="recent", favourite_count=0)
        assert with_web.pool_key(_row()) == _policy(taste_mode="recent", favourite_count=3).pool_key(_row())


class TestPoolKey:
    def test_a_row_without_ai_web_search_keeps_its_key_in_either_mode(self):
        wide = _policy(sources=("tmdb_similar",))
        recent = _policy(sources=("tmdb_similar",), taste_mode="recent", favourite_count=0)
        assert wide.pool_key(_row()) == recent.pool_key(_row())

    def test_recent_mode_keeps_the_key_whatever_the_favourite_count(self):
        a = _policy(taste_mode="recent", favourite_count=0)
        b = _policy(taste_mode="recent", favourite_count=8)
        assert a.pool_key(_row()) == b.pool_key(_row())

    def test_wide_mode_splits_from_recent_and_by_favourite_count(self):
        recent = _policy(taste_mode="recent", favourite_count=0).pool_key(_row())
        wide0 = _policy(favourite_count=0).pool_key(_row())
        wide8 = _policy(favourite_count=8).pool_key(_row())
        assert len({recent, wide0, wide8}) == 3

    def test_a_single_seed_row_keeps_the_recent_key_in_wide_mode(self):
        single = _row(max_seeds=1)
        assert _policy().pool_key(single) == _policy(taste_mode="recent").pool_key(single)


class TestWhatReachesTheGather:
    @pytest.mark.parametrize(("taste_mode", "expects_taste"), [("recent", False), ("wide", True)])
    def test_the_native_curator_is_handed_a_taste_only_in_wide_mode(self, taste_mode, expects_taste):
        curator = _TasteCurator()
        policy = make_policy(
            sources=["llm_web"],
            cfg_overrides={"taste_mode": taste_mode, "web_search_provider": "native"},
            curator=curator,
        )
        policy.user.history = _history()
        spec = _row()
        policy.specs = [spec]
        policy.pools_for(spec)
        assert len(curator.calls) == 1
        assert ("taste" in curator.calls[0]) is expects_taste

    def test_a_single_seed_row_is_handed_no_taste_in_wide_mode(self):
        curator = _TasteCurator()
        policy = make_policy(
            sources=["llm_web"], cfg_overrides={"taste_mode": "wide", "web_search_provider": "native"}, curator=curator
        )
        policy.user.history = _history()
        spec = _row(max_seeds=1)
        policy.specs = [spec]
        policy.pools_for(spec)
        assert curator.calls == [{}]
