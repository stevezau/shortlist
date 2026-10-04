"""Themes (#138): the spec's hash, its dateless-season form, and loading with hard rules."""

from __future__ import annotations

import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shortlist.engine.models import MediaType, RowLimits
from shortlist.engine.themes import ThemePick, ThemeSpec, load_theme, theme_as_season, theme_content_hash

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


def test_load_theme_keeps_ai_pick_only_if_in_library_for_in_library_set_but_keeps_id_in_ids():
    pick = ThemePick(9, MediaType.MOVIE, "ai", "Fits the brief")
    tmdb = _Tmdb([_item(1)], items={9: _item(9)})
    spec = _spec(picks=(pick,))

    titles = load_theme(tmdb, _Plex(), spec, {MediaType.MOVIE: {1: 10}})

    assert _ids(titles.titles) == {1, 9}
    assert _library_ids(titles.titles) == {1}
    assert titles.reasons == {(MediaType.MOVIE, 9): "Fits the brief"}


def test_load_theme_raises_when_tmdb_fails():
    tmdb = _Tmdb([_item(1)])
    tmdb.fail = True

    with pytest.raises(RuntimeError):
        load_theme(tmdb, _Plex(), _spec(), {MediaType.MOVIE: {1: 10}})
