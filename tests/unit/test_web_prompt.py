"""The native web-search prompt has to make the model look FORWARD, not at its training data.

Measured on a 2026 run before this: gpt-4o-mini and gpt-5-mini each returned 12 titles and not one
was from 2024 or later. Two things were missing and both are asserted here — the current year, and
an instruction to actually search. Asked a question that named the year, the same models searched
and cited real sources, so the tool was never the problem.

After: gpt-4o-mini 12 of 12 from 2024+, gpt-5-mini 9 of 12, Claude 12 of 12.
"""

from __future__ import annotations

from datetime import UTC, datetime

from shortlist.engine.curator.base import build_web_prompt
from shortlist.engine.models import MediaType, Seed


def _seeds() -> list[Seed]:
    return [Seed(tmdb_id=1, title="Severance", media_type=MediaType.SHOW, weight=1.0)]


class TestTheModelIsToldWhatYearItIs:
    def test_the_year_appears_in_the_prompt(self):
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "2026" in system

    def test_last_year_appears_too_so_recent_is_a_range_not_a_point(self):
        """A single year is a bullseye the model will miss; the useful window is the last two."""
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "2025" in system

    def test_it_defaults_to_the_real_current_year(self):
        system, _ = build_web_prompt(None, _seeds(), 12)
        assert str(datetime.now(UTC).year) in system

    def test_the_user_prompt_carries_the_window_too(self):
        _, user = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "2026" in user and "2025" in user


class TestTheModelIsToldToSearch:
    def test_it_says_to_search_rather_than_recall(self):
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "search the web" in system.lower()

    def test_it_says_the_models_own_knowledge_is_stale(self):
        """The lever that worked: naming the cutoff as the reason, not just asking nicely."""
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "out of date" in system.lower()


class TestTheOverCorrectionGuards:
    """Anchoring to the year made gpt-4o-mini swing to unreleased 2026 titles and 'Season 3'
    entries — real regressions, caught live, fixed with two explicit rules."""

    def test_it_forbids_unreleased_titles(self):
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "ALREADY RELEASED" in system

    def test_it_forbids_naming_a_season(self):
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "Season 2" in system  # quoted as the thing NOT to do


class TestTheRulesThatWereAlreadyThereSurvived:
    """The year rule is what makes a title resolvable; losing it in a rewrite would be silent."""

    def test_it_still_demands_an_exact_year(self):
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "exact release year" in system

    def test_it_still_forbids_recommending_a_watched_title(self):
        system, _ = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "already watched" in system

    def test_the_seed_titles_still_reach_the_user_prompt(self):
        _, user = build_web_prompt(None, _seeds(), 12, year=2026)
        assert "Severance" in user
