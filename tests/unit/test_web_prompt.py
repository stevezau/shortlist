"""The native web-search prompt has to make the model look FORWARD, not at its training data.

Measured on a 2026 run before this: gpt-4o-mini and gpt-5-mini each returned 12 titles and not one
was from 2024 or later. Two things were missing and both are asserted here — the current year, and
an instruction to actually search. Asked a question that named the year, the same models searched
and cited real sources, so the tool was never the problem.

After: gpt-4o-mini 12 of 12 from 2024+, gpt-5-mini 9 of 12, Claude 12 of 12.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shortlist.engine.curator.base import (
    build_web_pick_prompt,
    build_web_prompt,
    build_web_rag_prompt,
    builtin_guidance,
    builtin_template,
    web_system_prompt,
)
from shortlist.engine.models import MediaType, Seed, UserProfile, UserType
from shortlist.engine.web_guidance import BUILTIN, Guidance, render_owner_text


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


@pytest.fixture
def profile():
    return UserProfile(username="alex", plex_account_id=1, user_type=UserType.SHARED)


EXPECTED_NATIVE_2026_K40 = (
    "You are a film and TV recommender with live web search. Today is in 2026, which is LATER than "
    "your training cutoff — so your own knowledge of what is new is out of date. Search the web "
    "before answering rather than recommending from memory. Search for what 'what to watch next' "
    "articles, critics' best-of lists and review sites are recommending in 2026 and 2025. "
    "Based on what this person recently watched, give 40 titles they'd most likely want to watch "
    "next. Strongly prefer titles released in 2025 or 2026; include something older only "
    "when it is an unusually good match for their taste. Rules: (1) never recommend a title they "
    "already watched, or another season or sequel of one; (2) ALWAYS give the exact release year — "
    "it is used to look the title up, and a missing year means the recommendation is discarded; "
    "(3) use the exact title as released, not a description of it; (4) only titles ALREADY RELEASED "
    "and watchable now — never announced, upcoming or unaired ones; (5) name a series by its series "
    "title alone, never 'Season 2' or 'Part 3'. Prefer real, findable titles over "
    'obscure guesses. Respond with ONLY a JSON array of up to 40 objects, each {"title": str, '
    '"year": int, "media": "movie" or "show"}. No prose.'
)
EXPECTED_RAG_K40 = (
    "You are a film and TV recommender. Below are excerpts from recent web articles about what to "
    "watch. Based on what this person recently enjoyed, pick the 40 titles mentioned in these "
    "articles they'd most likely want to watch next. Prefer real, well-reviewed, findable titles. "
    "Give the exact release year wherever the article states it — it is used to look the title up. "
    'Respond with ONLY a JSON array of up to 40 objects, each {"title": str, "year": int or null, '
    '"media": "movie" or "show"}. No prose.'
)
EXPECTED_PICK_K40 = (
    "You are a film and TV recommender. Below is a list of titles that recent web articles "
    "recommend as things to watch next. Based on what this person recently enjoyed, "
    "pick the 40 they'd most likely want to watch next. Choose only from the list — do not add "
    "titles of your own. Keep each title and year exactly as written; they are used to look the "
    'title up. Respond with ONLY a JSON array of up to 40 objects, each {"title": str, "year": '
    'int or null, "media": "movie" or "show"}. No prose.'
)


class TestBuiltinPromptsAreByteIdentical:
    """No instructions anywhere must send exactly what this source always sent (#138 global constraint)."""

    def test_native(self, profile):
        assert build_web_prompt(profile, [], 40, year=2026)[0] == EXPECTED_NATIVE_2026_K40
        assert build_web_prompt(profile, [], 40, year=2026, guidance=BUILTIN)[0] == EXPECTED_NATIVE_2026_K40

    def test_native_user_message(self, profile):
        seeds = [Seed(tmdb_id=1, title="Severance", media_type=MediaType.SHOW, weight=1.0)]
        expected = (
            "They recently enjoyed:\n- Severance\n\n"
            "Search the web for what to watch next, then recommend up to 40 titles."
            " Favour things released in 2025 or 2026."
        )
        assert build_web_prompt(profile, seeds, 40, year=2026)[1] == expected
        assert build_web_prompt(profile, seeds, 40, year=2026, guidance=BUILTIN)[1] == expected

    def test_rag_and_pick(self, profile):
        assert build_web_rag_prompt(profile, [], 40)[0] == EXPECTED_RAG_K40
        assert build_web_pick_prompt(profile, [], 40)[0] == EXPECTED_PICK_K40

    def test_preview_matches_the_builders(self):
        assert web_system_prompt("native", k=40, year=2026, guidance=None) == EXPECTED_NATIVE_2026_K40
        assert web_system_prompt("searxng", k=40, year=2026, guidance=None) == EXPECTED_RAG_K40
        assert web_system_prompt("exa", k=40, year=2026, guidance=None) == EXPECTED_PICK_K40


class TestOwnerGuidance:
    def test_add_keeps_the_builtin_guidance_and_appends_the_rows_text(self):
        system = web_system_prompt("exa", k=40, year=2026, guidance=Guidance(extra="Nothing aimed at kids."))
        assert "Based on what this person recently enjoyed, pick the 40" in system
        assert "The server owner adds, for this row: Nothing aimed at kids. Choose only from the list" in system

    def test_replace_drops_the_builtin_guidance_but_keeps_every_mechanic(self):
        system = web_system_prompt("native", k=40, year=2026, guidance=Guidance(replace="Films from any decade."))
        assert "Strongly prefer titles released in" not in system
        assert "Films from any decade. Give up to 40 titles." in system
        for locked in (
            "Today is in 2026",
            "Search the web",
            "ALWAYS give the exact release year",
            "ALREADY RELEASED",
            "Respond with ONLY a JSON array of up to 40 objects",
        ):
            assert locked in system

    def test_replace_on_rag_and_pick_keeps_the_list_constraint(self):
        rag = web_system_prompt("searxng", k=40, year=2026, guidance=Guidance(replace="Any decade."))
        pick = web_system_prompt("exa", k=40, year=2026, guidance=Guidance(replace="Any decade."))
        assert "Pick up to 40 of the titles mentioned in these articles." in rag
        assert "Pick up to 40 of them." in pick and "Choose only from the list" in pick

    def test_braces_in_owner_text_never_reach_format(self, profile):
        system, _ = build_web_pick_prompt(profile, [], 40, guidance=Guidance(extra='Use {k} and {"a": 1}.'))
        assert 'Use {k} and {"a": 1}.' in system

    def test_builtin_guidance_is_the_sentence_an_owner_would_replace(self):
        assert builtin_guidance("native", k=40, year=2026).startswith(
            "Based on what this person recently watched, give 40 titles"
        )
        assert builtin_guidance("exa", k=40, year=2026) == (
            "Based on what this person recently enjoyed, pick the 40 they'd most likely want to watch next."
        )


class TestTheNativeUserMessageFollowsTheGuidance:
    """The built-in guidance's release-window steer is repeated in the native user message (#138 review)."""

    def test_own_guidance_drops_the_release_window_from_the_user_message(self):
        _, user = build_web_prompt(
            None, _seeds(), 40, year=2026, guidance=Guidance(replace="Classic 1970s films only.")
        )
        assert "Favour things released in" not in user
        assert user.endswith("then recommend up to 40 titles.")

    def test_added_guidance_keeps_it(self):
        _, user = build_web_prompt(None, _seeds(), 40, year=2026, guidance=Guidance(extra="No horror."))
        assert user.endswith("Favour things released in 2025 or 2026.")


EXPECTED_TEMPLATES = {
    "native": (
        "Based on what this person recently watched, give {count} titles they'd most likely want to watch "
        "next. Strongly prefer titles released in {last_year} or {year}; include something older only "
        "when it is an unusually good match for their taste."
    ),
    "exa": "Based on what this person recently enjoyed, pick the {count} they'd most likely want to watch next.",
    "searxng": (
        "Based on what this person recently enjoyed, pick the {count} titles mentioned in these "
        "articles they'd most likely want to watch next. Prefer real, well-reviewed, findable titles."
    ),
}


class TestBuiltinTemplate:
    """What Settings' "Write your own" starts from: the built-in guide with its placeholders still in."""

    @pytest.mark.parametrize("backend", ["native", "exa", "searxng"])
    def test_it_is_the_guide_with_placeholders_unfilled(self, backend: str):
        assert builtin_template(backend) == EXPECTED_TEMPLATES[backend]

    @pytest.mark.parametrize("backend", ["native", "exa", "searxng"])
    def test_rendering_it_gives_the_builtin_guidance(self, backend: str):
        assert render_owner_text(builtin_template(backend), k=40, year=2026) == builtin_guidance(
            backend, k=40, year=2026
        )

    def test_an_unknown_backend_is_native(self):
        assert builtin_template("auto") == EXPECTED_TEMPLATES["native"]
