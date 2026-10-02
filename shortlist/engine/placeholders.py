"""The placeholders a row's name, description and poster text can carry, and the questions asked about them.

Every module that renders, matches, validates or reports a row's name asks one of a few questions about its
template: does it name a seed, does it follow the season, can a title be predicted without a run, may this
field use it. Each used to spell its answer out by hand, which is how adding ``{season}`` meant finding
every ``"{top_seed}" in`` in the codebase. The answers live here; the callers decide what to do with them.

``web/src/lib/placeholders.ts`` is the SPA's copy of the token list and must stay in step with it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from shortlist.engine.models import RowSeason

if TYPE_CHECKING:
    from shortlist.engine.seasons import Catalogue

USER = "{user}"
LIBRARY_NAME = "{library_name}"
TOP_SEED = "{top_seed}"
SEASON = "{season}"
SEASON_EMOJI = "{season_emoji}"

#: A seasonal row's placeholders (discussion #124). Filled from the row's season by
#: `delivery.resolve_row_template`; one still standing afterwards means there was no season to fill it with.
SEASON_PLACEHOLDERS = (SEASON, SEASON_EMOJI)


def names_a_seed(text: str) -> bool:
    """Whether the text names a watch (``{top_seed}``), so it needs a pick with a seed to be filled."""
    return TOP_SEED in text


def uses_season(text: str) -> bool:
    """Whether a name, description or poster line depends on the row's season."""
    return any(placeholder in text for placeholder in SEASON_PLACEHOLDERS)


def needs_a_run(text: str) -> bool:
    """Whether a title from this template can only be known by the run that built it.

    A ``{top_seed}`` title differs per person and per night, and a seasonal collection wears whichever season
    it was last built for. Neither can be matched by rendering the template, so their collections are
    identified by the delivery ledger, and what the ledger recorded them as is claimed against other rows.
    """
    return names_a_seed(text) or uses_season(text)


def fill_season(text: str, season: RowSeason | None) -> str:
    """``text`` with the season placeholders filled, or untouched when there is no season to fill them."""
    if season is None:
        return text
    return text.replace(SEASON_EMOJI, season.emoji).replace(SEASON, season.name)


def catalogue_seasons(catalogue: Catalogue) -> list[RowSeason]:
    """Every season a row can follow, as a name-filling season (the anchor's year is irrelevant to a name)."""
    return [RowSeason(slug=s.slug, name=s.name, emoji=s.emoji, anchor=s.rule.anchor(2000)) for s in catalogue.values()]


def season_renderings(template: str, catalogue: Catalogue) -> list[str]:
    """``template`` once per catalogue season, for the checks that must see every title a seasonal row
    can wear — out of season it keeps the last one, and no single night's spec can render that."""
    if not uses_season(template):
        return [template]
    return [fill_season(template, season) for season in catalogue_seasons(catalogue)]


#: Where a name is being written, for `refusal`.
NameField = Literal["row_name", "fallback", "global_name", "person_name"]


def refusal(text: str, field: NameField, *, row_has_seasons: bool = False) -> str | None:
    """Why ``text`` may not be saved in ``field``, in the words the API answers with; None when it may.

    Args:
        text: The name as the owner typed it.
        field: ``row_name`` — a row's own name; ``fallback`` — a row's name for people its own name cannot be
            filled for; ``global_name`` — the default row's name (Settings); ``person_name`` — one person's
            override of the default row's name.
        row_has_seasons: For ``row_name``: whether the row follows any season.
    """
    if not text:
        return None
    if field == "fallback":
        if names_a_seed(text):
            return (
                "the fallback name is for people with nothing watched, so it can't use {top_seed} "
                "either — there'd still be nothing to put in it. Use a name that stands on its own."
            )
        if uses_season(text):
            return (
                "the fallback name stands in when the row's own name can't be filled in, so it can't use "
                "{season} or {season_emoji}. Use a name that stands on its own."
            )
    elif field == "person_name":
        if names_a_seed(text):
            return (
                "a per-person row name can't use {top_seed} — it needs a fallback name for people "
                "with nothing watched yet, and that lives on the row, not the person. Set it on the "
                "row instead."
            )
        if uses_season(text):
            return (
                "a per-person row name can't use {season} or {season_emoji} — it names the default row, which "
                "follows no season. Give a seasonal row its own name instead."
            )
    elif field == "global_name":
        if uses_season(text):
            # The default row follows no season, so the placeholder could never be filled and the row would
            # stop being built for everyone (discussion #124).
            return "can't use {season} or {season_emoji} — only a seasonal row's own name can"
    elif uses_season(text) and not row_has_seasons:
        return (
            "{season} and {season_emoji} only work on a row that follows seasons — pick its seasons, "
            "or take them out of the name."
        )
    return None
