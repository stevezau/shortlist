"""A row's "which libraries get requests" switch (#169): what `_record_demand` records once some are off."""

from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

from shortlist.engine.models import Candidate, MediaType, RequestOverrides, RowSpec
from shortlist.engine.rows import _record_demand, requestable_in_enabled_libraries

MOVIES = SimpleNamespace(type="movie", key="1", title="Movies")
KIDS = SimpleNamespace(type="movie", key=2, title="Kids Movies")
SHOWS = SimpleNamespace(type="show", key="3", title="TV Shows")
ANIME = SimpleNamespace(type="show", key="4", title="Anime")


def _movie(tmdb_id: int) -> Candidate:
    return Candidate(tmdb_id=tmdb_id, title=f"M{tmdb_id}", media_type=MediaType.MOVIE)


def _show(tmdb_id: int) -> Candidate:
    return Candidate(tmdb_id=tmdb_id, title=f"S{tmdb_id}", media_type=MediaType.SHOW)


def _skip(*keys: str) -> RequestOverrides:
    return RequestOverrides(skip_library_keys=keys)


class TestRequestableInEnabledLibraries:
    def test_a_title_is_kept_while_any_library_of_its_type_is_on(self):
        kept = requestable_in_enabled_libraries([_movie(1)], [MOVIES, KIDS], [MOVIES, KIDS], frozenset({"1"}))
        assert [c.tmdb_id for c in kept] == [1]

    def test_a_title_is_dropped_when_every_library_of_its_type_is_off(self):
        missing = [_movie(1), _show(2)]
        kept = requestable_in_enabled_libraries(
            missing, [MOVIES, KIDS, SHOWS], [MOVIES, KIDS, SHOWS], frozenset({"1", "2"})
        )
        assert [c.tmdb_id for c in kept] == [2]

    def test_keys_compare_as_strings(self):
        assert requestable_in_enabled_libraries([_movie(1)], [KIDS], [KIDS], frozenset({"2"})) == []

    def test_a_group_with_no_library_of_the_type_asks_only_when_none_of_the_rows_are_off(self):
        """The main group's pool spans every library's seeds; the HIGH case (Movies, TV Shows, Anime)."""
        row = [MOVIES, SHOWS, ANIME]
        assert requestable_in_enabled_libraries([_show(2)], [MOVIES], row, frozenset({"3"})) == []
        assert requestable_in_enabled_libraries([_show(2)], [MOVIES], row, frozenset({"1"})) == [_show(2)]

    def test_a_group_with_no_library_of_the_type_and_a_row_with_none_drops_it(self):
        assert requestable_in_enabled_libraries([_show(2)], [MOVIES], [MOVIES], frozenset({"1"})) == []
        assert requestable_in_enabled_libraries([_show(2)], [MOVIES], [MOVIES], frozenset({"9"})) == []

    def test_a_title_is_judged_by_the_group_that_holds_its_type(self):
        group = [MOVIES, SHOWS]
        assert requestable_in_enabled_libraries([_show(2)], group, group, frozenset({"1"})) == [_show(2)]
        assert requestable_in_enabled_libraries([_show(2)], group, group, frozenset({"3"})) == []


class _FakePolicy:
    """Just enough of RowPolicy for `_record_demand`: each row is a list of (group sections, pool)."""

    def __init__(self, rows: list[tuple[RowSpec, list[tuple[list, list[Candidate] | None]]]], sections: list):
        self.ctx = SimpleNamespace(delivery_sections=sections, write_lock=threading.Lock())
        self.user = SimpleNamespace(slug="sarah", username="sarah", request_tag="", display_name="Sarah")
        self.cfg = SimpleNamespace(requests=None)
        self.library_index = {}
        self._rows = dict((spec.slug, groups) for spec, groups in rows)
        self._specs = [spec for spec, _ in rows]
        self._groups: dict[str, list] = {}

    def gathered_specs(self):
        return self._specs

    def library_groups(self, spec, targets):
        groups = []
        for i, (sections, pool) in enumerate(self._rows[spec.slug]):
            group_spec = replace(spec, name_template=f"{spec.slug}-{i}")
            self._groups[group_spec.name_template] = pool
            groups.append((group_spec, sections))
        return groups

    def pools_for(self, spec):
        pool = self._groups[spec.name_template]
        return None if pool is None else (pool, [])


def _row(slug: str, overrides: RequestOverrides | None = None) -> RowSpec:
    return RowSpec(slug=slug, name_template=slug, size=5, media="both", request_overrides=overrides)


def _record(rows, sections=(MOVIES, KIDS, SHOWS)) -> dict[str, set]:
    demand: dict = {}
    _record_demand(_FakePolicy(rows, list(sections)), demand)
    return {slug: set(titles) for slug, titles in demand.items()}


class TestRecordDemand:
    def test_nothing_skipped_records_everything_even_a_type_the_row_has_no_library_for(self):
        pool = [_movie(1), _show(2)]
        for overrides in (None, RequestOverrides(min_rating=5.0)):
            got = _record([(_row("r", overrides), [([MOVIES], pool)])], sections=[MOVIES])
            assert got == {"r": {(1, MediaType.MOVIE), (2, MediaType.SHOW)}}

    def test_tv_off_on_a_movies_and_tv_row_drops_shows_and_keeps_movies(self):
        spec = _row("r", _skip("3"))
        got = _record([(spec, [([MOVIES, SHOWS], [_movie(1), _show(2)])])], sections=[MOVIES, SHOWS])
        assert got == {"r": {(1, MediaType.MOVIE)}}

    def test_a_movie_surfaced_only_by_the_off_split_library_is_dropped(self):
        spec = _row("r", _skip("2"))
        got = _record(
            [(spec, [([MOVIES], [_movie(1)]), ([KIDS], [_movie(5)])])],
            sections=[MOVIES, KIDS],
        )
        assert got == {"r": {(1, MediaType.MOVIE)}}

    def test_a_movie_both_split_libraries_surface_stays_while_one_of_them_is_on(self):
        spec = _row("r", _skip("2"))
        got = _record([(spec, [([MOVIES], [_movie(1)]), ([KIDS], [_movie(1), _movie(5)])])], sections=[MOVIES, KIDS])
        assert got == {"r": {(1, MediaType.MOVIE)}}

    def test_a_stale_key_narrows_nothing(self):
        """A deleted library's key still sits in the saved list; every switch shows on, so nothing is skipped."""
        pool = [_movie(1), _show(2)]
        with_stale = _record([(_row("r", _skip("99")), [([MOVIES, SHOWS], pool)])], sections=[MOVIES, SHOWS])
        without = _record([(_row("r"), [([MOVIES, SHOWS], pool)])], sections=[MOVIES, SHOWS])
        assert with_stale == without == {"r": {(1, MediaType.MOVIE), (2, MediaType.SHOW)}}

    def test_main_group_movies_are_asked_for_while_both_movie_libraries_are_on(self):
        """Movies and Kids Movies are split off; the main group is TV Shows alone, its pool holding movies too."""
        spec = _row("r", _skip("3"))
        got = _record(
            [
                (
                    spec,
                    [
                        ([SHOWS], [_movie(7), _show(9)]),
                        ([MOVIES], [_movie(1)]),
                        ([KIDS], [_movie(5)]),
                    ],
                )
            ],
            sections=[MOVIES, KIDS, SHOWS],
        )
        assert got == {"r": {(1, MediaType.MOVIE), (5, MediaType.MOVIE), (7, MediaType.MOVIE)}}

    def test_main_group_movies_are_dropped_once_either_movie_library_is_off(self):
        spec = _row("r", _skip("2"))
        got = _record(
            [(spec, [([SHOWS], [_movie(7), _show(9)]), ([MOVIES], [_movie(1)]), ([KIDS], [_movie(5)])])],
            sections=[MOVIES, KIDS, SHOWS],
        )
        assert got == {"r": {(1, MediaType.MOVIE), (9, MediaType.SHOW)}}

    def test_every_library_off_records_nothing_for_that_row_and_leaves_the_others(self):
        off = _row("off", _skip("1", "2", "3"))
        other = _row("other")
        got = _record(
            [
                (off, [([MOVIES, KIDS, SHOWS], [_movie(1), _show(2)])]),
                (other, [([MOVIES, KIDS, SHOWS], [_movie(1)])]),
            ]
        )
        assert got["off"] == set()
        assert got["other"] == {(1, MediaType.MOVIE)}

    def test_a_row_with_every_pool_unreadable_is_still_skipped_entirely(self):
        got = _record([(_row("r", _skip("1")), [([MOVIES], None)])], sections=[MOVIES])
        assert got == {}


def test_a_split_spec_carries_the_overrides():
    spec = _row("r", _skip("2"))
    assert replace(spec, library_keys=["2"], media="movie").request_overrides == _skip("2")


def test_the_overrides_field_is_last_and_defaults_to_nothing_skipped():
    assert RequestOverrides().skip_library_keys == ()
