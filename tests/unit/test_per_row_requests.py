"""A multi-row run must behave like one sub-run per row: each row gates on its OWN floors, spends its
own share of the rating budget, files into its own Sonarr/Radarr target, and competes fairly for the
run's slots.

Before this, one flat demand-ranked list decided everything: whichever row held the highest-demand
titles took every slot and every rating lookup, every night, since that ranking barely moves.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from shortlist.engine import requests as requests_mod
from shortlist.engine.models import (
    MediaType,
    MissingTitle,
    RequestConfig,
    RequestOverrides,
)
from shortlist.engine.request_config import resolve_request_config
from shortlist.engine.requests import RowRequest

from .test_requests import RADARR, SONARR, FakeArr, FakeMdbList, FakeTmdb


def _cfg(**kw) -> RequestConfig:
    defaults = dict(
        enabled=True,
        radarr=RADARR,
        sonarr=SONARR,
        min_rating=7.0,
        min_votes=100,
        min_demand=1,
        max_per_run=10,
        auto_send=True,
        auto_min_demand=1,
        auto_min_rating=0.0,
    )
    defaults.update(kw)
    return RequestConfig(**defaults)


def _title(tmdb_id: int, *, rating: float = 8.0, demand: int = 1, media=MediaType.MOVIE) -> MissingTitle:
    return MissingTitle(tmdb_id, f"t{tmdb_id}", media, 2021, rating, 500, demand=demand)


def _demand(*titles: MissingTitle):
    return {(t.tmdb_id, t.media_type): t for t in titles}


def _rows(*pairs: tuple[str, RequestConfig, dict]) -> list[RowRequest]:
    return [RowRequest(slug, cfg, demand) for slug, cfg, demand in pairs]


@pytest.fixture
def radarr(monkeypatch):
    """One FakeArr per distinct target, so a test can see WHICH target a title was filed into."""
    made: dict[tuple, FakeArr] = {}

    def factory(target, **kw):
        made.setdefault((target.root_folder, target.quality_profile_id), FakeArr())
        return made[(target.root_folder, target.quality_profile_id)]

    monkeypatch.setattr(requests_mod, "RadarrClient", factory)
    return made


class TestEachRowGatesOnItsOwnFloors:
    def test_a_strict_row_rejects_what_a_lenient_row_accepts(self, radarr):
        base = _cfg()
        strict = replace(base, min_rating=9.0)
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("strict", strict, _demand(_title(1, rating=7.5))), ("loose", base, _demand(_title(2, rating=7.5)))),
            dry_run=False,
        )
        assert report.considered_by_row == {"strict": 0, "loose": 1}
        assert [m.title for m in report.sent] == ["t2"]

    def test_min_demand_is_counted_within_the_row_not_across_rows(self, radarr):
        """One person wanting a title in two rows is demand 1 in each. A row asking for 2 people must
        not be satisfied by one person appearing in a second row."""
        base = _cfg(min_demand=2)
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("a", base, _demand(_title(1, demand=1))), ("b", base, _demand(_title(1, demand=1)))),
            dry_run=False,
        )
        assert report.sent == []

    def test_a_rows_year_window_applies_only_to_it(self, radarr):
        base = _cfg()
        modern = replace(base, min_year=2020)
        old = MissingTitle(1, "old", MediaType.MOVIE, 1999, 8.0, 500, demand=1)
        other = MissingTitle(2, "old2", MediaType.MOVIE, 1999, 8.0, 500, demand=1)
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("modern", modern, _demand(old)), ("any", base, _demand(other))), dry_run=False
        )
        assert [m.title for m in report.sent] == ["old2"]

    def test_auto_send_off_on_one_row_does_not_stop_another(self, radarr):
        base = _cfg()
        manual = replace(base, auto_send=False)
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("manual", manual, _demand(_title(1))), ("auto", base, _demand(_title(2)))),
            dry_run=False,
        )
        assert [m.title for m in report.sent] == ["t2"]
        assert [(m.title, m.detail) for m in report.queued] == [("t1", "auto-send is off")]


class TestEachRowFilesIntoItsOwnTarget:
    def test_a_title_goes_to_the_claiming_rows_root_folder_and_profile(self, radarr):
        base = _cfg()
        kids = resolve_request_config(base, RequestOverrides(radarr_root_folder="/kids", radarr_quality_profile_id=9))
        requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("kids", kids, _demand(_title(1))), ("main", base, _demand(_title(2)))),
            dry_run=False,
        )
        assert [t for t, _ in radarr[("/kids", 9)].movie_calls] == [1]
        assert [t for t, _ in radarr[("/movies", 1)].movie_calls] == [2]

    def test_rows_sharing_a_target_share_one_client(self, radarr):
        """One client per distinct target: the plex-safety write throttle lives on the client, so one
        per row would multiply the write rate by the number of rows."""
        base = _cfg()
        requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, _demand(_title(1))), ("b", base, _demand(_title(2)))), dry_run=False
        )
        assert len(radarr) == 1
        assert sorted(t for t, _ in radarr[("/movies", 1)].movie_calls) == [1, 2]


class TestTheRunCeilingIsSharedFairly:
    def test_one_row_cannot_take_every_slot(self, radarr):
        """The starvation this feature exists to fix: row A holds the highest-demand titles, so under
        a single flat ranking it took the whole cap every night and row B never sent anything."""
        base = _cfg(max_per_run=4)
        hot = _demand(*[_title(i, demand=50) for i in range(1, 10)])
        cold = _demand(*[_title(i, demand=1) for i in range(100, 110)])
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("hot", base, hot), ("cold", base, cold)), dry_run=False
        )
        assert report.sent_by_row == {"hot": 2, "cold": 2}

    def test_a_rows_own_max_binds_below_the_global(self, radarr):
        """The owner's case: global 10, one row capped at 3 -> 3."""
        base = _cfg(max_per_run=10)
        capped = resolve_request_config(base, RequestOverrides(max_per_row=3))
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("only", capped, _demand(*[_title(i) for i in range(1, 20)]))), dry_run=False
        )
        assert len(report.sent) == 3

    def test_a_row_cannot_raise_the_global_ceiling(self, radarr):
        base = _cfg(max_per_run=2)
        greedy = resolve_request_config(base, RequestOverrides(max_per_row=99))
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("greedy", greedy, _demand(*[_title(i) for i in range(1, 20)]))), dry_run=False
        )
        assert len(report.sent) == 2

    def test_overflow_is_queued_not_lost(self, radarr):
        base = _cfg(max_per_run=1)
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, _demand(_title(1), _title(2)))), dry_run=False
        )
        assert len(report.sent) == 1
        assert [m.detail for m in report.queued] == ["max_per_run (1) already filled"]


class TestCollisionsAcrossRows:
    def test_a_title_both_rows_want_is_sent_once(self, radarr):
        base = _cfg()
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, _demand(_title(1))), ("b", base, _demand(_title(1)))), dry_run=False
        )
        assert [t for t, _ in radarr[("/movies", 1)].movie_calls] == [1]
        assert len(report.sent) == 1

    def test_it_is_filed_under_the_earlier_rows_target(self, radarr):
        base = _cfg()
        kids = resolve_request_config(base, RequestOverrides(radarr_root_folder="/kids"))
        requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("kids", kids, _demand(_title(1))), ("main", base, _demand(_title(1)))),
            dry_run=False,
        )
        assert [t for t, _ in radarr[("/kids", 1)].movie_calls] == [1]
        # The base target's client exists — `_apply_arr_state` reconciles through it — but nothing
        # was filed into it.
        assert radarr[("/movies", 1)].movie_calls == []

    def test_a_row_that_lost_the_collision_does_not_queue_it_as_overflow(self, radarr):
        """It WAS sent — just under the other row. Queueing it would show the owner a title in the
        inbox that Radarr already has."""
        base = _cfg()
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, _demand(_title(1))), ("b", base, _demand(_title(1)))), dry_run=False
        )
        assert report.queued == []


class TestTheLookupBudgetIsSharedToo:
    """The rating gate is where a row is really starved: with one shared budget, row A's candidates
    consume every lookup and row B reaches allocation with nothing rated to put in its slots."""

    def test_both_rows_get_titles_rated(self, radarr):
        base = _cfg(rating_source="imdb", mdblist_api_key="k", max_per_run=10)
        big = _demand(*[_title(i) for i in range(1, 300)])
        other = _demand(*[_title(i) for i in range(1000, 1300)])
        mdb = FakeMdbList({})
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, big), ("b", base, other)), dry_run=True, mdblist=mdb
        )
        assert report.examined_by_row["a"] > 0
        assert report.examined_by_row["b"] > 0

    def test_an_unspent_share_passes_to_the_next_row(self, radarr):
        """Row A has two titles and cannot use its half, so row B gets the rest — the same
        redistribution the slot allocator does."""
        base = _cfg(rating_source="imdb", mdblist_api_key="k", max_per_run=10)
        mdb = FakeMdbList({})
        requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(
                ("a", base, _demand(_title(1), _title(2))), ("b", base, _demand(*[_title(i) for i in range(10, 400)]))
            ),
            dry_run=True,
            mdblist=mdb,
        )
        assert mdb.live_lookups == requests_mod._lookup_budget(base.max_per_run)

    def test_a_title_in_two_rows_costs_one_live_lookup(self, radarr):
        """The second row's read is a cache hit, so overlap never doubles the quota spend."""
        base = _cfg(rating_source="imdb", mdblist_api_key="k")
        mdb = FakeMdbList({})
        requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("a", base, _demand(_title(1))), ("b", base, _demand(_title(1)))),
            dry_run=True,
            mdblist=mdb,
        )
        assert mdb.live_lookups == 1


class TestReportingPerRow:
    def test_the_report_breaks_every_stage_down_by_row(self, radarr):
        """A run-wide total cannot answer "why did the kids row send nothing" once each row has its
        own floors and its own budget share."""
        base = _cfg(max_per_run=4)
        strict = replace(base, min_rating=9.9)
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("strict", strict, _demand(_title(1))), ("ok", base, _demand(_title(2)))),
            dry_run=False,
        )
        assert report.pool_by_row == {"strict": 1, "ok": 1}
        assert report.considered_by_row == {"strict": 0, "ok": 1}
        assert report.sent_by_row == {"ok": 1}


class TestDegenerateCases:
    def test_no_rows_sends_nothing_and_does_not_raise(self, radarr):
        report = requests_mod.request_missing(_cfg(), FakeTmdb(), [], dry_run=False)
        assert (report.sent, report.queued, report.outcomes) == ([], [], [])

    def test_a_row_with_an_empty_demand_map_is_harmless(self, radarr):
        base = _cfg()
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("empty", base, {}), ("real", base, _demand(_title(1)))), dry_run=False
        )
        assert [m.title for m in report.sent] == ["t1"]

    def test_an_unconfigured_arr_on_one_row_does_not_stop_the_other(self, radarr):
        """A row whose target is missing reports its own skip; the other row still sends."""
        base = _cfg()
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("none", replace(base, radarr=None), _demand(_title(1))), ("ok", base, _demand(_title(2)))),
            dry_run=False,
        )
        assert [m.title for m in report.sent] == ["t2"]
        assert any(o.status == "skipped_no_target" for o in report.outcomes)

    def test_a_show_and_a_movie_with_the_same_id_are_two_titles(self, radarr, monkeypatch):
        sonarr_fake = FakeArr()
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr_fake)
        base = _cfg()
        movie = _title(550)
        show = MissingTitle(550, "show", MediaType.SHOW, 2021, 8.0, 500, demand=1)
        report = requests_mod.request_missing(
            base, FakeTmdb(tvdb={550: 900}), _rows(("a", base, _demand(movie, show))), dry_run=False
        )
        assert len(report.sent) == 2


class TestTheRateLimitStopsTheWholeRun:
    """Audit round 2, 2026-08-18: once MDBList's daily quota is spent, asking again can only produce
    another 429 — but every remaining row fired its own doomed request and logged its own "daily
    limit reached", so one event became N wasted calls and N warnings against an API already
    refusing us."""

    @staticmethod
    def _demand_of(n: int, start: int):
        return {(t.tmdb_id, t.media_type): t for t in (_title(start + i) for i in range(n))}

    def test_later_rows_do_not_re_ask_a_spent_quota(self, radarr):
        base = _cfg(rating_source="imdb", mdblist_api_key="k", max_per_run=10)
        mdb = FakeMdbList({}, rate_limit_after=2)
        rows = _rows(*[(f"r{i}", base, self._demand_of(20, i * 100)) for i in range(4)])

        report = requests_mod.request_missing(base, FakeTmdb(), rows, dry_run=True, mdblist=mdb)

        # 2 successful + the one that discovers the 429. Four rows must not mean four discoveries.
        assert mdb.live_lookups == 3
        assert report.ratings_rate_limited is True

    def test_the_run_still_completes_on_tmdb_ratings(self, radarr):
        """Falling back is the point — a spent quota must degrade the run, never end it."""
        base = _cfg(rating_source="imdb", mdblist_api_key="k", max_per_run=10)
        mdb = FakeMdbList({}, rate_limit_after=2)
        rows = _rows(*[(f"r{i}", base, self._demand_of(20, i * 100)) for i in range(4)])

        report = requests_mod.request_missing(base, FakeTmdb(), rows, dry_run=True, mdblist=mdb)

        assert len(report.sent) == 10, "the run still fills its cap from TMDB scores"
        assert all(report.examined_by_row[f"r{i}"] > 0 for i in range(1, 4)), "no row is left unrated"


class TestTwoTitlesThatLookAlike:
    """Audit round 5: `MissingTitle` is a plain dataclass, so `==` compares field-for-field. Any code
    that identifies a title by value rather than by identity will collapse two distinct rows' copies
    of the same title — or two genuinely different titles that happen to match."""

    def test_identical_titles_in_one_row_are_both_accounted_for(self, radarr):
        base = _cfg(max_per_run=1)  # one sends, the other must be QUEUED rather than vanish
        twin_a = _title(1)
        twin_b = _title(2)
        twin_b.title = twin_a.title  # same name, same rating, same year — different tmdb_id

        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, _demand(twin_a, twin_b))), dry_run=False
        )

        assert len(report.sent) == 1
        assert len(report.queued) == 1, "the twin must be queued, not swallowed by a value comparison"
        assert {m.tmdb_id for m in report.sent} | {m.tmdb_id for m in report.queued} == {1, 2}
