"""Measured genre avoidance and the single floored negative multiplier (Wave 3, items 1 and 2).

Both defaults are OFF, so every test here that does not set `genre_avoidance` is also asserting that
an existing install is untouched.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine import candidates as candidates_mod
from shortlist.engine import ranking
from shortlist.engine.models import Candidate, MediaType, Seed


class TestGenreAvoidanceProfile:
    """`candidates.genre_avoidance_profile` — Log Ratio with sample-size shrinkage."""

    def test_a_low_watch_count_user_is_pulled_toward_the_library(self):
        """Three watches is not evidence of a preference.

        Someone who has watched three horror films has a 100% raw horror share, which a bare log
        ratio reads as an overwhelming signal. Shrinkage says: with this little evidence, assume they
        look mostly like the library until they show otherwise.
        """
        pool = {"Horror": 100, "Drama": 100}
        raw = candidates_mod.genre_avoidance_profile({"Horror": 3}, pool)

        # Raw share is 1.0 vs a pool share of 0.5 — log2(2) = +1.0 unshrunk.
        assert 0.0 < raw["Horror"] < 0.4, "a 3-watch history was trusted like a long one"

    def test_a_full_history_is_trusted_much_more(self):
        pool = {"Horror": 100, "Drama": 100}
        small = candidates_mod.genre_avoidance_profile({"Horror": 3}, pool)["Horror"]
        large = candidates_mod.genre_avoidance_profile({"Horror": 30}, pool)["Horror"]

        assert large > small * 2, "more evidence did not buy more trust"

    def test_matching_the_library_exactly_is_no_signal(self):
        profile = candidates_mod.genre_avoidance_profile({"Horror": 50, "Drama": 50}, {"Horror": 100, "Drama": 100})

        assert profile["Horror"] == pytest.approx(0.0)
        assert profile["Drama"] == pytest.approx(0.0)

    def test_an_avoided_genre_reads_negative(self):
        profile = candidates_mod.genre_avoidance_profile({"Drama": 40}, {"Horror": 100, "Drama": 100})

        assert profile["Horror"] < 0, "a genre they never touch should read as avoided"

    def test_the_ratio_is_clamped_both_ways(self):
        """A genre with almost no presence in the library produces an unbounded ratio, which would
        let one sparse genre dominate every score it touches."""
        profile = candidates_mod.genre_avoidance_profile({"Rare": 500}, {"Rare": 1, "Drama": 100_000})

        assert profile["Rare"] <= candidates_mod.GENRE_LOG_CLAMP
        assert profile["Drama"] >= -candidates_mod.GENRE_LOG_CLAMP

    @given(
        user_n=st.integers(min_value=0, max_value=500),
        pool_a=st.integers(min_value=1, max_value=1000),
        pool_b=st.integers(min_value=1, max_value=1000),
    )
    def test_the_shrunk_share_always_lies_between_the_raw_and_the_pool(self, user_n, pool_a, pool_b):
        """The interpolation invariant. If shrinkage ever lands OUTSIDE the two values it is mixing,
        it is not shrinking — it is inventing a preference nobody expressed."""
        pool = {"A": pool_a, "B": pool_b}
        profile = candidates_mod.genre_avoidance_profile({"A": user_n}, pool)
        if not profile or user_n == 0:
            return
        pool_share = pool_a / (pool_a + pool_b)
        raw_share = 1.0
        implied = pool_share * (2 ** profile["A"])
        assert min(raw_share, pool_share) - 1e-9 <= implied <= max(raw_share, pool_share) + 1e-9

    def test_no_history_or_no_library_yields_no_opinion(self):
        assert candidates_mod.genre_avoidance_profile({}, {"Horror": 10}) == {}
        assert candidates_mod.genre_avoidance_profile({"Horror": 10}, {}) == {}


class TestCandidateGenrePenalty:
    def test_only_the_negative_half_counts(self):
        """Liking something is already expressed by the seeds that produced the candidate. If this
        returned a bonus too, a loved genre would be paid for twice."""
        profile = {"Horror": 1.5, "Drama": -1.0}

        assert candidates_mod.candidate_genre_penalty(["Horror"], profile) == 0.0
        assert candidates_mod.candidate_genre_penalty(["Drama"], profile) == -1.0

    def test_a_genre_the_library_does_not_stock_is_neutral(self):
        """Nothing to compare against is not evidence of avoidance."""
        assert candidates_mod.candidate_genre_penalty(["Unknown"], {"Horror": -1.0}) == 0.0

    def test_it_averages_rather_than_sums(self):
        """Summing would penalise a title for having MORE genres, which is a cataloguing artefact."""
        profile = {"A": -1.0, "B": -1.0}

        assert candidates_mod.candidate_genre_penalty(["A", "B"], profile) == -1.0

    def test_no_genres_or_no_profile_is_neutral(self):
        assert candidates_mod.candidate_genre_penalty([], {"A": -1.0}) == 0.0
        assert candidates_mod.candidate_genre_penalty(["A"], {}) == 0.0


class TestNegativeMultiplier:
    def test_no_penalty_is_exactly_one(self):
        assert ranking.negative_multiplier() == 1.0
        assert ranking.negative_multiplier(0.0) == 1.0
        assert ranking.negative_multiplier(0.0, 0.0, 0.0) == 1.0

    def test_dampeners_combine_before_the_floor_not_after(self):
        """THE trap this function exists to prevent.

        Three signals each floored at 0.25 and then multiplied give 0.0156 — a 1.6% floor nobody
        chose, emerging from how many dampeners happen to be stacked rather than from any decision.
        """
        each_would_floor = math.log2(0.25)

        combined = ranking.negative_multiplier(each_would_floor, each_would_floor, each_would_floor)

        assert combined == pytest.approx(ranking.NEGATIVE_MULTIPLIER_FLOOR)
        assert combined > 0.25**3 * 10, "the dampeners compounded past the floor"

    def test_the_floor_holds_however_many_signals_pile_on(self):
        many = [math.log2(0.5)] * 20

        assert ranking.negative_multiplier(*many) == pytest.approx(ranking.NEGATIVE_MULTIPLIER_FLOOR)

    def test_a_positive_argument_cannot_become_a_bonus(self):
        """This is the NEGATIVE multiplier. A caller handing it a positive value is a bug, and it
        must not quietly turn into a promotion."""
        assert ranking.negative_multiplier(2.0) == 1.0

    def test_a_penalised_candidate_is_never_worthless(self):
        assert ranking.negative_multiplier(-100.0) >= ranking.NEGATIVE_MULTIPLIER_FLOOR


class TestScoreBackwardsCompatibility:
    def test_the_default_score_is_untouched_by_the_new_term(self):
        """Every existing install must score bit-for-bit as before until someone turns the dial."""
        candidate = Candidate(
            tmdb_id=1,
            title="x",
            media_type=MediaType.MOVIE,
            rating=7.5,
            affinity=0.8,
            seeds=[Seed(tmdb_id=2, title="s", media_type=MediaType.MOVIE, weight=0.5)],
        )
        expected = (1 + 1) * 7.5 * 1.5 * 0.8

        assert ranking.score(candidate) == pytest.approx(expected)

    def test_a_penalty_with_the_dial_off_changes_nothing(self):
        """The penalty rides on the candidate whether or not the owner asked for it, so the DIAL has
        to be what gates it — not the presence of the signal."""
        candidate = Candidate(tmdb_id=1, title="x", media_type=MediaType.MOVIE, rating=7.0, genre_penalty=-2.0)

        assert ranking.score(candidate) == pytest.approx(ranking.score(candidate, genre_avoidance=0.0))

    def test_turning_the_dial_up_demotes_an_avoided_candidate(self):
        candidate = Candidate(tmdb_id=1, title="x", media_type=MediaType.MOVIE, rating=7.0, genre_penalty=-1.0)

        assert ranking.score(candidate, genre_avoidance=1.0) < ranking.score(candidate)

    def test_it_demotes_but_never_reorders_past_the_floor(self):
        """A dampened title still has to lose on its own merits — a much better match must not be
        overtaken by a worse one just because the worse one avoided a penalty."""
        strong_but_penalised = Candidate(
            tmdb_id=1, title="strong", media_type=MediaType.MOVIE, rating=9.0, genre_penalty=-3.0
        )
        weak_but_clean = Candidate(tmdb_id=2, title="weak", media_type=MediaType.MOVIE, rating=4.0)

        assert ranking.score(strong_but_penalised, genre_avoidance=1.0) > ranking.score(weak_but_clean)
