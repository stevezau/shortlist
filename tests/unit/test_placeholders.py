"""The one place a row name's placeholders are asked about (`shortlist/engine/placeholders.py`)."""

from datetime import date

import pytest

from shortlist.engine import placeholders
from shortlist.engine.models import RowSeason
from shortlist.engine.placeholders import (
    fill_season,
    names_a_seed,
    needs_a_run,
    refusal,
    season_renderings,
    uses_season,
)
from shortlist.engine.seasons import BUILTIN_SEASONS, DateRule, Season

CHRISTMAS = RowSeason(slug="christmas", name="Christmas", emoji="🎄", anchor=date(2026, 12, 25))


class TestWhatATemplateDependsOn:
    @pytest.mark.parametrize(
        ("text", "seed", "season", "run"),
        [
            ("Picked for You", False, False, False),
            ("{user}'s {library_name} picks", False, False, False),
            ("Because you watched {top_seed}", True, False, True),
            ("{season} picks", False, True, True),
            ("{season_emoji} picks", False, True, True),
            ("{season} for {top_seed}", True, True, True),
            ("{Season} and {top_seed", False, False, False),
        ],
    )
    def test_each_question_is_answered_from_the_exact_tokens(self, text, seed, season, run):
        """Exact, case-sensitive tokens: the engine substitutes nothing else, so nothing else counts."""
        assert (names_a_seed(text), uses_season(text), needs_a_run(text)) == (seed, season, run)


class TestFillingTheSeason:
    def test_both_season_placeholders_are_filled(self):
        assert fill_season("{season_emoji} {season} picks", CHRISTMAS) == "🎄 Christmas picks"

    def test_without_a_season_the_text_is_untouched(self):
        assert fill_season("{season} picks", None) == "{season} picks"

    def test_a_seasonal_name_renders_once_per_catalogue_season(self):
        assert season_renderings("{season} picks", BUILTIN_SEASONS) == [
            "Valentine's Day picks",
            "Halloween picks",
            "Christmas picks",
        ]

    def test_a_plain_name_renders_once(self):
        assert season_renderings("Picked for You", BUILTIN_SEASONS) == ["Picked for You"]


class TestRefusal:
    @pytest.mark.parametrize("field", ["row_name", "fallback", "global_name", "person_name"])
    def test_a_plain_or_empty_name_is_never_refused(self, field):
        assert refusal("", field) is None
        assert refusal("✨ {library_name} for {user}", field) is None

    def test_a_fallback_can_use_neither_a_seed_nor_a_season(self):
        assert "{top_seed}" in refusal("Because you watched {top_seed}", "fallback")
        assert "{season}" in refusal("{season} picks", "fallback")

    def test_a_persons_name_can_use_neither_a_seed_nor_a_season(self):
        assert "{top_seed}" in refusal("Because you watched {top_seed}", "person_name")
        assert "{season}" in refusal("{season} picks", "person_name")

    @pytest.mark.parametrize("field", ["fallback", "person_name"])
    def test_a_name_with_both_is_refused_for_the_seed(self, field):
        """The order the old validators checked in, so the 422 an owner reads does not change."""
        assert "{top_seed}" in refusal("{season} for {top_seed}", field)
        assert "{season} or {season_emoji}" not in refusal("{season} for {top_seed}", field)

    def test_the_global_name_may_name_a_seed_but_not_a_season(self):
        """The setup wizard offers "Because you watched {top_seed}" for the default row, whose own fallback
        covers it; the default row follows no season."""
        assert refusal("Because you watched {top_seed}", "global_name") is None
        assert "{season}" in refusal("{season} picks", "global_name")

    def test_a_rows_own_name_may_use_the_season_only_when_the_row_follows_one(self):
        assert "follows seasons" in refusal("{season} picks", "row_name")
        assert refusal("{season} picks", "row_name", row_has_seasons=True) is None
        assert refusal("Because you watched {top_seed}", "row_name") is None

    def test_the_token_constants_are_the_engines_spelling(self):
        assert (placeholders.USER, placeholders.LIBRARY_NAME, placeholders.TOP_SEED) == (
            "{user}",
            "{library_name}",
            "{top_seed}",
        )
        assert placeholders.SEASON_PLACEHOLDERS == ("{season}", "{season_emoji}")


def test_season_renderings_include_custom_seasons() -> None:
    pat = Season(
        slug="pat",
        name="St Patrick's Day",
        emoji="☘️",
        rule=DateRule("fixed", month=3, day=17),
        description="",
        keywords=(1,),
    )
    catalogue = {**BUILTIN_SEASONS, "pat": pat}
    assert "☘️ St Patrick's Day picks" in season_renderings("{season_emoji} {season} picks", catalogue)
