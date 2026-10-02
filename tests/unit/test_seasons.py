"""Which season a seasonal row is in, on which day (discussion #124).

All of it is date arithmetic on pure functions, so every edge is pinned here: a window that crosses New
Year in either direction, two windows claiming one day, the pre-build night before a season starts, and
the gaps between seasons where the row is dormant.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from typing import ClassVar

import pytest

from shortlist.engine import seasons
from shortlist.engine.clients.plex_pms import LibraryTitle
from shortlist.engine.models import MediaType
from shortlist.engine.rows import row_shown_today

ALL = ["halloween", "christmas", "valentines"]


class TestCatalogue:
    def test_the_three_seasons_are_on_their_fixed_dates(self):
        assert (seasons.BUILTIN_SEASONS["halloween"].rule.month, seasons.BUILTIN_SEASONS["halloween"].rule.day) == (
            10,
            31,
        )
        assert (seasons.BUILTIN_SEASONS["christmas"].rule.month, seasons.BUILTIN_SEASONS["christmas"].rule.day) == (
            12,
            25,
        )
        assert (seasons.BUILTIN_SEASONS["valentines"].rule.month, seasons.BUILTIN_SEASONS["valentines"].rule.day) == (
            2,
            14,
        )

    def test_the_catalogue_is_in_calendar_order(self):
        """The editor lists seasons in this order, so a year reads top to bottom."""
        assert list(seasons.BUILTIN_SEASONS) == ["valentines", "halloween", "christmas"]

    def test_the_keyword_ids_are_the_measured_ones(self):
        """Measured on TMDB 2026-09-15 (`/search/keyword`): a wrong id matches real, unrelated films."""
        assert seasons.BUILTIN_SEASONS["halloween"].keywords[0] == 3335
        assert seasons.BUILTIN_SEASONS["christmas"].keywords[0] == 207317
        assert seasons.BUILTIN_SEASONS["valentines"].keywords[0] == 160404

    def test_halloween_and_valentines_widen_with_a_film_genre_and_christmas_does_not(self):
        """Keywords alone tag 63 Halloween and 20 Valentine's films on a real 10k-film library — too few for
        rows that differ person to person. Christmas tags 340 and has no genre to widen into."""
        assert seasons.BUILTIN_SEASONS["halloween"].movie_genres == (27,)
        assert seasons.BUILTIN_SEASONS["valentines"].movie_genres == (10749,)
        assert seasons.BUILTIN_SEASONS["christmas"].movie_genres == ()


class TestNormaliseSlugs:
    def test_unknown_seasons_are_refused(self):
        with pytest.raises(ValueError, match="easter"):
            seasons.normalise_slugs(["christmas", "easter"], catalogue=seasons.BUILTIN_SEASONS)

    def test_duplicates_collapse_and_the_order_follows_the_calendar(self):
        assert seasons.normalise_slugs(["christmas", "valentines", "christmas"], catalogue=seasons.BUILTIN_SEASONS) == [
            "valentines",
            "christmas",
        ]

    def test_an_empty_list_stays_empty(self):
        assert seasons.normalise_slugs([], catalogue=seasons.BUILTIN_SEASONS) == []


class TestShownOn:
    def test_a_season_shows_from_its_lead_through_its_day(self):
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 9, 30), catalogue=seasons.BUILTIN_SEASONS) is None
        assert (
            seasons.shown_on(["halloween"], 30, 0, date(2026, 10, 1), catalogue=seasons.BUILTIN_SEASONS).season.slug
            == "halloween"
        )
        assert (
            seasons.shown_on(["halloween"], 30, 0, date(2026, 10, 31), catalogue=seasons.BUILTIN_SEASONS).season.slug
            == "halloween"
        )
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 11, 1), catalogue=seasons.BUILTIN_SEASONS) is None

    def test_days_after_keep_it_up_past_its_day(self):
        assert (
            seasons.shown_on(["christmas"], 0, 1, date(2026, 12, 26), catalogue=seasons.BUILTIN_SEASONS).season.slug
            == "christmas"
        )
        assert seasons.shown_on(["christmas"], 0, 1, date(2026, 12, 27), catalogue=seasons.BUILTIN_SEASONS) is None

    def test_a_zero_lead_shows_it_on_its_day_alone(self):
        assert seasons.shown_on(["valentines"], 0, 0, date(2027, 2, 13), catalogue=seasons.BUILTIN_SEASONS) is None
        assert (
            seasons.shown_on(["valentines"], 0, 0, date(2027, 2, 14), catalogue=seasons.BUILTIN_SEASONS).season.slug
            == "valentines"
        )

    def test_the_window_carries_its_dates(self):
        window = seasons.shown_on(["christmas"], 30, 2, date(2026, 12, 1), catalogue=seasons.BUILTIN_SEASONS)
        assert (window.anchor, window.starts, window.ends) == (
            date(2026, 12, 25),
            date(2026, 11, 25),
            date(2026, 12, 27),
        )

    def test_a_lead_that_starts_in_the_previous_year_is_found(self):
        """Valentine's with a 60-day lead starts 16 Dec — the anchor is NEXT year's 14 Feb."""
        window = seasons.shown_on(["valentines"], 60, 0, date(2026, 12, 20), catalogue=seasons.BUILTIN_SEASONS)
        assert window.anchor == date(2027, 2, 14)

    def test_days_after_that_run_into_the_next_year_are_found(self):
        window = seasons.shown_on(["christmas"], 0, 10, date(2027, 1, 3), catalogue=seasons.BUILTIN_SEASONS)
        assert window.anchor == date(2026, 12, 25)

    def test_a_season_not_chosen_never_shows(self):
        assert seasons.shown_on(["valentines"], 30, 0, date(2026, 12, 20), catalogue=seasons.BUILTIN_SEASONS) is None

    def test_no_seasons_means_nothing_shows(self):
        assert seasons.shown_on([], 30, 0, date(2026, 12, 20), catalogue=seasons.BUILTIN_SEASONS) is None

    def test_when_two_windows_claim_a_day_the_coming_season_wins(self):
        """Halloween lingering into November meets Christmas starting early: Halloween is over, so the
        row moves on to what is coming."""
        window = seasons.shown_on(
            ["halloween", "christmas"], 60, 5, date(2026, 11, 2), catalogue=seasons.BUILTIN_SEASONS
        )
        assert window.season.slug == "christmas"

    def test_when_two_coming_seasons_claim_a_day_the_nearer_one_wins(self):
        window = seasons.shown_on(
            ["halloween", "christmas"], 90, 0, date(2026, 10, 20), catalogue=seasons.BUILTIN_SEASONS
        )
        assert window.season.slug == "halloween"

    def test_a_season_on_its_own_day_beats_one_still_to_come(self):
        """Its own day is the nearest a season can be: Halloween holds 31 Oct against an early Christmas."""
        window = seasons.shown_on(
            ["halloween", "christmas"], 55, 0, date(2026, 10, 31), catalogue=seasons.BUILTIN_SEASONS
        )
        assert window.season.slug == "halloween"

    def test_unknown_slugs_are_ignored_rather_than_raising(self):
        """A retired season in an old database must not crash every run — it simply never shows."""
        assert (
            seasons.shown_on(
                ["easter", "halloween"], 30, 0, date(2026, 10, 5), catalogue=seasons.BUILTIN_SEASONS
            ).season.slug
            == "halloween"
        )


class TestBuildOn:
    def test_the_night_before_a_season_starts_builds_it(self):
        """The pre-build night: the run before the window builds the row (hidden), so the midnight flip
        shows a fresh row rather than last season's."""
        assert seasons.shown_on(["halloween"], 30, 0, date(2026, 9, 30), catalogue=seasons.BUILTIN_SEASONS) is None
        assert (
            seasons.build_on(["halloween"], 30, 0, date(2026, 9, 30), catalogue=seasons.BUILTIN_SEASONS).season.slug
            == "halloween"
        )

    def test_two_nights_before_is_dormant(self):
        assert seasons.build_on(["halloween"], 30, 0, date(2026, 9, 29), catalogue=seasons.BUILTIN_SEASONS) is None

    def test_in_season_it_builds_the_season_it_shows(self):
        assert (
            seasons.build_on(["halloween"], 30, 0, date(2026, 10, 15), catalogue=seasons.BUILTIN_SEASONS).season.slug
            == "halloween"
        )

    def test_a_hand_over_builds_todays_season_not_tomorrows(self):
        """Halloween is on screen on its own day; switching the row to Christmas at 03:30 on 31 Oct would
        put Christmas picks on Halloween."""
        window = seasons.build_on(
            ["halloween", "christmas"], 55, 0, date(2026, 10, 31), catalogue=seasons.BUILTIN_SEASONS
        )
        assert window.season.slug == "halloween"

    def test_the_day_after_a_season_ends_is_dormant(self):
        assert seasons.build_on(["halloween"], 30, 0, date(2026, 11, 1), catalogue=seasons.BUILTIN_SEASONS) is None


class TestNextAfter:
    def test_it_names_the_next_window_to_start(self):
        window = seasons.next_after(ALL, 30, 0, date(2026, 11, 10), catalogue=seasons.BUILTIN_SEASONS)
        assert (window.season.slug, window.starts) == ("christmas", date(2026, 11, 25))

    def test_it_wraps_into_next_year(self):
        window = seasons.next_after(ALL, 30, 0, date(2026, 12, 26), catalogue=seasons.BUILTIN_SEASONS)
        assert (window.season.slug, window.starts) == ("valentines", date(2027, 1, 15))

    def test_a_window_already_open_is_not_next(self):
        window = seasons.next_after(["halloween"], 30, 0, date(2026, 10, 10), catalogue=seasons.BUILTIN_SEASONS)
        assert window.starts == date(2027, 10, 1)

    def test_no_seasons_has_no_next(self):
        assert seasons.next_after([], 30, 0, date(2026, 10, 10), catalogue=seasons.BUILTIN_SEASONS) is None


CHRISTMAS_KEYWORDS = "207317|272698|193048|255088|186933|5570|196450|1991|260365"


class _Tmdb:
    """Answers `discover_all` from a table keyed by (media type, the param that defines the query), and
    `list_item` from a table of single titles (None = TMDB no longer has it)."""

    def __init__(
        self,
        answers: dict[tuple[MediaType, str], list[dict]],
        details: dict[int, dict | None] | None = None,
    ):
        self.answers = answers
        self.details = details or {}
        self.queries: list[tuple[MediaType, dict]] = []
        self.looked_up: list[int] = []

    def discover_all(self, media_type: MediaType, params: dict, *, workers: int = 1) -> list[dict]:
        self.queries.append((media_type, dict(params)))
        key = params.get("with_keywords") or params.get("with_genres")
        return self.answers.get((media_type, key), [])

    def list_item(self, tmdb_id: int, media_type: MediaType) -> dict | None:
        self.looked_up.append(tmdb_id)
        return self.details.get(tmdb_id)


class _Plex:
    """Answers `collection_members` from a table; a collection not in it is absent tonight (None)."""

    def __init__(self, members: dict[tuple[str, str], list[LibraryTitle] | None]) -> None:
        self.members = members

    def collection_members(self, section_key: str, title: str) -> list[LibraryTitle] | None:
        return self.members.get((section_key, title))


NO_PLEX = _Plex({})


def _item(tmdb_id: int, genres: tuple[int, ...] = ()) -> dict:
    return {"id": tmdb_id, "title": f"t{tmdb_id}", "genre_ids": list(genres), "vote_average": 7.0, "vote_count": 100}


def _custom_season(**overrides) -> seasons.Season:
    """A custom season with no sources unless given; ``excluded`` is ``keyword_excluded_genres``."""
    if "excluded" in overrides:
        overrides["keyword_excluded_genres"] = overrides.pop("excluded")
    return seasons.Season(
        slug="c", name="C", emoji="*", rule=seasons.DateRule("fixed", month=3, day=17), description="", **overrides
    )


class TestLoadTitles:
    HALLOWEEN_KEYWORDS = "3335|180193|232795|9694|182794"

    def test_films_are_the_keywords_and_the_genre_and_shows_are_the_keywords_alone(self):
        tmdb = _Tmdb(
            {
                (MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [{"id": 1}, {"id": 2}],
                (MediaType.MOVIE, "27"): [{"id": 2}, {"id": 3}],
                (MediaType.SHOW, self.HALLOWEEN_KEYWORDS): [{"id": 1}],
            }
        )
        titles = seasons.load_titles(
            tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}}
        )
        assert titles.ids[MediaType.MOVIE] == frozenset({1, 2, 3})
        assert titles.ids[MediaType.SHOW] == frozenset({1})
        # Movie 1 and show 1 are different titles: TMDB ids are unique only within a media type.
        assert (MediaType.SHOW, {"with_genres": "27", "vote_count.gte": 200}) not in tmdb.queries

    def test_the_genre_query_keeps_to_well_voted_titles_and_the_keyword_query_does_not(self):
        """Christmas TV films are exactly the season and often have a handful of votes; a whole genre has
        tens of thousands of titles, so it takes the same floor the discover source uses."""
        tmdb = _Tmdb({})
        seasons.load_titles(
            tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}}
        )
        assert (MediaType.MOVIE, {"with_genres": "27", "vote_count.gte": 200}) in tmdb.queries
        assert (MediaType.MOVIE, {"with_keywords": self.HALLOWEEN_KEYWORDS}) in tmdb.queries

    def test_a_season_with_no_genre_makes_no_genre_query(self):
        tmdb = _Tmdb({})
        seasons.load_titles(
            tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["christmas"], {MediaType.MOVIE: {}, MediaType.SHOW: {}}
        )
        assert [params for _media, params in tmdb.queries if "with_genres" in params] == []

    def test_only_titles_the_library_holds_are_kept_as_candidates(self):
        """The season source adds these to a person's pool. Anything else would become request demand for
        thousands of films nobody asked for."""
        tmdb = _Tmdb(
            {(MediaType.MOVIE, "207317|272698|193048|255088|186933|5570|196450|1991|260365"): [{"id": 1}, {"id": 2}]}
        )
        library = {MediaType.MOVIE: {2: 9002}, MediaType.SHOW: {1: 9101}}
        titles = seasons.load_titles(tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["christmas"], library)
        assert [item["id"] for item in titles.in_library[MediaType.MOVIE]] == [2]
        assert titles.in_library[MediaType.SHOW] == []
        assert titles.ids[MediaType.MOVIE] == frozenset({1, 2})

    def test_a_title_both_queries_return_is_one_candidate(self):
        tmdb = _Tmdb({(MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [{"id": 2}], (MediaType.MOVIE, "27"): [{"id": 2}]})
        titles = seasons.load_titles(
            tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["halloween"], {MediaType.MOVIE: {2: 1}, MediaType.SHOW: {}}
        )
        assert [item["id"] for item in titles.in_library[MediaType.MOVIE]] == [2]

    def test_a_halloween_romance_or_drama_is_not_a_halloween_film(self):
        """TMDB tags any film with a Halloween scene. On a real library that let "When We First Met"
        (a rom-com) and "War Pony" (a drama) into Halloween rows; the family and comedy Halloween films
        — Hocus Pocus, Casper — are what the keywords are there to add."""
        tmdb = _Tmdb(
            {
                (MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [
                    {"id": 1, "genre_ids": [35, 10749, 14]},  # comedy, romance, fantasy
                    {"id": 2, "genre_ids": [18]},  # drama
                    {"id": 3, "genre_ids": [14, 35, 10751]},  # fantasy, comedy, family
                    {"id": 4, "genre_ids": [27, 18]},  # a horror drama, through the keyword
                ],
                (MediaType.MOVIE, "27"): [{"id": 5, "genre_ids": [27, 18]}],
            }
        )
        titles = seasons.load_titles(
            tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}}
        )
        assert titles.ids[MediaType.MOVIE] == frozenset({3, 4, 5})

    def test_christmas_keeps_its_romances(self):
        """Love Actually is a Christmas film. The exclusion is Halloween's, not every season's."""
        tmdb = _Tmdb({(MediaType.MOVIE, CHRISTMAS_KEYWORDS): [{"id": 1, "genre_ids": [35, 10749, 18]}]})
        titles = seasons.load_titles(
            tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["christmas"], {MediaType.MOVIE: {}, MediaType.SHOW: {}}
        )
        assert titles.ids[MediaType.MOVIE] == frozenset({1})

    def test_contains_asks_by_media_type(self):
        tmdb = _Tmdb({(MediaType.MOVIE, self.HALLOWEEN_KEYWORDS): [{"id": 5}]})
        titles = seasons.load_titles(
            tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["halloween"], {MediaType.MOVIE: {}, MediaType.SHOW: {}}
        )
        assert titles.contains(5, MediaType.MOVIE) is True
        assert titles.contains(5, MediaType.SHOW) is False


class TestLoadTitlesFromEverySource:
    """A custom season's films come from tags, a genre, Plex collections and hand picks (#137 D3)."""

    LIB: ClassVar[dict[MediaType, dict[int, int]]] = {
        MediaType.MOVIE: {1: 11, 2: 12, 3: 13, 50: 150, 60: 160},
        MediaType.SHOW: {},
    }

    def test_a_season_with_no_tags_never_runs_a_keyword_query(self) -> None:
        """An empty ``with_keywords`` is no filter at all to TMDB, so it would read ALL of TMDB."""
        tmdb = _Tmdb({}, {50: _item(50)})
        season = _custom_season(picks=((50, MediaType.MOVIE),))
        titles = seasons.load_titles(tmdb, NO_PLEX, season, self.LIB)
        assert not [query for query in tmdb.queries if "with_keywords" in query[1]]
        assert titles.contains(50, MediaType.MOVIE)

    def test_collection_members_join_the_season_and_are_in_library(self) -> None:
        plex = _Plex({("1", "Father's Day Movies"): [LibraryTitle(60, MediaType.MOVIE, "Big Fish", 2003)]})
        tmdb = _Tmdb({}, {60: _item(60, (18,))})
        season = _custom_season(collections=(seasons.CollectionRef("1", "Father's Day Movies"),))
        titles = seasons.load_titles(tmdb, plex, season, self.LIB)
        assert [item["id"] for item in titles.in_library[MediaType.MOVIE]] == [60]
        assert titles.in_library[MediaType.MOVIE][0]["genre_ids"] == [18]

    def test_a_missing_collection_is_reported_not_raised(self) -> None:
        """Kometa deletes its seasonal collections out of season (D5); a night without one builds from the rest."""
        tmdb = _Tmdb({(MediaType.MOVIE, "1"): [_item(1)]})
        season = _custom_season(keywords=(1,), collections=(seasons.CollectionRef("1", "Thanksgiving Movies"),))
        titles = seasons.load_titles(tmdb, NO_PLEX, season, self.LIB)
        assert titles.missing_collections == ("Thanksgiving Movies",)
        assert titles.contains(1, MediaType.MOVIE)

    def test_left_out_genres_drop_tag_and_collection_films_but_never_a_hand_pick(self) -> None:
        plex = _Plex({("1", "C"): [LibraryTitle(60, MediaType.MOVIE, "Scary", 2000)]})
        tmdb = _Tmdb(
            {(MediaType.MOVIE, "1"): [_item(1, (27,)), _item(2, (35,))]},
            {60: _item(60, (27,)), 50: _item(50, (27,))},
        )
        season = _custom_season(
            keywords=(1,),
            excluded=(27,),
            collections=(seasons.CollectionRef("1", "C"),),
            picks=((50, MediaType.MOVIE),),
        )
        ids = {item["id"] for item in seasons.load_titles(tmdb, plex, season, self.LIB).in_library[MediaType.MOVIE]}
        assert ids == {2, 50}

    def test_a_collection_film_with_the_seasons_own_genre_is_kept(self) -> None:
        """The same rule as a tag film: a horror drama is still horror."""
        plex = _Plex({("1", "C"): [LibraryTitle(60, MediaType.MOVIE, "Horror Drama", 2000)]})
        tmdb = _Tmdb({}, {60: _item(60, (27, 18))})
        season = _custom_season(movie_genres=(27,), excluded=(18,), collections=(seasons.CollectionRef("1", "C"),))
        assert seasons.load_titles(tmdb, plex, season, self.LIB).contains(60, MediaType.MOVIE)

    def test_a_title_tmdb_no_longer_has_is_skipped(self) -> None:
        tmdb = _Tmdb({}, {50: None})
        season = _custom_season(picks=((50, MediaType.MOVIE),))
        titles = seasons.load_titles(tmdb, NO_PLEX, season, self.LIB)
        assert titles.in_library[MediaType.MOVIE] == []
        assert not titles.contains(50, MediaType.MOVIE)

    def test_a_picked_show_is_a_show(self) -> None:
        tmdb = _Tmdb({}, {50: _item(50)})
        season = _custom_season(picks=((50, MediaType.SHOW),))
        titles = seasons.load_titles(tmdb, NO_PLEX, season, {MediaType.MOVIE: {50: 1}, MediaType.SHOW: {50: 2}})
        assert titles.contains(50, MediaType.SHOW) and not titles.contains(50, MediaType.MOVIE)
        assert [item["id"] for item in titles.in_library[MediaType.SHOW]] == [50]

    def test_a_title_tmdb_already_listed_is_not_looked_up_again(self) -> None:
        plex = _Plex(
            {
                ("1", "C"): [
                    LibraryTitle(1, MediaType.MOVIE, "One", 2000),
                    LibraryTitle(60, MediaType.MOVIE, "Sixty", 2000),
                ]
            }
        )
        tmdb = _Tmdb({(MediaType.MOVIE, "1"): [_item(1)]}, {60: _item(60)})
        season = _custom_season(
            keywords=(1,), collections=(seasons.CollectionRef("1", "C"),), picks=((60, MediaType.MOVIE),)
        )
        titles = seasons.load_titles(tmdb, plex, season, self.LIB)
        assert tmdb.looked_up == [60]
        assert titles.ids[MediaType.MOVIE] == frozenset({1, 60})

    def test_plex_sourced_titles_stop_at_the_cap_and_picks_come_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(seasons, "MAX_PLEX_SOURCED", 2)
        plex = _Plex({("1", "C"): [LibraryTitle(i, MediaType.MOVIE, f"t{i}", 2000) for i in (60, 61, 62)]})
        tmdb = _Tmdb({}, {i: _item(i) for i in (50, 60, 61, 62)})
        season = _custom_season(collections=(seasons.CollectionRef("1", "C"),), picks=((50, MediaType.MOVIE),))
        titles = seasons.load_titles(tmdb, plex, season, self.LIB)
        assert sorted(tmdb.looked_up) == [50, 60]
        assert titles.ids[MediaType.MOVIE] == frozenset({50, 60})

    def test_a_tmdb_failure_on_one_title_fails_the_season(self) -> None:
        """Only a title TMDB no longer has is skipped; an outage must not build the season from part of it."""

        class _Down(_Tmdb):
            def list_item(self, tmdb_id: int, media_type: MediaType) -> dict | None:
                raise RuntimeError("TMDB API error HTTP 503 for /movie/50")

        with pytest.raises(RuntimeError, match="503"):
            seasons.load_titles(_Down({}), NO_PLEX, _custom_season(picks=((50, MediaType.MOVIE),)), self.LIB)

    def test_built_in_queries_are_unchanged(self) -> None:
        tmdb = _Tmdb({})
        seasons.load_titles(tmdb, NO_PLEX, seasons.BUILTIN_SEASONS["halloween"], self.LIB)
        assert tmdb.queries == [
            (MediaType.MOVIE, {"with_keywords": "3335|180193|232795|9694|182794"}),
            (MediaType.SHOW, {"with_keywords": "3335|180193|232795|9694|182794"}),
            (MediaType.MOVIE, {"with_genres": "27", "vote_count.gte": 200}),
        ]
        assert tmdb.looked_up == []


class TestContentHash:
    def test_changes_with_sources_not_with_name_or_timing(self) -> None:
        a = _custom_season(keywords=(1,))
        assert seasons.season_content_hash(a) == seasons.season_content_hash(
            dataclasses.replace(a, name="Other", lead_days=3)
        )
        assert seasons.season_content_hash(a) != seasons.season_content_hash(dataclasses.replace(a, keywords=(1, 2)))

    @pytest.mark.parametrize(
        "change",
        [
            {"movie_genres": (27,)},
            {"keyword_excluded_genres": (18,)},
            {"collections": (seasons.CollectionRef("1", "C"),)},
            {"picks": ((1, MediaType.SHOW),)},
        ],
    )
    def test_every_source_changes_it(self, change: dict) -> None:
        a = _custom_season(keywords=(1,), picks=((1, MediaType.MOVIE),))
        assert seasons.season_content_hash(a) != seasons.season_content_hash(dataclasses.replace(a, **change))

    def test_order_does_not_change_it_and_it_fits_the_recipe(self) -> None:
        """The recipe column is 128 characters, so the hash stays short."""
        a = _custom_season(keywords=(1, 2), picks=((5, MediaType.MOVIE), (4, MediaType.SHOW)))
        b = _custom_season(keywords=(2, 1), picks=((4, MediaType.SHOW), (5, MediaType.MOVIE)))
        assert seasons.season_content_hash(a) == seasons.season_content_hash(b)
        assert len(seasons.season_content_hash(a)) == 16


class TestRowShownToday:
    """The one place a row's weekdays and its seasons combine into "is it on Plex today"."""

    def test_a_row_without_seasons_follows_its_weekdays_alone(self):
        monday = datetime(2026, 8, 31, 12, 0)
        assert row_shown_today([1], [], 30, 0, monday, catalogue=seasons.BUILTIN_SEASONS) is True
        assert row_shown_today([2], [], 30, 0, monday, catalogue=seasons.BUILTIN_SEASONS) is False

    def test_a_seasonal_row_is_hidden_out_of_season_on_every_day(self):
        assert (
            row_shown_today([], ["halloween"], 30, 0, datetime(2026, 9, 15, 12, 0), catalogue=seasons.BUILTIN_SEASONS)
            is False
        )

    def test_a_seasonal_row_is_shown_in_season(self):
        assert (
            row_shown_today([], ["halloween"], 30, 0, datetime(2026, 10, 15, 12, 0), catalogue=seasons.BUILTIN_SEASONS)
            is True
        )

    def test_weekdays_narrow_a_season(self):
        # 2026-10-15 is a Thursday (ISO 4).
        assert (
            row_shown_today([1], ["halloween"], 30, 0, datetime(2026, 10, 15, 12, 0), catalogue=seasons.BUILTIN_SEASONS)
            is False
        )
        assert (
            row_shown_today([4], ["halloween"], 30, 0, datetime(2026, 10, 15, 12, 0), catalogue=seasons.BUILTIN_SEASONS)
            is True
        )


class TestEasterSunday:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            (2024, date(2024, 3, 31)),
            (2025, date(2025, 4, 20)),
            (2026, date(2026, 4, 5)),
            (2027, date(2027, 3, 28)),
            (2028, date(2028, 4, 16)),
            (2029, date(2029, 4, 1)),
            (2030, date(2030, 4, 21)),
        ],
    )
    def test_matches_the_published_dates(self, year: int, expected: date) -> None:
        assert seasons.easter_sunday(year) == expected


class TestDateRule:
    def test_fixed(self) -> None:
        assert seasons.DateRule("fixed", month=3, day=17).anchor(2027) == date(2027, 3, 17)

    @pytest.mark.parametrize(
        ("rule", "year", "expected"),
        [
            (seasons.DateRule("nth", month=11, nth=4, weekday=3), 2026, date(2026, 11, 26)),  # US Thanksgiving
            (seasons.DateRule("nth", month=10, nth=2, weekday=0), 2026, date(2026, 10, 12)),  # Canadian Thanksgiving
            (seasons.DateRule("nth", month=9, nth=1, weekday=6), 2027, date(2027, 9, 5)),  # AU Father's Day
            (seasons.DateRule("nth", month=5, nth=-1, weekday=0), 2026, date(2026, 5, 25)),  # last Monday of May
            (seasons.DateRule("nth", month=6, nth=3, weekday=6), 2026, date(2026, 6, 21)),  # US Father's Day
        ],
    )
    def test_nth_weekday(self, rule: seasons.DateRule, year: int, expected: date) -> None:
        assert rule.anchor(year) == expected

    def test_easter_offset(self) -> None:
        assert seasons.DateRule("easter", offset=-21).anchor(2027) == date(2027, 3, 7)  # Mothering Sunday

    @pytest.mark.parametrize(
        ("rule", "label"),
        [
            (seasons.DateRule("fixed", month=3, day=17), "17 March"),
            (seasons.DateRule("nth", month=11, nth=4, weekday=3), "4th Thursday of November"),
            (seasons.DateRule("nth", month=5, nth=-1, weekday=0), "Last Monday of May"),
            (seasons.DateRule("easter"), "Easter Sunday"),
            (seasons.DateRule("easter", offset=-21), "21 days before Easter"),
            (seasons.DateRule("easter", offset=1), "1 day after Easter"),
        ],
    )
    def test_label(self, rule: seasons.DateRule, label: str) -> None:
        assert rule.label() == label

    @pytest.mark.parametrize(
        ("rule", "message"),
        [
            (seasons.DateRule("fixed", month=2, day=29), "29 February isn't every year"),
            (seasons.DateRule("fixed", month=4, day=31), "April has 30 days"),
            (seasons.DateRule("nth", month=11, nth=5, weekday=3), "1st to 4th, or last"),
            (seasons.DateRule("easter", offset=64), "within 63 days of Easter"),
            (seasons.DateRule("monthly"), "unknown kind of date"),
        ],
    )
    def test_validate_refuses_with_a_message_the_owner_can_act_on(self, rule: seasons.DateRule, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            rule.validate()


def _custom(slug: str, rule: seasons.DateRule, lead: int | None = None, after: int | None = None) -> seasons.Season:
    return seasons.Season(
        slug=slug,
        name=slug.title(),
        emoji="*",
        rule=rule,
        description="",
        keywords=(1,),
        lead_days=lead,
        after_days=after,
    )


class TestPerSeasonTiming:
    def test_a_custom_season_uses_its_own_lead_not_the_rows(self) -> None:
        pat = _custom("pat", seasons.DateRule("fixed", month=3, day=17), lead=7, after=0)
        catalogue = {**seasons.BUILTIN_SEASONS, "pat": pat}
        assert seasons.shown_on(["pat"], 30, 0, date(2027, 3, 9), catalogue=catalogue) is None
        assert seasons.shown_on(["pat"], 30, 0, date(2027, 3, 10), catalogue=catalogue).season is pat

    def test_built_ins_still_use_the_rows_lead(self) -> None:
        window = seasons.shown_on(["christmas"], 30, 0, date(2026, 11, 25), catalogue=seasons.BUILTIN_SEASONS)
        assert window is not None and window.anchor == date(2026, 12, 25)

    def test_new_years_eve_with_a_day_after_crosses_the_year(self) -> None:
        nye = _custom("nye", seasons.DateRule("fixed", month=12, day=31), lead=7, after=1)
        window = seasons.shown_on(["nye"], 30, 0, date(2027, 1, 1), catalogue={"nye": nye})
        assert window is not None and window.anchor == date(2026, 12, 31)

    def test_a_moving_date_moves(self) -> None:
        thx = _custom("thx", seasons.DateRule("nth", month=11, nth=4, weekday=3), lead=0, after=0)
        assert seasons.shown_on(["thx"], 30, 0, date(2026, 11, 26), catalogue={"thx": thx}) is not None
        assert seasons.shown_on(["thx"], 30, 0, date(2027, 11, 26), catalogue={"thx": thx}) is None  # 2027: 25 Nov


class TestCatalogueArgument:
    def test_normalise_orders_by_calendar_and_knows_custom_slugs(self) -> None:
        pat = _custom("pat", seasons.DateRule("fixed", month=3, day=17))
        catalogue = {**seasons.BUILTIN_SEASONS, "pat": pat}
        assert seasons.normalise_slugs(["christmas", "pat", "valentines"], catalogue=catalogue) == [
            "valentines",
            "pat",
            "christmas",
        ]

    def test_unknown_slug_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown season"):
            seasons.normalise_slugs(["nope"], catalogue=seasons.BUILTIN_SEASONS)

    def test_next_anchors(self) -> None:
        thx = _custom("thx", seasons.DateRule("nth", month=11, nth=4, weekday=3))
        assert seasons.next_anchors(thx, date(2026, 11, 27)) == [date(2027, 11, 25), date(2028, 11, 23)]
        assert seasons.next_anchors(thx, date(2026, 11, 26)) == [date(2026, 11, 26), date(2027, 11, 25)]

    def test_the_spec_carries_a_custom_seasons_content_hash_and_a_built_ins_carries_none(self) -> None:
        """The hash is what rebuilds a row when its season's sources change (D11); a built-in has none, so
        its recipe stays byte-identical."""
        pat = dataclasses.replace(_custom("pat", seasons.DateRule("fixed", month=3, day=17)), content_hash="abc")
        catalogue = {**seasons.BUILTIN_SEASONS, "pat": pat}
        assert seasons.row_season_on(["pat"], 30, 0, date(2027, 3, 1), catalogue=catalogue).content_hash == "abc"
        christmas = seasons.row_season_on(["christmas"], 30, 0, date(2026, 12, 1), catalogue=catalogue)
        assert christmas.content_hash == ""


class TestPresets:
    """The ready-made seasons the editor offers (#137 D9), exactly as the spec's table measured them."""

    #: The spec's table (`.claude/docs/issue-137-custom-seasons.md`, "Presets"), tag ids verified on TMDB.
    SPEC_TAGS: ClassVar[dict[str, tuple[int, ...]]] = {
        "new_years_eve": (613, 252123),
        "fourth_of_july": (235503, 159743, 282190, 190024, 2407),
        "thanksgiving_us": (4543,),
        "thanksgiving_ca": (4543,),
        "st_patricks_day": (209352, 10310, 14985, 299594, 4729),
        "easter": (9921, 9923),
        "mothers_day": (173983,),
        "mothering_sunday": (173983,),
        "fathers_day": (),
        "fathers_day_au_nz": (),
    }
    #: Each preset's day in 2026 — Easter fell on 5 April — and its (lead, after) timing.
    SPEC_DAYS: ClassVar[dict[str, tuple[date, int, int]]] = {
        "new_years_eve": (date(2026, 12, 31), 7, 1),
        "fourth_of_july": (date(2026, 7, 4), 7, 0),
        "thanksgiving_us": (date(2026, 11, 26), 14, 0),
        "thanksgiving_ca": (date(2026, 10, 12), 7, 0),
        "st_patricks_day": (date(2026, 3, 17), 7, 0),
        "easter": (date(2026, 4, 5), 14, 0),
        "mothers_day": (date(2026, 5, 10), 7, 0),
        "mothering_sunday": (date(2026, 3, 15), 7, 0),
        "fathers_day": (date(2026, 6, 21), 7, 0),
        "fathers_day_au_nz": (date(2026, 9, 6), 7, 0),
    }

    def test_tag_ids_are_the_spec_tables(self) -> None:
        assert {preset.key: preset.season.keywords for preset in seasons.PRESETS} == self.SPEC_TAGS

    def test_each_falls_on_its_day_with_its_own_timing(self) -> None:
        assert {
            p.key: (p.season.rule.anchor(2026), p.season.lead_days, p.season.after_days) for p in seasons.PRESETS
        } == self.SPEC_DAYS

    def test_every_preset_validates_and_fits_what_the_editor_accepts(self) -> None:
        builtin_names = {season.name.casefold() for season in seasons.BUILTIN_SEASONS.values()}
        for preset in seasons.PRESETS:
            preset.season.rule.validate()
            assert 1 <= len(preset.season.name) <= 40 and 1 <= len(preset.season.emoji) <= 8, preset.key
            assert 0 <= preset.season.lead_days <= seasons.MAX_LEAD_DAYS, preset.key
            assert 0 <= preset.season.after_days <= seasons.MAX_AFTER_DAYS, preset.key
            assert len(preset.key) <= 32, "seasons.preset is String(32)"
            assert preset.season.name.casefold() not in builtin_names, "names are unique, built-ins included"
        assert len({p.key for p in seasons.PRESETS}) == len(seasons.PRESETS)
        assert len({p.season.name.casefold() for p in seasons.PRESETS}) == len(seasons.PRESETS)

    def test_every_preset_tag_has_its_tmdb_name(self) -> None:
        """The editor shows a tag by name, and a preset is saved without a TMDB lookup."""
        for preset in seasons.PRESETS:
            for tag in preset.season.keywords:
                assert seasons.PRESET_TAG_NAMES[tag], (preset.key, tag)

    def test_fathers_day_has_no_tag_and_says_what_to_add_instead(self) -> None:
        """TMDB has no Father's Day tag: the editor opens it empty and asks for a collection or picks."""
        for key in ("fathers_day", "fathers_day_au_nz"):
            preset = next(p for p in seasons.PRESETS if p.key == key)
            assert preset.season.keywords == ()
            assert "collection" in preset.note and "picks" in preset.note

    def test_st_patricks_day_leaves_out_horror(self) -> None:
        """The leprechaun tag otherwise brings in the *Leprechaun* slashers."""
        preset = next(p for p in seasons.PRESETS if p.key == "st_patricks_day")
        assert preset.season.keyword_excluded_genres == (27,)
        assert preset.note


def _voted(tmdb_id: int, votes: int, title: str | None = None) -> dict:
    return {"id": tmdb_id, "title": title or f"t{tmdb_id}", "genre_ids": [], "vote_count": votes}


class _PagedTmdb(_Tmdb):
    """`_Tmdb` that also records how many page workers each list read was given."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.workers: list[int] = []

    def discover_all(self, media_type: MediaType, params: dict, *, workers: int = 1) -> list[dict]:
        self.workers.append(workers)
        return super().discover_all(media_type, params)


class TestPreview:
    """What the season editor shows while the owner builds a season (#137 D10): the count, where it comes
    from, and a sample."""

    LIB: ClassVar[dict[MediaType, dict[int, int]]] = {
        MediaType.MOVIE: {1: 11, 2: 12, 3: 13, 4: 14, 5: 15},
        MediaType.SHOW: {1: 21},
    }
    TODAY = date(2026, 10, 2)

    def _sources(self) -> tuple[_PagedTmdb, _Plex, seasons.Season]:
        """Every source overlaps the one before it, so each marginal count differs from its raw one."""
        show_one = {"id": 1, "name": "Show One", "genre_ids": [], "vote_count": 10}
        tmdb = _PagedTmdb(
            {
                (MediaType.MOVIE, "1|2"): [_voted(1, 50), _voted(2, 900), _voted(9, 5000)],  # 9: not in a library
                (MediaType.SHOW, "1|2"): [show_one],
                (MediaType.MOVIE, "1"): [_voted(1, 50), _voted(2, 900)],
                (MediaType.SHOW, "1"): [show_one],
                (MediaType.MOVIE, "2"): [_voted(2, 900), _voted(9, 5000)],
                (MediaType.MOVIE, "27"): [_voted(2, 900), _voted(3, 300)],
            },
            {4: _voted(4, 70), 5: _voted(5, 2000, "Picked")},
        )
        plex = _Plex(
            {
                ("1", "Kometa"): [
                    LibraryTitle(3, MediaType.MOVIE, "t3", 2000),
                    LibraryTitle(4, MediaType.MOVIE, "t4", 2000),
                ]
            }
        )
        season = _custom_season(
            keywords=(1, 2),
            movie_genres=(27,),
            collections=(seasons.CollectionRef("1", "Kometa"), seasons.CollectionRef("1", "Gone")),
            picks=((4, MediaType.MOVIE), (5, MediaType.MOVIE)),
        )
        return tmdb, plex, season

    def test_marginal_counts_add_up_to_the_total_load_titles_gives(self) -> None:
        tmdb, plex, season = self._sources()
        result = seasons.preview(tmdb, plex, season, self.LIB, today=self.TODAY)
        loaded = seasons.load_titles(tmdb, plex, season, self.LIB)

        assert result.total == sum(len(items) for items in loaded.in_library.values()) == 6
        # Tags: films 1, 2 and show 1. The genre adds 3; the collection adds 4 (3 is the genre's); picks add 5.
        assert (result.from_tags, result.from_genre, result.from_collections, result.from_picks) == (3, 1, 1, 1)
        assert result.from_tags + result.from_genre + result.from_collections + result.from_picks == result.total

    def test_each_tag_is_counted_on_its_own_and_only_in_the_libraries(self) -> None:
        tmdb, plex, season = self._sources()
        result = seasons.preview(tmdb, plex, season, self.LIB, today=self.TODAY)
        assert result.per_tag == {1: 3, 2: 1}
        assert (MediaType.MOVIE, {"with_keywords": "2"}) in tmdb.queries

    def test_each_collection_says_whether_it_was_found_and_what_it_gives(self) -> None:
        tmdb, plex, season = self._sources()
        result = seasons.preview(tmdb, plex, season, self.LIB, today=self.TODAY)
        assert result.per_collection == (
            seasons.CollectionCount(title="Kometa", section_key="1", found=True, in_library=2),
            seasons.CollectionCount(title="Gone", section_key="1", found=False, in_library=0),
        )

    def test_a_left_out_genre_is_left_out_of_the_tag_and_collection_counts_too(self) -> None:
        """Counts must agree with the total, which never holds a film the season leaves out."""
        tmdb = _Tmdb({(MediaType.MOVIE, "1"): [_item(1, (27,)), _item(2)]}, {3: _item(3, (27,))})
        plex = _Plex({("1", "C"): [LibraryTitle(3, MediaType.MOVIE, "t3", 2000)]})
        season = _custom_season(keywords=(1,), excluded=(27,), collections=(seasons.CollectionRef("1", "C"),))
        result = seasons.preview(tmdb, plex, season, self.LIB, today=self.TODAY)
        assert (result.total, result.per_tag, result.per_collection[0].in_library) == (1, {1: 1}, 0)

    def test_the_sample_is_the_most_voted_titles_in_the_libraries_picks_and_collections_included(self) -> None:
        tmdb, plex, season = self._sources()
        result = seasons.preview(tmdb, plex, season, self.LIB, today=self.TODAY)
        assert result.sample == ("Picked", "t2", "t3", "t4", "t1", "Show One")

    def test_the_sample_stops_at_ten(self) -> None:
        tmdb = _Tmdb({(MediaType.MOVIE, "1"): [_voted(i, i) for i in range(1, 16)]})
        library = {MediaType.MOVIE: {i: i for i in range(1, 16)}, MediaType.SHOW: {}}
        result = seasons.preview(tmdb, NO_PLEX, _custom_season(keywords=(1,)), library, today=self.TODAY)
        assert result.sample == tuple(f"t{i}" for i in range(15, 5, -1))

    def test_the_next_date_is_on_or_after_today(self) -> None:
        tmdb, plex, season = self._sources()  # 17 March
        result = seasons.preview(tmdb, plex, season, self.LIB, today=self.TODAY)
        assert (result.next_date, result.rule_error) == (date(2027, 3, 17), None)

    @pytest.mark.parametrize(
        ("rule", "message"),
        [
            (seasons.DateRule("fixed", month=2, day=29), "29 February"),
            (seasons.DateRule("nth", month=11, nth=7, weekday=3), "1st to 4th"),
        ],
    )
    def test_an_invalid_rule_still_counts_the_films(self, rule: seasons.DateRule, message: str) -> None:
        """The owner may be mid-way through choosing a date; the films panel must not go blank meanwhile."""
        tmdb, plex, season = self._sources()
        result = seasons.preview(tmdb, plex, dataclasses.replace(season, rule=rule), self.LIB, today=self.TODAY)
        assert result.next_date is None and message in result.rule_error
        assert result.total == 6

    def test_list_reads_get_the_workers_they_are_given(self) -> None:
        """Only the editor reads pages concurrently; a run's reads keep the client's default."""
        tmdb, plex, season = self._sources()
        seasons.preview(tmdb, plex, season, self.LIB, today=self.TODAY, workers=6)
        assert tmdb.workers and set(tmdb.workers) == {6}

    def test_a_season_with_no_sources_reads_nothing(self) -> None:
        tmdb = _PagedTmdb({})
        result = seasons.preview(tmdb, NO_PLEX, _custom_season(), self.LIB, today=self.TODAY)
        assert tmdb.queries == [] and tmdb.looked_up == []
        assert (result.total, result.per_tag, result.per_collection, result.sample) == (0, {}, (), ())
