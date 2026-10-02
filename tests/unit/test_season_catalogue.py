"""shortlist/server/services/season_catalogue.py: the built-ins plus the owner's own seasons (issue #137)."""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from shortlist.engine.models import MediaType
from shortlist.engine.seasons import CollectionRef, DateRule, season_content_hash
from shortlist.server.db.models import SeasonDef
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.season_catalogue import load_catalogue, make_slug, season_from_row


@pytest.fixture
def session(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    with make_session_factory(engine)() as session:
        yield session
    engine.dispose()


def _row(**overrides) -> SeasonDef:
    """An unsaved row with every column filled — a transient ORM object carries none of its defaults."""
    fields = {
        "slug": "x",
        "name": "X",
        "emoji": "🎉",
        "rule_kind": "fixed",
        "month": 1,
        "day": 1,
        "nth": 1,
        "weekday": 0,
        "easter_offset": 0,
        "lead_days": 7,
        "after_days": 0,
        "tags": [{"id": 5, "name": "x"}],
        "genre": None,
        "excluded_genres": [],
        "collections": [],
        "picks": [],
    }
    return SeasonDef(**(fields | overrides))


def test_custom_seasons_follow_the_built_ins(session) -> None:
    session.add(
        SeasonDef(
            slug="st-patricks-day",
            name="St Patrick's Day",
            emoji="☘️",
            rule_kind="fixed",
            month=3,
            day=17,
            tags=[{"id": 209352, "name": "st. patrick's day"}],
            excluded_genres=[27],
        )
    )
    session.commit()
    catalogue = load_catalogue(session)
    assert list(catalogue)[:3] == ["valentines", "halloween", "christmas"]
    pat = catalogue["st-patricks-day"]
    assert pat.rule == DateRule("fixed", month=3, day=17) and pat.lead_days == 7 and pat.after_days == 0
    assert pat.keywords == (209352,) and pat.keyword_excluded_genres == (27,) and not pat.builtin
    assert pat.content_hash == season_content_hash(pat)


def test_custom_seasons_are_ordered_by_id_after_the_built_ins(session) -> None:
    for slug in ("zebra-day", "apple-day"):
        session.add(SeasonDef(slug=slug, name=slug, emoji="🎉", rule_kind="fixed", month=5, day=1))
        session.commit()

    assert list(load_catalogue(session)) == ["valentines", "halloween", "christmas", "zebra-day", "apple-day"]


def test_the_built_ins_keep_an_empty_content_hash(session) -> None:
    catalogue = load_catalogue(session)

    assert {catalogue[slug].content_hash for slug in ("valentines", "halloween", "christmas")} == {""}


class TestSeasonFromRow:
    def test_every_source_and_timing_column_reaches_the_season(self) -> None:
        row = SeasonDef(
            slug="thanksgiving",
            name="Thanksgiving",
            emoji="🦃",
            rule_kind="nth",
            month=11,
            day=1,
            nth=4,
            weekday=3,
            easter_offset=0,
            lead_days=10,
            after_days=2,
            tags=[{"id": 1, "name": "thanksgiving"}, {"id": 2, "name": "pilgrims"}],
            genre=10751,
            excluded_genres=[27, 53],
            collections=[{"section_key": "1", "section_title": "Movies", "title": "Thanksgiving Films"}],
            picks=[
                {"tmdb_id": 11, "media_type": "movie", "title": "Planes, Trains", "year": 1987},
                {"tmdb_id": 22, "media_type": "show", "title": "Friends", "year": 1994},
            ],
        )

        season = season_from_row(row)

        assert season.slug == "thanksgiving" and season.name == "Thanksgiving" and season.emoji == "🦃"
        assert season.rule == DateRule("nth", month=11, day=1, nth=4, weekday=3, offset=0)
        assert (season.lead_days, season.after_days) == (10, 2)
        assert season.keywords == (1, 2)
        assert season.movie_genres == (10751,)
        assert season.keyword_excluded_genres == (27, 53)
        assert season.collections == (CollectionRef(section_key="1", title="Thanksgiving Films"),)
        assert season.picks == ((11, MediaType.MOVIE), (22, MediaType.SHOW))
        assert season.description == "" and season.builtin is False
        assert season.content_hash == season_content_hash(season)

    def test_an_easter_rule_carries_its_offset(self) -> None:
        row = _row(rule_kind="easter", easter_offset=-21)

        assert season_from_row(row).rule == DateRule("easter", month=1, day=1, nth=1, weekday=0, offset=-21)

    def test_no_genre_means_no_movie_genres(self) -> None:
        assert season_from_row(_row(genre=None)).movie_genres == ()

    def test_picks_read_from_json_are_hashable_int_and_media_type_pairs(self) -> None:
        """JSON hands back lists and strings; `season_content_hash` sorts `(id, media_type.value)` and the
        engine builds a set of them, so a pick has to be an `(int, MediaType)` tuple."""
        row = _row(picks=[{"tmdb_id": "603", "media_type": "movie", "title": "The Matrix", "year": 1999}])

        (pick,) = season_from_row(row).picks

        assert pick == (603, MediaType.MOVIE) and type(pick[0]) is int and type(pick[1]) is MediaType
        assert set(season_from_row(row).picks) == {(603, MediaType.MOVIE)}


class TestACorruptRowIsSkipped:
    """Rows are validated when they are written, but a hand-edited row must not crash every run: the
    season drops out of the catalogue, and rows that follow it go dormant (the unknown-slug path)."""

    @pytest.mark.parametrize(
        "corruption",
        [
            {"rule_kind": "nth", "nth": 7},  # `DateRule.label()` would KeyError on it
            {"rule_kind": "fixed", "month": 2, "day": 29},
            {"rule_kind": "lunar"},
            {"picks": [{"tmdb_id": 1, "media_type": "film", "title": "?", "year": 2000}]},
        ],
    )
    def test_a_corrupt_row_is_left_out_with_a_warning_naming_it(self, session, corruption: dict) -> None:
        fields = {"name": "Broken", "emoji": "💥", "rule_kind": "fixed", "month": 3, "day": 17} | corruption
        session.add(SeasonDef(slug="broken", **fields))
        session.add(SeasonDef(slug="fine", name="Fine", emoji="🙂", rule_kind="fixed", month=3, day=18))
        session.commit()
        lines: list[str] = []
        sink = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            catalogue = load_catalogue(session)
        finally:
            logger.remove(sink)

        assert "broken" not in catalogue and "fine" in catalogue
        assert any("broken" in line for line in lines), lines

    def test_a_stored_row_cannot_replace_a_built_in(self, session) -> None:
        session.add(SeasonDef(slug="christmas", name="Xmas", emoji="🎅", rule_kind="fixed", month=7, day=25))
        session.commit()
        lines: list[str] = []
        sink = logger.add(lines.append, level="WARNING", format="{message}")
        try:
            christmas = load_catalogue(session)["christmas"]
        finally:
            logger.remove(sink)

        assert christmas.builtin and christmas.rule == DateRule("fixed", month=12, day=25)
        assert any("christmas" in line for line in lines), lines

    def test_season_from_row_refuses_an_invalid_rule(self) -> None:
        with pytest.raises(ValueError):
            season_from_row(_row(rule_kind="nth", month=11, nth=7, weekday=3))


@pytest.mark.parametrize(
    ("name", "taken", "slug"),
    [
        ("St Patrick's Day", set(), "st-patricks-day"),
        ("Christmas", {"christmas"}, "christmas-2"),
        ("🎆", set(), "season"),
    ],
)
def test_make_slug(name, taken, slug) -> None:
    assert make_slug(name, taken) == slug


@pytest.mark.parametrize(
    ("name", "taken", "slug"),
    [
        ("St Patrick\N{RIGHT SINGLE QUOTATION MARK}s Day", set(), "st-patricks-day"),
        ("  Día de Muertos!! ", set(), "dia-de-muertos"),
        ("Christmas", {"christmas", "christmas-2"}, "christmas-3"),
        ("🎆", {"season"}, "season-2"),
    ],
)
def test_make_slug_edge_cases(name, taken, slug) -> None:
    assert make_slug(name, taken) == slug


def test_make_slug_never_takes_a_built_in_slug_whatever_it_is_told() -> None:
    """Rows store slugs: a custom "Hallowe'en" under `halloween` would be read as the built-in by every row."""
    assert make_slug("Hallowe'en", set()) == "halloween-2"
    assert make_slug("Christmas", {"christmas-2"}) == "christmas-3"


@pytest.mark.parametrize(
    "name",
    [
        "x" * 100,
        "a" * 55 + " bcdefgh",  # the cut lands just after a dash
        "\N{TELEPHONE SIGN}" * 40,  # 40 characters, the most a name may have, and 120 letters decomposed
    ],
)
def test_make_slug_fits_the_column_even_with_a_suffix(name: str) -> None:
    base = make_slug(name, set())
    suffixed = make_slug(name, {base, *(f"{base}-{n}" for n in range(2, 12))})
    assert len(base) <= 56 and not base.endswith("-")
    assert suffixed == f"{base}-12" and len(suffixed) <= 64
