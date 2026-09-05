"""`shortlist/engine/eval/replay.py`: the ruler has to be straight before it can measure anything.

These tests do not check whether the recommendation engine is good. They check that the harness
cannot lie about it — which is the only thing a test can establish here, and the thing that matters
most, because a leaking harness produces confident numbers that are entirely wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shortlist.engine.eval.replay import Comparison, ReplayOutcome, holdout_cases, replay_case, summarise
from shortlist.engine.models import (
    Candidate,
    EngineConfig,
    MediaType,
    Seed,
    UserProfile,
    UserType,
    WatchedItem,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def watched(title: str, days_ago: int, tmdb_id: int, *, rating: float | None = None) -> WatchedItem:
    return WatchedItem(
        title=title,
        media_type=MediaType.MOVIE,
        watched_at=NOW - timedelta(days=days_ago),
        tmdb_id=tmdb_id,
        user_rating=rating,
    )


def candidate(tmdb_id: int, *, title: str = "c", rating: float = 8.0) -> Candidate:
    return Candidate(
        tmdb_id=tmdb_id,
        title=title,
        media_type=MediaType.MOVIE,
        rating=rating,
        seeds=[Seed(tmdb_id=1, title="seed", media_type=MediaType.MOVIE, weight=1.0)],
    )


@pytest.fixture
def user() -> UserProfile:
    return UserProfile(username="mike", plex_account_id=202, user_type=UserType.SHARED)


@pytest.fixture
def config() -> EngineConfig:
    return EngineConfig(row_size=3, candidates_pre_rank=10, max_seeds=5)


class TestHoldoutCases:
    def test_a_later_watch_never_leaks_into_an_earlier_cases_seeds(self, user):
        """The trap that silently inflates every number.

        Testing several of one person's recent watches by removing ONE item from the whole list
        leaves every LATER watch in place — so the earliest case is seeded with four watches that had
        not happened yet, and the harness reports a future it was shown.
        """
        history = [watched(f"m{i}", days_ago=i, tmdb_id=100 + i) for i in range(5)]

        cases = holdout_cases(user, history, max_holdouts=5)

        for case in cases:
            assert all(h.watched_at < case.held_out.watched_at for h in case.history_before), (
                f"{case.held_out.title} was seeded with a watch from its own future"
            )

    def test_the_held_out_title_is_never_in_its_own_history(self, user):
        history = [watched(f"m{i}", days_ago=i, tmdb_id=100 + i) for i in range(4)]

        for case in holdout_cases(user, history):
            assert case.held_out not in case.history_before

    def test_it_takes_the_most_recent_watches(self, user):
        history = [watched(f"m{i}", days_ago=i, tmdb_id=100 + i) for i in range(10)]

        cases = holdout_cases(user, history, max_holdouts=3)

        assert [c.held_out.tmdb_id for c in cases] == [100, 101, 102]

    def test_a_watch_with_no_tmdb_id_cannot_be_a_case(self, user):
        """Nothing can be looked for that has no id to look for."""
        history = [
            WatchedItem(title="unmatched", media_type=MediaType.MOVIE, watched_at=NOW, tmdb_id=None),
            watched("known", days_ago=1, tmdb_id=100),
        ]

        assert [c.held_out.tmdb_id for c in holdout_cases(user, history)] == [100]

    def test_an_empty_history_produces_no_cases(self, user):
        assert holdout_cases(user, []) == []

    def test_a_single_watch_case_has_an_empty_history(self, user):
        """Degenerate but legitimate — it must not raise, and it must not borrow from elsewhere."""
        cases = holdout_cases(user, [watched("only", days_ago=0, tmdb_id=100)])

        assert len(cases) == 1
        assert cases[0].history_before == []


class TestReplayCase:
    """The `dropped` list and the pool are supplied by fakes so these test the harness, not TMDB."""

    def _tmdb(self, monkeypatch, pool: list[Candidate]) -> object:
        monkeypatch.setattr(
            "shortlist.engine.eval.replay.candidates_mod.gather_candidates",
            lambda *a, **k: list(pool),
        )
        monkeypatch.setattr(
            "shortlist.engine.eval.replay.history_mod.derive_seeds",
            lambda *a, **k: [Seed(tmdb_id=1, title="seed", media_type=MediaType.MOVIE, weight=1.0)],
        )
        return object()

    def _case(self, user):
        history = [watched(f"m{i}", days_ago=i + 1, tmdb_id=200 + i) for i in range(3)]
        held = watched("target", days_ago=0, tmdb_id=999)
        return holdout_cases(user, [held, *history])[0]

    def test_the_held_out_title_is_not_excluded_as_already_watched(self, user, config, monkeypatch):
        """THE leakage trap.

        `filter_candidates`' watched-exclusion set must be built from the reduced history. Built from
        the full history it contains the held-out title, so every case is dropped as
        "already_watched" — a permanent 0% hit rate that reads as "the algorithm never works" rather
        than "the harness is broken", which is exactly the kind of wrong number nobody questions.
        """
        case = self._case(user)
        tmdb = self._tmdb(monkeypatch, [candidate(999, title="target")])
        index = {MediaType.MOVIE: {999: 1}, MediaType.SHOW: {}}

        outcome = replay_case(
            case, config, config_label="x", tmdb=tmdb, library_index=index, resolve_tmdb_id=lambda *_: None
        )

        assert outcome.drop_reason != "already_watched", "the harness hid the title it was looking for"
        assert outcome.rank == 1
        assert outcome.hit is True

    def test_a_title_the_gather_never_proposed_is_reported_as_not_gathered(self, user, config, monkeypatch):
        case = self._case(user)
        tmdb = self._tmdb(monkeypatch, [candidate(111), candidate(222)])
        index = {MediaType.MOVIE: {111: 1, 222: 1}, MediaType.SHOW: {}}

        outcome = replay_case(
            case, config, config_label="x", tmdb=tmdb, library_index=index, resolve_tmdb_id=lambda *_: None
        )

        assert outcome.gathered is False
        assert outcome.rank is None
        assert outcome.reciprocal_rank == 0.0
        assert outcome.hit is False

    def test_gathered_but_filtered_out_is_distinguishable_from_never_gathered(self, user, config, monkeypatch):
        """Three different failures — never proposed, proposed then filtered, proposed and ranked
        badly — must not collapse into one bucket, or a gather regression is indistinguishable from
        a ranking one."""
        case = self._case(user)
        tmdb = self._tmdb(monkeypatch, [candidate(999, title="target")])
        index = {MediaType.MOVIE: {}, MediaType.SHOW: {}}  # not in the library

        outcome = replay_case(
            case, config, config_label="x", tmdb=tmdb, library_index=index, resolve_tmdb_id=lambda *_: None
        )

        assert outcome.gathered is True, "it WAS proposed"
        assert outcome.drop_reason == "not_in_your_libraries"
        assert outcome.hit is False

    def test_pool_size_is_reported_so_a_rank_can_be_interpreted(self, user, config, monkeypatch):
        """9th of 12 and 9th of 400 are different answers, and a config that changes the pool changes
        what a rank is worth."""
        case = self._case(user)
        pool = [candidate(300 + i, rating=9.0 - i * 0.1) for i in range(8)] + [candidate(999, rating=1.0)]
        tmdb = self._tmdb(monkeypatch, pool)
        index = {MediaType.MOVIE: {c.tmdb_id: 1 for c in pool}, MediaType.SHOW: {}}

        outcome = replay_case(
            case, config, config_label="x", tmdb=tmdb, library_index=index, resolve_tmdb_id=lambda *_: None
        )

        assert outcome.pool_size == len(pool)
        assert outcome.rank == len(pool), "the worst-rated candidate should rank last"

    def test_reciprocal_rank_matches_the_rank(self, user, config, monkeypatch):
        case = self._case(user)
        pool = [candidate(300, rating=9.0), candidate(999, rating=5.0)]
        tmdb = self._tmdb(monkeypatch, pool)
        index = {MediaType.MOVIE: {300: 1, 999: 1}, MediaType.SHOW: {}}

        outcome = replay_case(
            case, config, config_label="x", tmdb=tmdb, library_index=index, resolve_tmdb_id=lambda *_: None
        )

        assert outcome.rank == 2
        assert outcome.reciprocal_rank == pytest.approx(0.5)

    def test_it_reports_whether_ratings_could_be_trusted(self, user, config, monkeypatch):
        """`dislike_threshold` does nothing for an account whose ratings are mostly tool-written, and
        a flat result there means "not applicable", not "the change did nothing"."""
        case = self._case(user)
        tmdb = self._tmdb(monkeypatch, [candidate(999)])
        index = {MediaType.MOVIE: {999: 1}, MediaType.SHOW: {}}

        outcome = replay_case(
            case, config, config_label="x", tmdb=tmdb, library_index=index, resolve_tmdb_id=lambda *_: None
        )

        assert isinstance(outcome.ratings_trusted, bool)


class TestSummarise:
    def _outcome(self, rr: float, label: str) -> ReplayOutcome:
        case = holdout_cases(
            UserProfile(username="u", plex_account_id=1, user_type=UserType.SHARED),
            [watched("a", days_ago=0, tmdb_id=1), watched("b", days_ago=1, tmdb_id=2)],
            max_holdouts=1,
        )[0]
        return ReplayOutcome(
            case=case,
            config_label=label,
            gathered=rr > 0,
            rank=int(1 / rr) if rr else None,
            pool_size=10,
            in_final_row=rr >= 0.5,
            reciprocal_rank=rr,
        )

    def test_a_marginal_split_is_reported_as_noise_not_a_result(self):
        """The number this harness most easily produces at N=40 is a meaningless 51/49, and a bare
        "+0.03 MRR" would be read as a win."""
        a = [self._outcome(0.5, "a") for _ in range(10)]
        b = [self._outcome(1.0, "b") for _ in range(5)] + [self._outcome(0.25, "b") for _ in range(5)]

        text = summarise(Comparison("a", "b", a, b))

        assert "noise" in text.lower()

    def test_a_consistent_direction_is_reported_as_worth_acting_on(self):
        a = [self._outcome(0.25, "a") for _ in range(10)]
        b = [self._outcome(1.0, "b") for _ in range(9)] + [self._outcome(0.2, "b")]

        text = summarise(Comparison("a", "b", a, b))

        assert "worth acting on" in text.lower()
        assert "per-case table" in text.lower(), "even a good result must point at the evidence"

    def test_all_ties_says_nothing_measurable_happened(self):
        a = [self._outcome(0.5, "a") for _ in range(6)]
        b = [self._outcome(0.5, "b") for _ in range(6)]

        assert "nothing measurable" in summarise(Comparison("a", "b", a, b)).lower()

    def test_no_cases_does_not_divide_by_zero(self):
        assert "nothing to summarise" in summarise(Comparison("a", "b", [], [])).lower()
