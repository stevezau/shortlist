"""Owner-written guidance for AI web search (discussion #138)."""

import pytest

from shortlist.engine.web_guidance import (
    BUILTIN,
    AiInstructions,
    Guidance,
    render_owner_text,
    resolve_guidance,
)


class TestFromStored:
    @pytest.mark.parametrize(
        "stored",
        [
            None,
            {},
            [],
            "own",
            {"tone": "warm"},
            {"mode": "own", "text": "   "},
            {"mode": "default", "text": "x"},
            {"mode": "bogus", "text": "x"},
            {"mode": "add", "text": 7},
        ],
    )
    def test_anything_unrecognised_reads_as_no_instructions(self, stored):
        assert AiInstructions.from_stored(stored) is None

    def test_add_and_own_round_trip_with_text_trimmed(self):
        assert AiInstructions.from_stored({"mode": "add", "text": "  Only French films. "}) == AiInstructions(
            "add", "Only French films."
        )
        assert AiInstructions.from_stored({"mode": "own", "text": "Any decade."}) == AiInstructions(
            "own", "Any decade."
        )


class TestResolveGuidance:
    def test_no_row_and_no_server_text_is_builtin(self):
        assert resolve_guidance(None, "") == BUILTIN
        assert resolve_guidance(None, "   ") == BUILTIN
        assert BUILTIN.is_builtin and BUILTIN.fingerprint() == ""

    def test_server_text_replaces_the_builtin_for_rows_without_their_own(self):
        assert resolve_guidance(None, " Favour classics. ") == Guidance(replace="Favour classics.")

    def test_add_keeps_the_server_text_and_adds_the_rows(self):
        assert resolve_guidance(AiInstructions("add", "No kids films."), "") == Guidance(extra="No kids films.")
        assert resolve_guidance(AiInstructions("add", "No kids films."), "Favour classics.") == Guidance(
            replace="Favour classics.", extra="No kids films."
        )

    def test_own_replaces_whatever_the_server_says(self):
        assert resolve_guidance(AiInstructions("own", "Any decade."), "Favour classics.") == Guidance(
            replace="Any decade."
        )

    def test_fingerprint_differs_by_content_and_is_short(self):
        a, b = Guidance(extra="x").fingerprint(), Guidance(replace="x").fingerprint()
        assert a and b and a != b and len(a) == 16


class TestRenderOwnerText:
    def test_only_count_year_and_last_year_are_filled(self):
        text = " Give {count}, from {last_year} or {year}. "
        assert render_owner_text(text, k=40, year=2026) == "Give 40, from 2025 or 2026."

    def test_every_other_brace_is_kept_as_typed(self):
        text = 'Use {k} and {0} and {{x}} and {"a": 1} and a lone { brace'
        assert render_owner_text(text, k=40, year=2026) == text
