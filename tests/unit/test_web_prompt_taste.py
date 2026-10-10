"""A rendered taste profile (#152) replaces the recent-watches list in the web prompts; without one, nothing changes."""

from datetime import UTC, datetime

from shortlist.engine.clients.search import TitleCandidate
from shortlist.engine.curator.base import (
    build_web_pick_prompt,
    build_web_prompt,
    build_web_rag_prompt,
    taste_summary,
    web_system_prompt,
)
from shortlist.engine.models import MediaType, Seed, UserProfile, UserType, WatchedItem
from shortlist.engine.taste import TastePrompt
from shortlist.engine.web_guidance import Guidance

TASTE = TastePrompt("What this person has watched. TASTE-MARKER", True)
RECENT = TastePrompt("Recently watched (most recent first):\n- Pinned (2020)", False)


def _profile() -> UserProfile:
    history = [WatchedItem(title="Severance", media_type=MediaType.SHOW, watched_at=datetime(2026, 1, 1, tzinfo=UTC))]
    return UserProfile(username="a", plex_account_id=1, user_type=UserType.SHARED, history=history)


def _seeds() -> list[Seed]:
    return [Seed(tmdb_id=1, title="Severance", media_type=MediaType.SHOW)]


def _candidates() -> list[TitleCandidate]:
    return [TitleCandidate(title="Silo", year=2023, media="show")]


class TestWithoutTasteNothingChanges:
    def test_native(self):
        assert build_web_prompt(_profile(), _seeds(), 12, year=2026) == build_web_prompt(
            _profile(), _seeds(), 12, year=2026, taste=None
        )

    def test_the_system_prompts_still_say_recently(self):
        for backend in ("native", "exa", "searxng"):
            assert "recently" in web_system_prompt(backend, k=40, year=2026, guidance=None)

    def test_pick_and_rag_still_use_the_twenty_newest(self):
        _, pick = build_web_pick_prompt(_profile(), _candidates(), 10, year=2026)
        _, rag = build_web_rag_prompt(_profile(), [], 10, year=2026)
        assert pick.startswith(taste_summary(_profile()))
        assert rag.startswith(taste_summary(_profile()))


class TestWithTaste:
    def test_native_user_prompt_swaps_the_recent_block_and_keeps_its_closing_sentence(self):
        system, user = build_web_prompt(_profile(), _seeds(), 12, year=2026, taste=TASTE)
        assert user.startswith(TASTE.text)
        assert "They recently enjoyed" not in user
        assert user.endswith("Favour things released in 2025 or 2026.")
        assert "Search the web for what to watch next, then recommend up to 12 titles." in user
        assert "this person's viewing history" in system
        assert "recently watched" not in system

    def test_pick_prompt_carries_the_taste_and_the_wide_guide(self):
        system, user = build_web_pick_prompt(_profile(), _candidates(), 10, year=2026, taste=TASTE)
        assert user.startswith(TASTE.text)
        assert "Recently watched (most recent first)" not in user
        assert "- Silo (2023) [show]" in user
        assert "Based on this person's viewing history, pick the 10" in system

    def test_rag_prompt_carries_the_taste_and_the_wide_guide(self):
        system, user = build_web_rag_prompt(_profile(), [], 10, year=2026, taste=TASTE)
        assert user.startswith(TASTE.text)
        assert "this person's viewing history" in system

    def test_an_owners_replacement_text_is_untouched(self):
        guidance = Guidance(replace="Films from any decade.")
        plain = build_web_pick_prompt(_profile(), _candidates(), 10, year=2026, guidance=guidance)[0]
        wide = build_web_pick_prompt(_profile(), _candidates(), 10, year=2026, guidance=guidance, taste=TASTE)[0]
        assert plain == wide
        assert "Films from any decade." in wide

    def test_an_owners_extra_text_is_kept(self):
        system, _ = build_web_pick_prompt(
            _profile(), _candidates(), 10, year=2026, guidance=Guidance(extra="No horror."), taste=TASTE
        )
        assert "No horror." in system and "viewing history" in system


class TestARecentTastePrompt:
    def test_the_pick_prompt_lists_exactly_the_given_titles_with_the_ordinary_guide(self):
        system, user = build_web_pick_prompt(_profile(), _candidates(), 10, year=2026, taste=RECENT)
        assert user.startswith(RECENT.text)
        assert "Severance" not in user
        assert system == build_web_pick_prompt(_profile(), _candidates(), 10, year=2026)[0]

    def test_the_rag_prompt_is_the_same(self):
        system, user = build_web_rag_prompt(_profile(), [], 10, year=2026, taste=RECENT)
        assert user.startswith(RECENT.text)
        assert system == build_web_rag_prompt(_profile(), [], 10, year=2026)[0]

    def test_the_native_prompt_ignores_it(self):
        assert build_web_prompt(_profile(), _seeds(), 12, year=2026, taste=RECENT) == build_web_prompt(
            _profile(), _seeds(), 12, year=2026
        )
