"""Themes (#138): the spec's hash, its dateless-season form, and loading with hard rules."""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine.clients.plex_pms import LibraryTitle
from shortlist.engine.models import MediaType, RowLimits
from shortlist.engine.themes import (
    ThemeCollection,
    ThemePick,
    ThemeSpec,
    load_theme,
    theme_as_season,
    theme_content_hash,
)

TAG = 111


def _spec(**overrides) -> ThemeSpec:
    fields = {
        "slug": "twists",
        "name": "Twist endings",
        "emoji": "🌀",
        "media": (MediaType.MOVIE,),
        "tags": (TAG,),
        "genres": (),
        "excluded_genres": (),
        "collections": (),
        "picks": (),
        "rules": RowLimits(),
        "min_votes": None,
    }
    return ThemeSpec(**{**fields, **overrides})


class _Tmdb:
    """discover_all by tag, list_item and details by id; a title not in ``items`` is gone from TMDB."""

    def __init__(self, tagged: list[dict], items: dict[int, dict] | None = None, details: dict | None = None):
        self.tagged = tagged
        self.items = items or {}
        self.detail_table = details or {}
        self.fail = False

    def discover_all(self, media_type: MediaType, params: dict, *, workers: int = 1) -> list[dict]:
        if self.fail:
            raise RuntimeError("tmdb down")
        return self.tagged if media_type is MediaType.MOVIE else []

    def list_item(self, tmdb_id: int, media_type: MediaType) -> dict | None:
        return self.items.get(tmdb_id)

    def details(self, tmdb_id: int, media_type: MediaType) -> dict:
        return self.detail_table.get(tmdb_id, {})


class _Plex:
    def collection_members(self, section_key: str, title: str):
        return None


def _item(tmdb_id: int, year: int = 2000, rating: float = 7.0, votes: int = 500) -> dict:
    return {
        "id": tmdb_id,
        "title": f"t{tmdb_id}",
        "release_date": f"{year}-05-01",
        "vote_average": rating,
        "vote_count": votes,
        "genre_ids": [],
    }


def _ids(titles) -> set[int]:
    return set(titles.ids[MediaType.MOVIE])


def _library_ids(titles) -> set[int]:
    return {int(item["id"]) for item in titles.in_library[MediaType.MOVIE]}


def test_theme_content_hash_is_stable_when_only_name_changes():
    assert theme_content_hash(_spec()) == theme_content_hash(_spec(name="Other", emoji=None))


def test_theme_content_hash_changes_when_a_pick_is_added():
    pick = ThemePick(tmdb_id=5, media=MediaType.MOVIE, origin="ai", reason=None)

    assert theme_content_hash(_spec()) != theme_content_hash(_spec(picks=(pick,)))


def test_theme_content_hash_ignores_a_pick_reason_but_not_a_rule():
    plain = ThemePick(5, MediaType.MOVIE, "ai", None)
    reasoned = ThemePick(5, MediaType.MOVIE, "ai", "because")

    assert theme_content_hash(_spec(picks=(plain,))) == theme_content_hash(_spec(picks=(reasoned,)))
    assert theme_content_hash(_spec()) != theme_content_hash(_spec(rules=RowLimits(max_runtime=120)))
    assert theme_content_hash(_spec()) != theme_content_hash(_spec(min_votes=100))


@given(
    tags=st.lists(st.integers(1, 1000), unique=True, min_size=1),
    pick_ids=st.lists(st.integers(1, 1000), unique=True),
    seed=st.integers(),
)
def test_theme_hash_order_independent(tags, pick_ids, seed):
    picks = [ThemePick(i, MediaType.MOVIE, "ai", None) for i in pick_ids]
    shuffled_tags, shuffled_picks = list(tags), list(picks)
    random.Random(seed).shuffle(shuffled_tags)
    random.Random(seed).shuffle(shuffled_picks)

    one = _spec(tags=tuple(tags), picks=tuple(picks))
    other = _spec(tags=tuple(shuffled_tags), picks=tuple(shuffled_picks))

    assert theme_content_hash(one) == theme_content_hash(other)


def test_theme_as_season_carries_the_sources_and_resolves_genre_names():
    pick = ThemePick(5, MediaType.SHOW, "owner", None)
    spec = _spec(genres=("Thriller",), excluded_genres=("Horror",), picks=(pick,))

    season = theme_as_season(spec)

    assert season.keywords == (TAG,)
    assert season.movie_genres == (53,)
    assert season.keyword_excluded_genres == (27,)
    assert season.picks == ((5, MediaType.SHOW),)
    assert season.slug == "twists"


def test_load_theme_drops_titles_over_max_runtime():
    tmdb = _Tmdb([_item(1), _item(2)], details={1: {"runtime": 130}, 2: {"runtime": 100}})
    spec = _spec(rules=RowLimits(max_runtime=120))

    titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {1: 10, 2: 20}})

    assert _ids(titles.titles) == {2}
    assert _library_ids(titles.titles) == {2}


def test_load_theme_applies_year_rating_votes_rules():
    tmdb = _Tmdb(
        [
            _item(1, year=1985),
            _item(2, year=2005, rating=5.0),
            _item(3, year=2005, rating=8.0, votes=10),
            _item(4, year=2005, rating=8.0, votes=900),
        ]
    )
    spec = _spec(rules=RowLimits(min_year=1990, min_rating=6.0), min_votes=100)
    index = {MediaType.MOVIE: {i: i for i in (1, 2, 3, 4)}}

    titles = load_theme(tmdb, _Plex(), spec, index)

    assert _ids(titles.titles) == {4}
    assert _library_ids(titles.titles) == {4}


def test_load_theme_leaves_out_a_pick_the_libraries_do_not_hold():
    pick = ThemePick(9, MediaType.MOVIE, "ai", "Fits the brief")
    tmdb = _Tmdb([_item(1)], items={9: _item(9)})
    spec = _spec(picks=(pick,))

    titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {1: 10}})

    assert _ids(titles.titles) == {1}
    assert _library_ids(titles.titles) == {1}
    assert titles.reasons == {}
    assert titles.held == 1


def test_load_theme_raises_when_tmdb_fails():
    tmdb = _Tmdb([_item(1)])
    tmdb.fail = True

    with pytest.raises(RuntimeError):
        load_theme(tmdb, _Plex(), _spec(), {MediaType.MOVIE: {1: 10}})


class _StrictPlex:
    """Like the real reader: a collection is found only under its own section key."""

    def __init__(self, section_key: str, members: list[LibraryTitle]):
        self.section_key = section_key
        self.members = members

    def collection_members(self, section_key: str, title: str):
        return self.members if section_key == self.section_key and title == "Mind benders" else None


def _members() -> list[LibraryTitle]:
    return [LibraryTitle(tmdb_id=7, media_type=MediaType.MOVIE, title="t7", year=2000)]


def test_load_theme_reads_collection_members_under_their_section_key():
    tmdb = _Tmdb([], items={7: _item(7)})
    spec = _spec(tags=(), collections=(ThemeCollection("3", "Mind benders"),))

    titles = load_theme(tmdb, _StrictPlex("3", _members()), spec, {MediaType.MOVIE: {7: 70}})

    assert _library_ids(titles.titles) == {7}
    assert titles.titles.missing_collections == ()


def test_load_theme_reports_a_collection_under_the_wrong_section_key_as_missing():
    tmdb = _Tmdb([], items={7: _item(7)})
    spec = _spec(tags=(), collections=(ThemeCollection("9", "Mind benders"),))

    titles = load_theme(tmdb, _StrictPlex("3", _members()), spec, {MediaType.MOVIE: {7: 70}})

    assert _ids(titles.titles) == set()
    assert titles.titles.missing_collections == ("Mind benders",)


def test_theme_content_hash_covers_collection_section_key_and_title():
    one = _spec(collections=(ThemeCollection("3", "A"),))

    assert theme_content_hash(one) != theme_content_hash(_spec(collections=(ThemeCollection("4", "A"),)))
    assert theme_content_hash(one) != theme_content_hash(_spec(collections=(ThemeCollection("3", "B"),)))


def test_load_theme_exempts_picks_from_min_votes_but_not_from_other_rules():
    pick = ThemePick(9, MediaType.MOVIE, "ai", None)
    old = ThemePick(8, MediaType.MOVIE, "ai", None)
    tmdb = _Tmdb(
        [_item(1, votes=10)],
        items={9: _item(9, votes=3), 8: _item(8, year=1950, votes=3)},
    )
    spec = _spec(picks=(pick, old), rules=RowLimits(min_year=1990), min_votes=100)

    titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {1: 1, 8: 8, 9: 9}})

    assert _ids(titles.titles) == {9}


def test_load_theme_makes_no_details_calls_when_rules_are_inactive():
    tmdb = _Tmdb([_item(1)])
    calls: list[int] = []
    tmdb.details = lambda tmdb_id, media_type: calls.append(tmdb_id) or {}  # type: ignore[method-assign]

    titles = load_theme(tmdb, _Plex(), _spec(), {MediaType.MOVIE: {1: 1}})

    assert _ids(titles.titles) == {1}
    assert calls == []


def test_theme_hash_ignores_genre_case_and_alias():
    assert theme_content_hash(_spec(genres=("Science Fiction",))) == theme_content_hash(_spec(genres=("sci-fi",)))
    assert theme_content_hash(_spec(genres=("Horror",))) == theme_content_hash(_spec(genres=("horror",)))


def test_theme_as_season_resolves_scifi_aliases():
    for name in ("sci-fi", "SciFi", "Science Fiction"):
        assert theme_as_season(_spec(genres=(name,))).movie_genres == (878,)


def test_theme_hash_handles_known_and_unknown_genres_in_any_order():
    one = _spec(genres=("Horror", "Foo"), excluded_genres=("Bar", "Comedy"))
    other = _spec(genres=("Foo", "horror"), excluded_genres=("comedy", "Bar"))

    assert theme_content_hash(one) == theme_content_hash(other)


def test_theme_hash_never_confuses_an_unknown_genre_with_a_known_one():
    assert theme_content_hash(_spec(genres=("Horror",))) != theme_content_hash(_spec(genres=("Foo",)))
    assert theme_content_hash(_spec(genres=("id:27",))) != theme_content_hash(_spec(genres=("Horror",)))


def test_load_theme_applies_rules_only_to_titles_the_libraries_hold():
    """A runtime limit costs one details() call per title ON THE SERVER, never one per title TMDB lists."""
    tmdb = _Tmdb([_item(1), _item(2), _item(3), _item(4)])
    calls: list[int] = []
    original = tmdb.details
    tmdb.details = lambda tmdb_id, media: calls.append(tmdb_id) or original(tmdb_id, media)
    spec = _spec(rules=RowLimits(max_runtime=100))

    titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {1: 10, 2: 20}})

    assert sorted(calls) == [1, 2]
    assert titles.held == 2


def test_load_theme_leaves_out_a_kind_the_theme_does_not_cover():
    tmdb = _Tmdb([_item(1)])
    spec = _spec(media=(MediaType.SHOW,))

    titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {1: 10}, MediaType.SHOW: {}})

    assert _ids(titles.titles) == set() and titles.held == 0


def _counting(tmdb: _Tmdb, fail: bool = False) -> list[int]:
    calls: list[int] = []
    original = tmdb.details

    def details(tmdb_id: int, media: MediaType) -> dict:
        calls.append(tmdb_id)
        if fail:
            raise RuntimeError("tmdb down")
        return original(tmdb_id, media)

    tmdb.details = details  # type: ignore[method-assign]
    return calls


class TestRuntimeLookups:
    def test_cheap_rules_run_before_any_details_call(self):
        tmdb = _Tmdb([_item(1, year=1980), _item(2, year=2005)], details={2: {"runtime": 90}})
        calls = _counting(tmdb)
        spec = _spec(rules=RowLimits(max_runtime=100, min_year=2000))

        titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {1: 10, 2: 20}})

        assert calls == [2], "a title failing min_year was asked for its running time"
        assert _ids(titles.titles) == {2}

    def test_concurrent_lookups_give_the_same_titles_in_the_same_order(self):
        ids = list(range(1, 41))
        tagged = [_item(i) for i in ids]
        details = {i: {"runtime": 90 if i % 3 else 150} for i in ids}
        tmdb = _Tmdb(tagged, details=details)
        calls = _counting(tmdb)
        spec = _spec(rules=RowLimits(max_runtime=100))

        titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {i: i for i in ids}})

        assert sorted(calls) == ids
        assert [int(item["id"]) for item in titles.titles.in_library[MediaType.MOVIE]] == [i for i in ids if i % 3]

    def test_max_details_checks_the_named_titles_then_the_most_voted(self):
        tagged = [_item(i, votes=i * 10) for i in range(1, 11)]
        # 1 is named but has the fewest votes; every title is 200 minutes long.
        tmdb = _Tmdb(tagged, details={i: {"runtime": 200} for i in range(1, 11)})
        calls = _counting(tmdb)
        spec = _spec(rules=RowLimits(max_runtime=100), picks=(ThemePick(1, MediaType.MOVIE, "ai", None),))

        titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {i: i for i in range(1, 11)}}, max_details=3)

        assert sorted(calls) == [1, 9, 10]
        assert (titles.runtime_checked, titles.runtime_total) == (3, 10)
        assert _ids(titles.titles) == {2, 3, 4, 5, 6, 7, 8}, "the unchecked titles are kept, the checked long ones go"

    def test_without_max_details_every_survivor_is_checked(self):
        tmdb = _Tmdb([_item(i) for i in range(1, 11)], details={i: {"runtime": 90} for i in range(1, 11)})
        calls = _counting(tmdb)

        titles = load_theme(
            tmdb, _Plex(), _spec(rules=RowLimits(max_runtime=100)), {MediaType.MOVIE: {i: i for i in range(1, 11)}}
        )

        assert sorted(calls) == list(range(1, 11))
        assert (titles.runtime_checked, titles.runtime_total) == (10, 10)

    def test_the_breaker_stops_asking_after_repeated_failures_and_keeps_every_title(self):
        ids = list(range(1, 61))
        tmdb = _Tmdb([_item(i) for i in ids])
        calls = _counting(tmdb, fail=True)

        titles = load_theme(
            tmdb, _Plex(), _spec(rules=RowLimits(max_runtime=100)), {MediaType.MOVIE: {i: i for i in ids}}
        )

        assert len(calls) < 20, "the breaker never opened"
        assert _ids(titles.titles) == set(ids)
