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
    ArrTarget,
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


class TestOneInboxRowPerTitle:
    """Audit round 7, 2026-08-18: per-row demand means a title several rows want exists as several
    MissingTitle objects. Both reached `report.queued`, and `request_candidates` is UNIQUE on
    (tmdb_id, media_type) — so persisting that run died with an IntegrityError and lost the WHOLE
    inbox write, not just the duplicate."""

    def _manual(self, **kw):
        return _cfg(auto_send=False, **kw)

    def test_a_title_two_rows_queued_yields_one_row(self, radarr):
        base = self._manual()
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("a", base, _demand(_title(550))), ("b", base, _demand(_title(550)))),
            dry_run=True,
        )
        keys = [(m.tmdb_id, m.media_type) for m in report.queued]
        assert len(keys) == len(set(keys)) == 1

    def test_the_surviving_row_keeps_every_rows_evidence(self, radarr):
        """The inbox exists to say WHO wanted a title and from WHICH row — dropping a copy silently
        would drop half that answer."""
        base = self._manual()
        a, b = _title(550), _title(550)
        a.wanters, b.wanters = {"sarah"}, {"mike"}
        a.tags, b.tags = {"kids"}, {"prestige"}
        b.demand = 4

        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, _demand(a)), ("b", base, _demand(b))), dry_run=True
        )

        kept = report.queued[0]
        assert kept.wanters == {"sarah", "mike"}
        assert kept.tags == {"kids", "prestige"}
        # Max, never the sum: a person in two rows is one person, and summing invents a second.
        assert kept.demand == 4

    def test_a_title_sent_under_one_row_is_not_queued_by_another(self, radarr):
        """It went out; an inbox row would name a title Radarr already has."""
        auto = _cfg(max_per_run=5)
        manual = _cfg(auto_send=False)
        report = requests_mod.request_missing(
            auto,
            FakeTmdb(),
            _rows(("sender", auto, _demand(_title(550))), ("waiter", manual, _demand(_title(550)))),
            dry_run=False,
        )
        assert [m.tmdb_id for m in report.sent] == [550]
        assert report.queued == []


class TestTagsSurviveTheRowSplit:
    """Audit round 9, 2026-08-18: the guide promises a requested title carries "the tags of every
    per-person row they're in". Per-row demand splits one title into one object per row and only the
    CLAIMING row's object is sent — so the union silently became "whichever row got there first",
    breaking the one thing the tag is for: hanging Radarr rules on which rows asked."""

    def test_a_sent_title_carries_every_wanting_rows_tags(self, radarr):
        base = _cfg()
        kids, prestige = _title(550), _title(550)
        kids.tags, prestige.tags = {"kids", "sarah"}, {"prestige", "mike"}

        requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("kids", base, _demand(kids)), ("prestige", base, _demand(prestige))),
            dry_run=False,
        )

        assert radarr[("/movies", 1)].tag_calls == [{"kids", "sarah", "prestige", "mike"}]

    def test_a_sent_title_names_everyone_who_wanted_it(self, radarr):
        base = _cfg()
        a, b = _title(550), _title(550)
        a.wanters, b.wanters = {"sarah"}, {"mike"}

        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("a", base, _demand(a)), ("b", base, _demand(b))), dry_run=False
        )

        assert report.sent[0].wanters == {"sarah", "mike"}

    def test_demand_is_deliberately_not_merged(self, radarr):
        """It is the floor `min_demand` is checked against, so merging would let one row's popularity
        satisfy another row's threshold — the cross-row leak this whole feature removed."""
        strict = _cfg(min_demand=3)
        loose = _cfg(min_demand=1)
        lonely, popular = _title(550, demand=1), _title(550, demand=9)

        report = requests_mod.request_missing(
            strict,
            FakeTmdb(),
            _rows(("strict", strict, _demand(lonely)), ("loose", loose, _demand(popular))),
            dry_run=False,
        )

        assert report.pool_by_row == {"strict": 0, "loose": 1}, "the strict row must not borrow demand"


class TestTaggingEndToEnd:
    """All three tag layers must still reach Radarr once demand is split per row.

    The guide promises a requested title carries the global tag, the tag of every PERSON who wanted
    it, and the tag of every ROW they're in. Per-row demand made the last two accumulate separately,
    which is where a layer could silently go missing.
    """

    def test_all_three_layers_reach_the_arr(self, radarr):
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=1, root_folder="/movies", tag="shortlist"
        )
        base = _cfg(radarr=tagged)
        title = _title(550)
        title.tags = {"sarah", "picked-for-family"}  # person tag + row tag, as _record_demand builds them

        requests_mod.request_missing(base, FakeTmdb(), _rows(("picked", base, _demand(title))), dry_run=False)

        # The engine passes the per-user/per-row tags as extra_tags; the client unions the target's
        # own global tag onto them (`arr.py::_tag_ids`), so the engine must not drop either layer.
        assert radarr[("/movies", 1)].tag_calls == [{"sarah", "picked-for-family"}]

    def test_a_rows_own_target_keeps_the_global_tag(self, radarr):
        """A per-row folder override must not strip the global tag off the target it copies."""
        tagged = ArrTarget(
            url="http://radarr.test", api_key="rk", quality_profile_id=1, root_folder="/movies", tag="shortlist"
        )
        kids = resolve_request_config(_cfg(radarr=tagged), RequestOverrides(radarr_root_folder="/kids"))
        assert kids.radarr.tag == "shortlist"

    def test_a_title_two_rows_want_carries_both_rows_tags(self, radarr):
        base = _cfg()
        kids, prestige = _title(550), _title(550)
        kids.tags, prestige.tags = {"kids"}, {"prestige"}

        requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("kids", base, _demand(kids)), ("prestige", base, _demand(prestige))),
            dry_run=False,
        )

        assert radarr[("/movies", 1)].tag_calls == [{"kids", "prestige"}]

    def test_a_queued_title_keeps_its_tags_for_the_later_approval(self, radarr):
        """The inbox row is what an approval months later is built from, so the tags have to survive
        being queued, not just being sent."""
        base = _cfg(auto_send=False)
        title = _title(550)
        title.tags = {"sarah", "kids"}

        report = requests_mod.request_missing(base, FakeTmdb(), _rows(("kids", base, _demand(title))), dry_run=False)

        assert report.queued[0].tags == {"sarah", "kids"}

    def test_shows_route_to_sonarr_with_their_tags(self, radarr, monkeypatch):
        sonarr_fake = FakeArr()
        monkeypatch.setattr(requests_mod, "SonarrClient", lambda *a, **k: sonarr_fake)
        base = _cfg()
        show = MissingTitle(70, "show", MediaType.SHOW, 2021, 8.0, 500, demand=1)
        show.tags = {"sarah", "tv-row"}

        requests_mod.request_missing(base, FakeTmdb(tvdb={70: 900}), _rows(("tv", base, _demand(show))), dry_run=False)

        assert sonarr_fake.tag_calls == [{"sarah", "tv-row"}]


class TestARowSetToNeverAskDoesNotAsk:
    """Architecture review HIGH, 2026-08-18, end to end. `max_per_row` used 0 as its "inherit"
    sentinel, so a row the editor describes as "never asks for anything on its own" was handed the
    FULL run cap and could auto-send that many titles into Radarr. The unit fix is in
    `resolve_request_config`; this asserts it survives the whole path the owner's setting travels."""

    def test_zero_sends_nothing_while_the_other_row_still_works(self, radarr):
        base = _cfg(max_per_run=6)
        never = resolve_request_config(base, RequestOverrides(max_per_row=0))
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(
                ("never", never, _demand(*[_title(i) for i in range(1, 9)])),
                ("normal", base, _demand(*[_title(i) for i in range(100, 109)])),
            ),
            dry_run=False,
        )
        assert report.sent_by_row.get("never", 0) == 0, "a row set to 0 must send nothing"
        assert report.sent_by_row["normal"] == 6, "and must hand its whole share to the other row"

    def test_its_titles_still_reach_the_inbox_for_approval(self, radarr):
        """The caption promises exactly this: "its picks still wait in Requests for you to approve"."""
        base = _cfg(max_per_run=6)
        never = resolve_request_config(base, RequestOverrides(max_per_row=0))
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("never", never, _demand(_title(1), _title(2)))), dry_run=False
        )
        assert report.sent == []
        assert {m.tmdb_id for m in report.queued} == {1, 2}

    def test_the_claiming_row_is_stamped_on_every_sent_title(self, radarr):
        """`row_slug` is what an approval months later uses to pick the right Arr target, and it can
        no longer be derived from `why` now that provenance is merged across rows."""
        base = _cfg(max_per_run=4)
        report = requests_mod.request_missing(
            base,
            FakeTmdb(),
            _rows(("a", base, _demand(_title(1))), ("b", base, _demand(_title(2)))),
            dry_run=False,
        )
        assert {m.row_slug for m in report.sent} == {"a", "b"}


class TestTheClaimingRowSurvivesTheRequeuePath:
    """Second architecture review HIGH, 2026-08-18. The claiming slug was stamped only on the CLAIMED
    object — but when an earlier row also held the title back, that row's copy is already in
    `report.queued`, and `_dedupe_queued` keeps the earliest. So the surviving inbox row had no slug
    and fell back to the earliest `why` entry: the mis-file the stamp exists to prevent.

    Scenario: a kids row (earlier, auto-send off, /kids) and a main row (later, auto-send on,
    /movies) both want a title. Main claims it, Radarr errors, the title is queued — and a later
    approval must send it to /movies, not /kids.
    """

    def _rows_for(self, radarr_fail: bool):
        base = _cfg(max_per_run=5)
        kids = replace(base, auto_send=False)
        return base, _rows(("kids", kids, _demand(_title(1))), ("main", base, _demand(_title(1))))

    def test_the_queued_row_carries_the_row_that_actually_claimed_it(self, monkeypatch):
        fake = FakeArr(raise_on=1)  # Radarr rejects it, so the claim is requeued
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        base, rows = self._rows_for(True)

        report = requests_mod.request_missing(base, FakeTmdb(), rows, dry_run=False)

        assert len(report.queued) == 1
        assert report.queued[0].row_slug == "main", "the inbox row must name the row that sent it"

    def test_the_queued_row_carries_the_real_failure_not_a_threshold_note(self, monkeypatch):
        """The earlier row's copy said "auto-send is off". That is not why it is not here — Radarr
        rejected it — and losing that re-opens "a failed auto-send used to vanish"."""
        fake = FakeArr(raise_on=1)
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: fake)
        base, rows = self._rows_for(True)

        report = requests_mod.request_missing(base, FakeTmdb(), rows, dry_run=False)

        assert "boom" in report.queued[0].detail
        assert "auto-send is off" not in report.queued[0].detail

    def test_a_row_held_by_its_own_cap_is_told_so(self, radarr):
        """And not "max_per_run already filled", which points at a global setting the owner can
        raise forever without effect."""
        base = _cfg(max_per_run=5)
        capped = resolve_request_config(base, RequestOverrides(max_per_row=1))
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("capped", capped, _demand(_title(1), _title(2)))), dry_run=False
        )
        assert len(report.sent) == 1
        assert "this row's own limit (1)" in report.queued[0].detail

    def test_a_row_merely_inheriting_the_run_cap_still_blames_the_run(self, radarr):
        """The mirror: a row that never set a limit must not be told its own limit bound."""
        base = _cfg(max_per_run=1)
        report = requests_mod.request_missing(
            base, FakeTmdb(), _rows(("plain", base, _demand(_title(1), _title(2)))), dry_run=False
        )
        assert "max_per_run (1) already filled" in report.queued[0].detail
