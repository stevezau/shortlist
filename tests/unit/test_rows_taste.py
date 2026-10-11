"""Which rows tell the AI web search about a person's whole history (#152), and which keep sending what they did."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from shortlist.engine.models import MediaType, RowOverride, RowSpec, WatchedItem
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


def _policy(*, sources=("llm_web",), favourite_count=4, older_count=0, **cfg):
    policy = make_policy(
        sources=list(sources),
        cfg_overrides={
            "favourite_count": favourite_count,
            "older_count": older_count,
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
        taste, favourites, _ = _policy(favourite_count=0).taste_for(_row())
        assert taste.wide is False and taste.text.startswith("Recently watched (most recent first):")
        assert favourites == []

    def test_a_single_seed_row_sends_the_recent_list_even_in_wide_mode(self):
        taste, favourites, _ = _policy().taste_for(_row(max_seeds=1))
        assert taste.wide is False and favourites == []

    def test_a_row_without_ai_web_search_sends_none(self):
        taste, favourites, _ = _policy(sources=("tmdb_similar",)).taste_for(_row())
        assert taste is None and favourites == []

    def test_the_recent_list_leaves_out_a_dont_seed_title(self):
        policy = _policy(favourite_count=0)
        policy.user.blocked_seeds = {100}
        text = policy.taste_for(_row()).taste.text
        assert "Film 0 " not in text and "Film 1 " in text

    def test_a_library_pinned_row_lists_only_its_own_titles(self):
        policy = _policy(favourite_count=0)
        history = policy.user.history
        for item in history[:3]:
            object.__setattr__(item, "rating_key", 1000 + item.tmdb_id)
        for item in history[3:]:
            object.__setattr__(item, "rating_key", 2000 + item.tmdb_id)
        policy.ctx.section_index = {"1": {item.tmdb_id: item.rating_key for item in history[:3]}, "2": {}}
        text = policy.taste_for(_row(library_keys=["1"])).taste.text
        assert text.count("\n- ") == 3
        assert "Film 0 " in text and "Film 3 " not in text

    def test_wide_mode_renders_the_profile_and_resolves_the_favourites(self):
        taste, favourites, _ = _policy().taste_for(_row())
        assert taste.wide is True
        assert taste.text.startswith("What this person has watched.")
        assert "Film 15 (2015) - film, watched 3 times" in taste.text
        assert [(f.tmdb_id, f.title) for f in favourites] == [(115, "Film 15")]

    def test_it_is_built_once_per_media_and_libraries(self):
        policy = _policy()
        assert policy.taste_for(_row()) is policy.taste_for(_row())

    def test_zero_counts_in_the_config_send_the_recent_list(self):
        taste, favourites, older = _policy(favourite_count=0, older_count=0).taste_for(_row())
        assert taste.wide is False and favourites == [] and older == []


class TestPoolKey:
    def test_a_row_without_ai_web_search_keeps_its_key_in_either_mode(self):
        wide = _policy(sources=("tmdb_similar",))
        recent = _policy(sources=("tmdb_similar",), favourite_count=0)
        assert wide.pool_key(_row()) == recent.pool_key(_row())

    def test_zero_counts_keep_the_key_a_row_always_had(self):
        assert _policy(favourite_count=0, older_count=0).pool_key(_row()) == _policy(
            favourite_count=0, older_count=0, recent_count=10
        ).pool_key(_row())

    def test_the_mix_splits_the_key_by_each_count(self):
        keys = {
            _policy(favourite_count=0, older_count=0).pool_key(_row()),
            _policy(favourite_count=4, older_count=0).pool_key(_row()),
            _policy(favourite_count=8, older_count=0).pool_key(_row()),
            _policy(favourite_count=0, older_count=4).pool_key(_row()),
            _policy(favourite_count=4, older_count=4).pool_key(_row()),
        }
        assert len(keys) == 5

    def test_a_single_seed_row_keeps_the_recent_key_whatever_the_counts(self):
        single = _row(max_seeds=1)
        assert _policy().pool_key(single) == _policy(favourite_count=0).pool_key(single)


class TestWhatReachesTheGather:
    @pytest.mark.parametrize(
        ("counts", "expects_taste"),
        [({}, False), ({"favourite_count": 3}, True), ({"older_count": 3}, True)],
    )
    def test_the_native_curator_is_handed_a_taste_only_when_a_count_is_set(self, counts, expects_taste):
        curator = _TasteCurator()
        policy = make_policy(
            sources=["llm_web"],
            cfg_overrides={**counts, "web_search_provider": "native"},
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
            sources=["llm_web"], cfg_overrides={"favourite_count": 4, "web_search_provider": "native"}, curator=curator
        )
        policy.user.history = _history()
        spec = _row(max_seeds=1)
        policy.specs = [spec]
        policy.pools_for(spec)
        assert curator.calls == [{}]


class TestHistoryMixCounts:
    def test_a_persons_override_beats_the_row_and_the_row_beats_the_server(self):
        policy = _policy(favourite_count=1, older_count=2)
        row = _row()
        assert (policy.effective_favourite_count(row), policy.effective_older_count(row)) == (1, 2)
        row = _row(favourite_count=5, older_count=0)
        assert (policy.effective_favourite_count(row), policy.effective_older_count(row)) == (5, 0)
        policy.user.row_overrides["r"] = RowOverride(favourite_count=7, older_count=3)
        assert (policy.effective_favourite_count(row), policy.effective_older_count(row)) == (7, 3)

    def test_an_override_of_zero_is_a_choice_not_an_absence(self):
        policy = _policy(favourite_count=4, older_count=4)
        policy.user.row_overrides["r"] = RowOverride(favourite_count=0, older_count=0)
        assert policy.uses_wide_taste(_row()) is False

    def test_either_count_alone_widens_the_taste(self):
        assert _policy(favourite_count=0, older_count=3).uses_wide_taste(_row()) is True
        assert _policy(favourite_count=3, older_count=0).uses_wide_taste(_row()) is True

    def test_older_watches_become_seeds_to_search(self):
        _, _, older = _policy(favourite_count=0, older_count=3).taste_for(_row())
        assert len(older) == 3 and "Film 15" not in {s.title for s in older}

    def test_rows_with_different_counts_are_built_apart(self):
        policy = _policy(favourite_count=0, older_count=0)
        assert policy.taste_for(_row(older_count=3)) is not policy.taste_for(_row())

    def test_a_row_override_of_the_count_splits_the_pool_key_from_the_server_default(self):
        policy = _policy(favourite_count=0, older_count=0)
        assert policy.pool_key(_row(older_count=3)) != policy.pool_key(_row())

    def test_the_counts_reach_the_gather(self):
        policy = _policy(favourite_count=2, older_count=3, web_search_provider="exa")
        policy.specs = [spec := _row()]
        with patch("shortlist.engine.rows._gather_pool", side_effect=RuntimeError("stop")) as gather:
            policy.pools_for(spec)
        kwargs = gather.call_args.kwargs
        assert kwargs["favourite_count"] == 2 and kwargs["older_count"] == 3
        assert kwargs["taste"].wide and kwargs["older_seeds"] and kwargs["favourite_seeds"]


class TestSharedRowsNeverWiden:
    def test_a_shared_row_reads_no_one_persons_history_for_the_ai(self):
        """A shared row is a tally of what the server's people watched. It builds no `RowPolicy`, so it asks
        for no taste, favourites, older watches or ratings, whatever the counts are set to."""
        import inspect

        from shortlist.engine import rows

        source = inspect.getsource(rows._shared_row) + inspect.getsource(rows._run_shared)
        for name in ("taste_for(", "history_mix(", "_gather_pool(", "RowPolicy(", "favourite_count", "older_count"):
            assert name not in source
