"""The replay can now run the AI (#152): it must show the model only what preceded the held-out watch."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from shortlist.engine.clients.search import SearchResult, TitleCandidate
from shortlist.engine.eval.replay import (
    ReplayOutcome,
    first_watch_keep,
    furthest_stage,
    holdout_cases,
    paired_picked,
    replay_case,
    summarise_picked,
)
from shortlist.engine.models import EngineConfig, MediaType, UserProfile, UserType, WatchedItem

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def watched(title, days_ago, tmdb_id, *, plays=1, media=MediaType.MOVIE) -> WatchedItem:
    return WatchedItem(
        title=title,
        media_type=media,
        watched_at=NOW - timedelta(days=days_ago),
        tmdb_id=tmdb_id,
        watch_count=plays,
    )


@pytest.fixture
def user() -> UserProfile:
    return UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)


class TestHoldoutFilter:
    def test_the_filter_applies_before_the_cap(self, user):
        history = [watched(f"m{i}", i, 100 + i) for i in range(6)]
        cases = holdout_cases(user, history, max_holdouts=2, keep=lambda w: w.title not in {"m0", "m1"})
        assert [c.held_out.title for c in cases] == ["m2", "m3"]

    def test_a_movie_rewatch_is_never_a_case(self, user):
        history = [watched("again", 0, 1, plays=3), watched("first", 1, 2)]
        assert [c.held_out.title for c in holdout_cases(user, history)] == ["first"]

    def test_a_show_with_many_episodes_can_still_be_a_case(self, user):
        history = [watched("series", 0, 1, plays=12, media=MediaType.SHOW)]
        assert len(holdout_cases(user, history)) == 1

    def test_the_cases_with_no_filter_are_what_they_were(self, user):
        history = [watched(f"m{i}", i, 100 + i) for i in range(4)]
        assert [c.held_out.title for c in holdout_cases(user, history, max_holdouts=3)] == ["m0", "m1", "m2"]


class _Search:
    name = "exa"
    results_per_query = 10

    def __init__(self, titles):
        self._titles = titles
        self.queries: list[str] = []

    def search(self, query, *, num_results=8):
        self.queries.append(query)
        return [SearchResult(title="An article", url="https://example.com", text="body")]

    def search_detailed(self, query, *, num_results=8):
        return self.search(query, num_results=num_results), list(self._titles)


class _Curator:
    supports_native_web_search = False
    last_tokens = 0
    last_output_tokens = 0

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, system, user):
        self.prompts += [system, user]
        self.last_tokens = 50
        return self.reply


class _Cache:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


def _tmdb():
    tmdb = SimpleNamespace()
    tmdb.suggestions = lambda tid, mt: []
    tmdb.genre_names = lambda mt: {}
    tmdb.search = lambda title, mt, year=None: (
        {"id": 999, "title": title, "genre_ids": [], "vote_average": 8.0} if title == "Target" else None
    )
    return tmdb


def _case(user):
    history = [watched(f"Earlier {i}", i + 1, 200 + i) for i in range(4)]
    held = watched("Target", 0, 999)
    return holdout_cases(user, [held, *history])[0]


def _replay(user, curator, search, *, favourite_count=2, older_count=0, cache=None):
    config = EngineConfig(
        row_size=3,
        candidates_pre_rank=10,
        max_seeds=5,
        candidate_sources=["llm_web"],
        web_search_provider="exa",
        favourite_count=favourite_count,
        older_count=older_count,
    )
    return replay_case(
        _case(user),
        config,
        config_label="x",
        tmdb=_tmdb(),
        library_index={MediaType.MOVIE: {999: 1}, MediaType.SHOW: {}},
        resolve_tmdb_id=lambda item: item.tmdb_id,
        curator=curator,
        search=search,
        web_search_cache=cache,
    )


class TestTheAiSeesOnlyThePast:
    @pytest.mark.parametrize("counts", [(0, 0), (2, 0), (0, 2), (2, 2)])
    def test_the_held_out_title_is_in_no_prompt_and_no_query(self, user, counts):
        curator = _Curator("[]")
        search = _Search([TitleCandidate(title="Target", year=2025, media="movie")])
        _replay(user, curator, search, favourite_count=counts[0], older_count=counts[1])
        assert curator.prompts
        assert all("Target" not in p.replace("- Target (2025) [movie]", "") for p in curator.prompts)
        assert all("Target" not in q for q in search.queries)
        assert any("Earlier 0" in p for p in curator.prompts)

    def test_found_by_search_is_separate_from_picked_by_the_ai(self, user):
        search = _Search([TitleCandidate(title="Target", year=2025, media="movie")])
        # An empty reply would fall back to the whole extraction (as a run does), so the AI picks something else.
        found_only = _replay(user, _Curator('[{"title": "Other", "year": 2025, "media": "movie"}]'), search)
        assert found_only.found_by_search is True
        assert found_only.ai_picked is False
        picked = _replay(user, _Curator('[{"title": "Target", "year": 2025, "media": "movie"}]'), search)
        assert picked.found_by_search and picked.ai_picked and picked.gathered

    def test_a_title_the_search_never_found_is_neither(self, user):
        search = _Search([TitleCandidate(title="Something else", year=2025, media="movie")])
        outcome = _replay(user, _Curator("[]"), search)
        assert (outcome.found_by_search, outcome.ai_picked) == (False, False)

    def test_tokens_and_new_searches_are_reported(self, user):
        search = _Search([TitleCandidate(title="Target", year=2025, media="movie")])
        cache = _Cache()
        first = _replay(user, _Curator("[]"), search, cache=cache)
        assert first.tokens == 50
        assert first.new_searches == len(search.queries) > 0
        again = _replay(user, _Curator("[]"), search, cache=cache)
        assert again.new_searches == 0


class TestFoundByGroup:
    """A history deep enough that all three groups have titles: 12 recent, 3 favourites, 20 older."""

    def _outcome(self, user, titles, *, favourite_count=2, older_count=2):
        history = [watched(f"Recent {i}", i + 1, 300 + i) for i in range(12)]
        history += [watched(f"Fav {i}", 100 + i, 400 + i, plays=3) for i in range(3)]
        history += [watched(f"Old {i}", 200 + i, 500 + i) for i in range(20)]
        case = holdout_cases(user, [watched("Target", 0, 999), *history])[0]
        config = EngineConfig(
            row_size=3,
            candidates_pre_rank=10,
            max_seeds=5,
            candidate_sources=["llm_web"],
            web_search_provider="exa",
            favourite_count=favourite_count,
            older_count=older_count,
        )
        return replay_case(
            case,
            config,
            config_label="x",
            tmdb=_tmdb(),
            library_index={MediaType.MOVIE: {999: 1}, MediaType.SHOW: {}},
            resolve_tmdb_id=lambda item: item.tmdb_id,
            curator=_Curator("[]"),
            search=_Search(titles),
        )

    def test_each_kind_reports_which_of_its_searches_held_the_title(self, user):
        outcome = self._outcome(user, [TitleCandidate(title="Target", year=2025, media="movie")])
        assert outcome.found_by_group == {"recent": list(range(1, 6)), "favourite": [1, 2], "older": [1, 2]}

    def test_a_title_no_search_found_is_in_no_group(self, user):
        outcome = self._outcome(user, [TitleCandidate(title="Something else", year=2025, media="movie")])
        assert outcome.found_by_group == {}

    def test_with_both_counts_at_zero_only_recent_searches_run(self, user):
        outcome = self._outcome(
            user, [TitleCandidate(title="Target", year=2025, media="movie")], favourite_count=0, older_count=0
        )
        assert set(outcome.found_by_group) == {"recent"}


class TestFirstWatchFilter:
    def _show(self, key, days_ago=0):
        return WatchedItem(
            title="s", media_type=MediaType.SHOW, watched_at=NOW - timedelta(days=days_ago), tmdb_id=1, rating_key=key
        )

    def test_a_show_started_long_before_is_not_a_discovery(self):
        keep = first_watch_keep({7: NOW - timedelta(days=30)})
        assert keep(self._show(7)) is False

    def test_a_show_first_played_within_a_day_is_a_discovery(self):
        keep = first_watch_keep({7: NOW - timedelta(hours=20)})
        assert keep(self._show(7)) is True

    def test_no_recorded_first_play_keeps_it(self):
        assert first_watch_keep({})(self._show(7)) is True
        assert first_watch_keep({})(self._show(None)) is True


class TestPairedPicked:
    def _o(self, picked):
        return ReplayOutcome(case=None, config_label="x", gathered=picked, ai_picked=picked)

    def test_counts_and_stage(self):
        before = [self._o(False), self._o(True), self._o(False)]
        after = [self._o(True), self._o(True), self._o(False)]
        assert paired_picked(before, after) == (1, 0, 2)
        assert furthest_stage(self._o(True)) == "AI picked"
        assert furthest_stage(self._o(False)) == "not found"

    def test_a_split_is_noise(self):
        before = [self._o(False), self._o(True)]
        after = [self._o(True), self._o(False)]
        assert "noise" in summarise_picked("A", "B", before, after)


def test_a_movie_seen_before_in_another_library_is_not_a_discovery(user):
    """Each library's copy carries its own play count, so one play in each is still a rewatch."""
    history = [watched("Twice", 1, 9), watched("Twice", 300, 9), watched("New", 2, 10)]
    cases = holdout_cases(user, history, max_holdouts=5)
    # The copy played last year was a discovery then; the one played yesterday is a rewatch.
    assert [(c.held_out.title, c.held_out.watched_at) for c in cases] == [
        ("New", NOW - timedelta(days=2)),
        ("Twice", NOW - timedelta(days=300)),
    ]
