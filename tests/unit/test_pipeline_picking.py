"""Which titles a row picks: rating source, recency, seeds, variety, traces and row limits."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import ClassVar
from unittest.mock import MagicMock

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.models import (
    MediaType,
    Pick,
    RowSpec,
)
from tests.conftest import fake_media_item, make_profile, make_watched, plextv_user
from tests.unit.pipeline_support import (
    _ranked,
    ctx,  # noqa: F401
)


class TestRatingSource:
    """Ordering by "Highest rated" when the owner picked a non-TMDB service (IMDb, Trakt, …).

    The score comes from MDBList, which the request gate already uses. Only rows actually ordered by
    rating pay for it, and only for the picks that survived into the row.
    """

    def _rating_ctx(self, ctx, source: str, mdblist):
        from tests.unit.test_pipeline_row_overrides_refresh import TestRowPickOrder as T

        T()._ordered_row_ctx(ctx, "rating")
        ctx.config.rating_source = source
        ctx.mdblist = mdblist

    def _ids(self, report):
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        return [p["tmdb_id"] for p in picks]

    def test_a_rating_row_sorts_on_the_configured_service_not_tmdb(self, ctx: EngineContext, mock_plextv):
        """The whole point: TMDB rates 10 highest and 19 lowest in this fixture, so an IMDb order that
        reverses them cannot be TMDB's numbers by coincidence."""
        from tests.unit.test_requests import FakeMdbList

        mdblist = FakeMdbList({tid: (float(tid), 5000) for tid in range(10, 20)})  # IMDb: 19 best, 10 worst
        self._rating_ctx(ctx, "imdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == sorted(ids, reverse=True), f"sorted by the IMDb score, got {ids}"
        assert mdblist.calls == len(ids), f"one lookup per delivered pick, not per candidate, got {mdblist.calls}"

    def test_the_default_source_costs_no_lookups_at_all(self, ctx: EngineContext, mock_plextv):
        """TMDB is already on every candidate, so the default must not touch MDBList — otherwise every
        rating-ordered row on the server would spend quota for a number it already had."""
        from tests.unit.test_requests import FakeMdbList

        mdblist = FakeMdbList({})
        self._rating_ctx(ctx, "tmdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert mdblist.calls == 0, "the TMDB default is a no-op"
        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"still ordered by TMDB score, got {ids}"

    def test_a_spent_quota_falls_the_whole_row_back_to_tmdb(self, ctx: EngineContext, mock_plextv):
        """A row must never be sorted on two services' scales at once. Falling back only for the
        titles AFTER the 429 would interleave IMDb scores with TMDB ones, which is worse than either.
        """
        from tests.unit.test_requests import FakeMdbList

        # Reversed vs TMDB, so a partial application would be obvious in the delivered order.
        mdblist = FakeMdbList({tid: (float(tid), 5000) for tid in range(10, 20)}, rate_limit_after=2)
        self._rating_ctx(ctx, "imdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"whole row on TMDB scores, got {ids}"

    def test_a_title_the_service_cannot_score_sorts_last(self, ctx: EngineContext, mock_plextv):
        """An unrated title is not an error — IMDb simply has no score for some titles. It goes to the
        end of the row rather than dropping out of it or failing the run."""
        from tests.unit.test_requests import FakeMdbList

        ratings: dict[int, tuple[float, int] | None] = {tid: (float(tid), 5000) for tid in range(10, 20)}
        # 12 is one of the five titles that actually reach this row (selection takes the top 5 by
        # ranking), and would otherwise sit mid-row on the IMDb scale — so "sorts last" is a real move.
        ratings[12] = None
        mdblist = FakeMdbList(ratings)
        self._rating_ctx(ctx, "imdb", mdblist)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids[-1] == 12, f"the unscored title sorts last, got {ids}"
        assert sorted(ids) == [10, 11, 12, 13, 14], f"and is still delivered, not dropped, got {ids}"

    def test_no_mdblist_key_configured_leaves_the_row_on_tmdb(self, ctx: EngineContext, mock_plextv):
        """`ctx.mdblist` is None when no key is set. Choosing IMDb without one must degrade to TMDB,
        which is what the setting documents — not raise on every rating-ordered row."""
        self._rating_ctx(ctx, "imdb", None)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"fell back to TMDB, got {ids}"

    def test_the_service_score_never_overwrites_the_persisted_tmdb_rating(self):
        """`Pick.rating` is TMDB's, is persisted as such, and comes back on every carried-forward pick.

        The service score is returned as a separate override map instead of being written onto the
        pick. Writing it made a fallback impossible to honour: a refresh night mixes carried picks
        (holding last run's MDBList score) with newcomers (holding TMDB's), and once both sit in the
        same field nothing can tell them apart — so a quota-spent night sorted one row on two
        services' scales, which is worse than sorting it on either.
        """
        from shortlist.engine.rows import _apply_order, _rated_by_source
        from tests.unit.test_requests import FakeMdbList

        picks = [
            Pick(tmdb_id=1, rating_key=1, title="A", rank=1, reason="", media_type=MediaType.MOVIE, rating=9.0),
            Pick(tmdb_id=2, rating_key=2, title="B", rank=2, reason="", media_type=MediaType.MOVIE, rating=1.0),
        ]
        ctx = MagicMock()
        ctx.config.rating_source = "imdb"
        ctx.mdblist_rate_limited = False
        ctx.mdblist = FakeMdbList({1: (2.0, 500), 2: (8.0, 500)})  # IMDb reverses TMDB's order

        overrides = _rated_by_source(picks, ctx)
        ordered = _apply_order(picks, "rating", row_slug="r", user_slug="u", run_day=5, ratings=overrides)

        assert [p.rating for p in picks] == [9.0, 1.0], "the picks themselves still carry TMDB's score"
        assert [p.tmdb_id for p in ordered] == [2, 1], "but the row is ordered on the IMDb score"
        # And with no map (the quota-spent / no-key fallback) the SAME picks order on TMDB alone.
        fallback = _apply_order(picks, "rating", row_slug="r", user_slug="u", run_day=5, ratings=None)
        assert [p.tmdb_id for p in fallback] == [1, 2], "the fallback is one consistent TMDB scale"

    def _picks(self, report):
        return next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]

    def test_the_breakdown_carries_the_score_the_row_was_sorted_on(self, ctx: EngineContext, mock_plextv):
        """The run page renders the breakdown. A row sorted on IMDb but showing TMDB scores looks
        unordered, so each pick carries the sorted-on score and its source beside the TMDB one."""
        from tests.unit.test_requests import FakeMdbList

        ratings: dict[int, tuple[float, int] | None] = {tid: (float(tid) / 2, 5000) for tid in range(10, 20)}
        ratings[12] = None
        self._rating_ctx(ctx, "imdb", FakeMdbList(ratings))
        mock_plextv.users = [plextv_user(100, "sarah")]

        picks = self._picks(pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]))

        for p in picks:
            assert p["order_rating_source"] == "imdb"
            assert p["order_rating"] == (0.0 if p["tmdb_id"] == 12 else p["tmdb_id"] / 2)
            assert p["rating_source"] == "tmdb"
            assert p["rating"] == 9.5 - (p["tmdb_id"] - 10) * 0.5, "rating stays TMDB's"

    def test_a_tmdb_sorted_row_has_no_order_score_but_names_its_rating_source(self, ctx: EngineContext, mock_plextv):
        from tests.unit.test_requests import FakeMdbList

        self._rating_ctx(ctx, "tmdb", FakeMdbList({}))
        mock_plextv.users = [plextv_user(100, "sarah")]

        picks = self._picks(pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]))

        assert picks
        for p in picks:
            assert p["order_rating"] is None
            assert p["order_rating_source"] is None
            assert p["rating_source"] == "tmdb"

    def test_a_spent_quota_leaves_no_order_score_on_the_picks(self, ctx: EngineContext, mock_plextv):
        from tests.unit.test_requests import FakeMdbList

        mdblist = FakeMdbList({tid: (float(tid), 5000) for tid in range(10, 20)}, rate_limit_after=2)
        self._rating_ctx(ctx, "imdb", mdblist)
        mock_plextv.users = [plextv_user(100, "sarah")]

        picks = self._picks(pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]))

        assert picks
        for p in picks:
            assert p["order_rating"] is None
            assert p["order_rating_source"] is None
            assert p["rating_source"] == "tmdb"

    def test_a_spent_quota_stops_being_retried_for_the_rest_of_the_run(self, ctx: EngineContext, mock_plextv):
        """Without a latch, every rating-ordered row for every user re-attempts after the first 429 —
        and each attempt is retried three times honouring Retry-After (up to 60s). On a 40-user server
        that is minutes of stall for results that are thrown away."""
        from tests.unit.test_requests import FakeMdbList

        mdblist = FakeMdbList({tid: (float(tid), 5000) for tid in range(10, 20)}, rate_limit_after=0)
        self._rating_ctx(ctx, "imdb", mdblist)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(101, "mike")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=101)])

        assert ctx.mdblist_rate_limited, "the run latched the spent quota"
        assert mdblist.calls == 1, f"one failed call for the whole run, not one per row per user, got {mdblist.calls}"


class TestRecency:
    """The "Recent releases" weight — how a row resolves it, what it must not cost, and that it
    actually reaches the delivered row. The curve itself is covered in test_ranking.py."""

    # 2026-06-15. Fixed so the assertions below state real years instead of drifting with the clock —
    # the engine reads the run's day, never `date.today()`.
    RUN_DAY = date(2026, 6, 15).toordinal()

    def _policy(self, ctx: EngineContext, user) -> object:
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver

        return RowPolicy(
            ctx=ctx,
            user=user,
            cfg=ctx.config,
            specs=[],
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )

    @pytest.mark.parametrize(
        ("stored", "global_value", "expected", "why"),
        [
            (None, 0.0, 0.0, "unset row + off globally = off, the shipped default"),
            (None, 0.6, 0.6, "an unset row inherits the global"),
            (0.9, 0.6, 0.9, "the row's own value beats the global"),
            (0.0, 0.6, 0.0, "an explicit 0 is a CHOICE, not 'unset' — a Hidden Gems row must stay off"),
        ],
    )
    def test_a_row_resolves_its_own_value_before_the_global(
        self, ctx: EngineContext, stored, global_value, expected, why
    ):
        ctx.config = replace(ctx.config, recency=global_value)
        policy = self._policy(ctx, make_profile("sarah", account_id=100))

        assert policy.effective_recency(RowSpec(slug="r", name_template="R", size=5, recency=stored)) == expected, why

    def test_two_rows_differing_only_in_recency_still_share_one_candidate_pool(self, ctx: EngineContext):
        """The cost guarantee that decided WHERE this is applied.

        Recency re-ranks each row's copy of the pool, downstream of the shared gather. Had it gone
        into `pre_rank` instead, `pool_key` would have had to split on it — and every distinct value
        a person's rows use would buy another full TMDB/Trakt/LLM gather, nightly, for a setting that
        changes nothing about which candidates exist.
        """
        ctx.config = replace(ctx.config, recency=0.0)
        policy = self._policy(ctx, make_profile("sarah", account_id=100))
        gems = RowSpec(slug="gems", name_template="Hidden Gems", size=5, recency=0.0)
        new = RowSpec(slug="new", name_template="New & Notable", size=5, recency=1.0)

        assert policy.pool_key(gems) == policy.pool_key(new)

    def _two_candidates_of_different_vintage(self, ctx: EngineContext) -> None:
        """Candidate 10 = older but better rated; candidate 20 = newer. Ranking with no age term
        leads with 10, so any run that leads with 20 did so because of recency and nothing else."""
        ctx.tmdb.suggestions.return_value = [
            (
                {
                    "id": 10,
                    "title": "Nineties Classic",
                    "genre_ids": [],
                    "vote_average": 8.0,
                    "release_date": "1996-03-01",
                },
                1.0,
            ),
            (
                {"id": 20, "title": "Modern Pick", "genre_ids": [], "vote_average": 7.0, "release_date": "2024-03-01"},
                1.0,
            ),
        ]

    def test_without_recency_the_older_better_rated_title_still_leads(self, ctx: EngineContext, mock_plextv):
        """The control arm. This is today's behaviour and the complaint that prompted the feature:
        release date is invisible to ranking, so the 1996 title wins on rating alone."""
        self._two_candidates_of_different_vintage(ctx)
        ctx.run_day = self.RUN_DAY
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=2)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        assert delivered[0] == 10, f"expected the 1996 title to lead with recency off, got {delivered}"

    def test_a_row_at_full_recency_leads_with_the_newer_title(self, ctx: EngineContext, mock_plextv):
        """The feature, end to end: same pool, same ratings, only the setting differs."""
        self._two_candidates_of_different_vintage(ctx)
        ctx.run_day = self.RUN_DAY
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=2, recency=1.0)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        assert delivered[0] == 20, f"expected the 2024 title to lead at full recency, got {delivered}"

    def test_the_older_title_is_demoted_not_dropped(self, ctx: EngineContext, mock_plextv):
        """A weight, not a filter. If recency ever starts excluding titles, a thin library returns
        short rows — and "older titles still reach rows" stops being true."""
        self._two_candidates_of_different_vintage(ctx)
        ctx.run_day = self.RUN_DAY
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=2, recency=1.0)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert {p.tmdb_id for p in report.users[0].picks} == {10, 20}, "both titles must still be delivered"

    def test_the_global_default_reaches_a_row_that_sets_nothing(self, ctx: EngineContext, mock_plextv):
        """Server-wide setting -> row. The per-row test above could pass with the global ignored."""
        self._two_candidates_of_different_vintage(ctx)
        ctx.run_day = self.RUN_DAY
        ctx.config = replace(ctx.config, recency=1.0)
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=2)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        assert delivered[0] == 20, f"the global default never reached the row, got {delivered}"


class TestRecencySweep:
    """The setting across its whole RANGE, on a pool built so nothing else can explain the result.

    Ten candidates, identical rating, identical affinity, one shared seed — only the release year
    differs, spanning 1970..2025. Titles ascend with year ("A 1970" .. "J 2025"), so with the weight
    OFF every score ties and `_sort_key`'s alphabetical tiebreak hands back the five OLDEST. Any run
    that returns newer titles did so because of this setting and nothing else.

    This exists because the e2e fake cannot show it: there, `seed_frequency` (8->1), `affinity`
    (1.0->0.5) and year (1999->2008) are all inversely correlated by construction, so scores span
    12x while a 9-year age gap can only swing 2.2x. A correct weight is invisible there.
    """

    RUN_DAY = date(2026, 6, 15).toordinal()
    YEARS: ClassVar[list[int]] = [1970, 1976, 1982, 1988, 1994, 2000, 2006, 2012, 2018, 2025]

    def _pool(self, ctx: EngineContext) -> None:
        library = {900: 999}
        suggestions = []
        for i, year in enumerate(self.YEARS):
            tmdb_id = 100 + i
            library[tmdb_id] = 2000 + i
            suggestions.append(
                (
                    {
                        "id": tmdb_id,
                        "title": f"{chr(ord('A') + i)} {year}",
                        "genre_ids": [],
                        "vote_average": 7.5,  # identical, so rating can never explain an ordering
                        "release_date": f"{year}-03-01",
                    },
                    1.0,  # identical affinity, likewise
                )
            )
        ctx.tmdb.suggestions.return_value = suggestions
        ctx.plex.build_library_index.return_value = library
        ctx.run_day = self.RUN_DAY

    def _delivered_years(self, ctx: EngineContext, mock_plextv, recency: float | None) -> list[int]:
        self._pool(ctx)
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, recency=recency)]
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        picks = sorted(report.users[0].picks, key=lambda p: p.rank)
        return [p.year for p in picks if p.year]

    def test_with_the_weight_off_the_row_is_the_five_oldest(self, ctx: EngineContext, mock_plextv):
        """The control. Establishes that the pool really is tied on everything but year — if this
        ever stops returning the oldest five, every other case in this class is measuring noise."""
        assert self._delivered_years(ctx, mock_plextv, 0.0) == [1970, 1976, 1982, 1988, 1994]

    def test_at_full_strength_the_row_is_the_five_newest(self, ctx: EngineContext, mock_plextv):
        """The complete inversion of the control — same pool, same run, one setting changed."""
        assert self._delivered_years(ctx, mock_plextv, 1.0) == [2025, 2018, 2012, 2006, 2000]

    @pytest.mark.parametrize("recency", [0.0, 0.25, 0.5, 0.75, 1.0])
    def test_the_row_is_always_full_whatever_the_setting(self, ctx: EngineContext, mock_plextv, recency):
        """A weight must never cost the row titles. If any setting can return a short row, it has
        started behaving like a filter and the "old titles still reach rows" promise is broken."""
        assert len(self._delivered_years(ctx, mock_plextv, recency)) == 5

    def test_turning_the_dial_up_never_makes_a_row_older(self, ctx: EngineContext, mock_plextv):
        """Monotonicity across the whole slider — the property the UI's era strip claims.

        Asserted as non-decreasing rather than strictly increasing: with ten candidates and five
        slots, neighbouring settings can legitimately agree. What must never happen is the mean
        going DOWN as the owner asks for newer titles.
        """
        means = []
        for recency in (0.0, 0.25, 0.5, 0.75, 1.0):
            years = self._delivered_years(ctx, mock_plextv, recency)
            means.append(sum(years) / len(years))
        assert means == sorted(means), f"raising the setting made a row older: {means}"
        assert means[-1] > means[0], f"the full range changed nothing: {means}"

    def test_a_row_that_sets_nothing_follows_the_global_across_the_range(self, ctx: EngineContext, mock_plextv):
        """The inherit path, swept — a row storing None must track the global, not sit at one value."""
        ctx.config = replace(ctx.config, recency=1.0)
        assert self._delivered_years(ctx, mock_plextv, None) == [2025, 2018, 2012, 2006, 2000]

    def test_an_explicit_zero_beats_a_high_global(self, ctx: EngineContext, mock_plextv):
        """A "Hidden Gems" row on a modern-leaning server. If `recency=0.0` were ever read as
        "unset", this row would silently become a new-releases row like every other."""
        ctx.config = replace(ctx.config, recency=1.0)
        assert self._delivered_years(ctx, mock_plextv, 0.0) == [1970, 1976, 1982, 1988, 1994]

    def test_titles_with_no_release_year_are_not_swept_to_the_back(self, ctx: EngineContext, mock_plextv):
        """An undated title ranks on its merits. TMDB serves plenty with no release_date, and a
        `year or 0` fallback would bury every one of them the moment the owner turns this up."""
        self._pool(ctx)
        ctx.config.candidates_pre_rank = 40  # else the 11th candidate loses the cut below, not the weight
        undated = dict(ctx.tmdb.suggestions.return_value[0][0])
        undated.update({"id": 300, "title": "Z Undated", "release_date": ""})
        ctx.tmdb.suggestions.return_value = [*ctx.tmdb.suggestions.return_value, (undated, 1.0)]
        ctx.plex.build_library_index.return_value = {**ctx.plex.build_library_index.return_value, 300: 3000}
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, recency=1.0)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert 300 in {p.tmdb_id for p in report.users[0].picks}, "an undated title was buried by the age weight"

    def test_the_weight_decides_the_pre_rank_CUT_not_just_the_order_within_it(self, ctx: EngineContext, mock_plextv):
        """The weight must reach PAST the `candidates_pre_rank` truncation, not merely reorder it.

        The pool is capped per media type before a row selects from it. If that cut is taken on the
        base score alone, a newer title ranking below the cap can never be rescued however high the
        owner turns this — on a catalog-deep library the pool exceeds the cap routinely, so the
        setting would quietly stop working exactly where it is needed most.

        Cap of 3 against ten candidates makes it unmissable: the base-score cut keeps the three
        alphabetically-first (= oldest, see the class docstring), so a row that returns the three
        NEWEST proves the weight was applied before the truncation rather than after it.
        """
        self._pool(ctx)
        ctx.config.candidates_pre_rank = 3
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=3, recency=1.0)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        years = [p.year for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        assert years == [2025, 2018, 2012], f"the weight never reached past the cut, got {years}"

    def test_the_cut_still_falls_back_to_the_base_score_when_the_weight_is_off(self, ctx: EngineContext, mock_plextv):
        """The other half: at 0 the truncation must be byte-identical to what it always was."""
        self._pool(ctx)
        ctx.config.candidates_pre_rank = 3
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=3, recency=0.0)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        years = [p.year for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        assert years == [1970, 1976, 1982], f"the base-score cut changed, got {years}"

    def _shared_years(self, ctx: EngineContext, mock_plextv, recency: float | None, global_recency: float) -> list[int]:
        self._pool(ctx)
        ctx.config = replace(ctx.config, recency=global_recency)
        ctx.config.rows = [
            RowSpec(
                slug="popular",
                name_template="Popular here",
                size=5,
                shared=True,
                min_watchers=1,  # one fake watcher is enough to clear the aggregate-privacy floor
                recency=recency,
            )
        ]
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])
        picks = sorted(
            (p for u in report.users for p in u.picks if p.collection_slug == "popular"), key=lambda p: p.rank
        )
        seen: list[int] = []
        for pick in picks:  # the same shared row is reported per user; one copy is what we assert on
            if pick.year and pick.year not in seen:
                seen.append(pick.year)
        return seen

    def test_a_SHARED_row_ignores_recency_because_its_ranking_is_the_watch_count(self, ctx: EngineContext, mock_plextv):
        """Recency weights a title's release date inside a SCORED CANDIDATE POOL. A shared row no
        longer has one — it is the server's most-watched titles, ranked by how many people watched
        them (owner decision, 2026-08-13) — so there is nothing for the weight to act on, and it
        joins `watched_pct`/`rewatch`/`cold_start` as a dial with no meaning for a row nobody owns.

        Asserted rather than deleted: the three tests this replaces proved the shared path resolved
        the dial independently, and silently dropping them would leave "does recency still do
        something here?" answered nowhere.
        """
        assert self._shared_years(ctx, mock_plextv, 0.0, 1.0) == self._shared_years(ctx, mock_plextv, 1.0, 0.0), (
            "release-date weighting must not change a row ordered by watch count"
        )

    def test_two_rows_at_different_settings_each_get_their_own_cut(self, ctx: EngineContext, mock_plextv):
        """One person, one shared gather, two rows disagreeing about release date — each must get
        the cut its OWN setting implies. This is the case a single shared truncation cannot serve."""
        self._pool(ctx)
        ctx.config.candidates_pre_rank = 3
        ctx.config.rows = [
            RowSpec(slug="gems", name_template="Hidden Gems", size=3, recency=0.0),
            RowSpec(slug="new", name_template="New and Notable", size=3, recency=1.0),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        by_row: dict[str, list[int]] = {}
        for pick in sorted(report.users[0].picks, key=lambda p: p.rank):
            by_row.setdefault(pick.collection_slug, []).append(pick.year)
        assert by_row["gems"] == [1970, 1976, 1982], by_row
        assert by_row["new"] == [2025, 2018, 2012], by_row


class TestSeedCycling:
    """`RowPolicy`'s side of seed cycling: what forces a nightly cadence, and what may share a
    derivation. The rotation itself is covered in test_history.py."""

    def _policy(self, ctx: EngineContext, user) -> object:
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver

        return RowPolicy(
            ctx=ctx,
            user=user,
            cfg=ctx.config,
            specs=[],
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )

    @pytest.mark.parametrize(
        ("name_template", "seed_window", "stored", "expected", "why"),
        [
            ("Because you watched {top_seed}", 1, 0, 1, "a named row overrides even a frozen value"),
            ("Because you watched {top_seed}", 1, None, 1, "and the inherited global"),
            # The arm the row editor originally failed to mirror: an UNNAMED row that cycles is run
            # nightly too, so a cadence control on it would state a cadence the row never uses.
            ("Tonight's pick", 3, 0, 1, "a cycling row is nightly whether or not it names a seed"),
            ("Tonight's pick", 1, 0, 0, "a row that follows no watch still freezes at 0"),
            ("Tonight's pick", 1, None, 8, "and still inherits the global otherwise"),
        ],
    )
    def test_only_a_row_following_a_watch_is_forced_nightly(
        self, ctx: EngineContext, name_template, seed_window, stored, expected, why
    ):
        ctx.config = replace(ctx.config, refresh_days=8)
        policy = self._policy(ctx, make_profile("sarah", account_id=100))
        spec = RowSpec(slug="r", name_template=name_template, size=5, seed_window=seed_window, refresh_days=stored)

        assert policy.effective_refresh_days(spec) == expected, why

    def test_the_DEFAULT_row_is_forced_nightly_from_the_global_template(self, ctx: EngineContext):
        """The row-identity cell the matrix above cannot reach, and the one that matters most.

        `context_builder` blanks the default row's `name_template` on purpose — its title comes from
        the global `row.name_template`, which is what the wizard and Settings edit. Asking the SPEC
        whether it names a seed therefore answered "no" for the one row every new install starts
        with, and the wizard offers "Because you watched {top_seed}" for exactly that row: it was
        neither forced nightly nor rebuilt when its seed moved, while the editor hid the cadence
        control and promised "every night".
        """
        ctx.config = replace(ctx.config, refresh_days=8, row_name_template="Because you watched {top_seed}")
        policy = self._policy(ctx, make_profile("sarah", account_id=100))
        default_row = RowSpec(slug="picked", name_template="", size=5)

        assert policy.effective_refresh_days(default_row) == 1

    def test_a_per_user_template_that_names_a_seed_also_forces_nightly(self, ctx: EngineContext):
        """Same precedence, middle rung: `resolve_row_template` is row -> user -> global, so a
        per-user override naming a seed has to count as much as the row's own template."""
        ctx.config = replace(ctx.config, refresh_days=8, row_name_template="Picked for You")
        user = make_profile("sarah", account_id=100)
        user.row_name_template = "Because you watched {top_seed}"
        policy = self._policy(ctx, user)

        assert policy.effective_refresh_days(RowSpec(slug="picked", name_template="", size=5)) == 1

    def test_two_cycling_rows_do_not_share_one_derivation(self, ctx: EngineContext):
        """The seed cache keys on (media, libraries, max_seeds) — which two cycling rows can match on
        exactly. Without the row's own offset in the key they share one entry and land on the SAME
        watch, which is the opposite of what turning cycling on asks for."""
        user = make_profile("sarah", account_id=100)
        user.history = [make_watched(f"Movie {i}", days_ago=i, tmdb_id=900 + i * 7) for i in range(5)]
        ctx.run_day = 5
        policy = self._policy(ctx, user)
        # Identical in every keyed dimension except the slug the offset is derived from.
        common = {"name_template": "", "size": 5, "media": "movie", "max_seeds": 1, "seed_window": 3}
        leads = {slug: policy.seeds_for(RowSpec(slug=slug, **common))[0].title for slug in ("alpha", "beta", "gamma")}

        assert len(set(leads.values())) > 1, f"every cycling row picked the same watch: {leads}"

    def test_the_cycle_offset_survives_a_restart(self):
        """A per-process-salted `hash` would re-phase every restart, so a row would re-pick its seed
        and rebuild on every run — the same reason `_is_refresh_night` uses crc32."""
        import zlib

        from shortlist.engine.rows import seed_cycle_offset

        assert seed_cycle_offset("picked", "sarah", 5) == 5 + zlib.crc32(b"picked|sarah")
        # And two people's rows sit at different points in the cycle, so a server does not re-derive
        # every cycling row on the same night.
        assert seed_cycle_offset("picked", "sarah", 5) != seed_cycle_offset("picked", "mike", 5)


class TestRefreshNightVariety:
    """A row built varied must stay varied when it refreshes.

    `pre_rank` output is pure score, and one heavily-watched title's look-alikes dominate it — which
    is precisely why `diversify_by_seed` exists. If the refresh branch merges survivors and
    newcomers and then truncates to `k` by pool order, it re-applies the ordering diversify just
    defeated: the row collapses onto the dominant taste and never recovers, because the collapsed
    row is what carries forward to the next refresh.
    """

    RUN_DAY = date(2026, 6, 15).toordinal()

    def _ctx(self, ctx: EngineContext) -> None:
        """Two watches, so two seeds. The first suggests 20 titles, the second only 4 — the lopsided
        shape a real pool has when someone has watched one show far more than anything else."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {
            900: 999,
            901: 998,
            **{i: 2000 + i for i in range(10, 34)},
        }
        dominant = [{"id": i, "title": f"D{i}", "genre_ids": [], "vote_average": 9.0} for i in range(10, 30)]
        minority = [{"id": i, "title": f"M{i}", "genre_ids": [], "vote_average": 6.0} for i in range(30, 34)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(dominant if tid == 900 else minority)
        ctx.history_source.fetch.return_value = [
            make_watched("Heavy", days_ago=1, rating_key=999),
            make_watched("Light", days_ago=2, rating_key=998),
        ]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=6, media="movie", refresh_days=1)]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        ctx.run_day = self.RUN_DAY

    @staticmethod
    def _seeds_of(picks) -> set:
        return {p["seed_title"] for p in picks if p.get("seed_title")}

    def _movies(self, report):
        return next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]

    def test_a_bootstrap_row_draws_on_both_tastes(self, ctx: EngineContext, mock_plextv):
        """The control: without it, the refresh assertion below could pass on a row that was never
        varied in the first place."""
        self._ctx(ctx)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert len(self._seeds_of(self._movies(report))) >= 2

    def test_a_refresh_night_does_not_collapse_the_row_onto_one_taste(self, ctx: EngineContext, mock_plextv):
        """The regression. Last run's row held both tastes; tonight it refreshes."""
        self._ctx(ctx)
        prior = [
            Pick(
                tmdb_id=t,
                rating_key=2000 + t,
                title=f"T{t}",
                rank=i + 1,
                reason="kept",
                media_type=MediaType.MOVIE,
                collection_slug="picked",
                section_key="1",
                library="Movies",
                seed_tmdb_id=900 if t < 30 else 901,
                seed_title="Heavy" if t < 30 else "Light",
            )
            for i, t in enumerate([10, 11, 12, 30, 31, 32])
        ]
        ctx.previous_picks = {("sarah", "picked", "1"): prior}
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        picks = self._movies(report)
        seeds = self._seeds_of(picks)
        assert len(seeds) >= 2, f"the row collapsed onto {seeds}: {[p['title'] for p in picks]}"

    def test_the_kept_two_thirds_still_survive_a_refresh(self, ctx: EngineContext, mock_plextv):
        """Variety must not be bought by throwing away the stability guarantee — the strongest
        two-thirds of last run's row still carry over."""
        self._ctx(ctx)
        prior = [
            Pick(
                tmdb_id=t,
                rating_key=2000 + t,
                title=f"T{t}",
                rank=i + 1,
                reason="kept",
                media_type=MediaType.MOVIE,
                collection_slug="picked",
                section_key="1",
                library="Movies",
                seed_tmdb_id=900 if t < 30 else 901,
                seed_title="Heavy" if t < 30 else "Light",
            )
            for i, t in enumerate([10, 11, 12, 30, 31, 32])
        ]
        ctx.previous_picks = {("sarah", "picked", "1"): prior}
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        ids = {p["tmdb_id"] for p in self._movies(report)}
        assert {10, 11, 12, 30} <= ids, f"the strongest two-thirds did not survive: {sorted(ids)}"


class TestSharedRowHonoursPickOrder:
    """A shared row's display order must work. `_apply_order` lived only in the per-person path, so
    a shared row set to "Shuffled" or "Highest rated" delivered ranking order regardless — while the
    editor went on offering the control."""

    RUN_DAY = date(2026, 6, 15).toordinal()

    def _ctx(self, ctx: EngineContext, order: str) -> None:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {10: 2010, 20: 2020}
        # A shared row is the server's most-watched titles, so the fixture is the WATCHING. Three
        # people watched "Old" and two watched "New", so the popularity order is unambiguously
        # Old-then-New and any run that leads with "New" did so because of `pick_order` alone.
        ctx.history_source.fetch.return_value = []
        ctx.config.rows = [
            RowSpec(
                slug="popular",
                name_template="Popular",
                size=2,
                media="movie",
                shared=True,
                min_watchers=2,
                pick_order=order,
            )
        ]
        ctx.config.min_history = 1
        ctx.run_day = self.RUN_DAY

    def _delivered(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike"), plextv_user(300, "amy")]
        old = make_watched("Old", days_ago=2, rating_key=2010, tmdb_id=10, year=1990)
        new = make_watched("New", days_ago=1, rating_key=2020, tmdb_id=20, year=2024)
        profiles = [
            make_profile("sarah", account_id=100),
            make_profile("mike", account_id=200),
            make_profile("amy", account_id=300),
        ]
        # Everyone watched "Old"; only two watched "New" — 3 watchers against 2.
        for profile, history in zip(profiles, ([old, new], [old, new], [old]), strict=True):
            profile.history = history
        report = pipeline_mod.run(ctx, profiles)
        # DELIVERED order, i.e. list order — never sorted by rank. Rank is the selection order and
        # `_apply_order` deliberately leaves it alone, so re-sorting on it would undo the very thing
        # under test.
        entries = [e for u in report.users for e in u.breakdown if e["row_slug"] == "popular"]
        assert entries, "the shared row delivered nothing — fixture problem, not the feature"
        seen: list[int] = []
        for pick in entries[0]["picks"]:
            if pick["tmdb_id"] not in seen:
                seen.append(pick["tmdb_id"])
        return seen

    def test_newest_first_actually_reorders_a_shared_row(self, ctx: EngineContext, mock_plextv):
        """10 is the better-ranked title, so ranking order leads with it; "newest" must not."""
        self._ctx(ctx, "newest")
        assert self._delivered(ctx, mock_plextv)[0] == 20

    def test_best_match_order_is_still_the_ranking(self, ctx: EngineContext, mock_plextv):
        """The control — "best" is a no-op, so this proves the reorder above came from the setting."""
        self._ctx(ctx, "best")
        assert self._delivered(ctx, mock_plextv)[0] == 10


class TestColdStartRowsAreFullSizeAndFromTheRightLibrary:
    """A cold-start row is what a NEW user sees first, and it was arriving half empty.

    `_cold_start_picks` split `k` across `sections_by_type()` — one representative library per media
    type — while `_build_section_picks` then took only that library's own share. On any server with
    both a movie and a TV library, every cold row came back at half its configured size. The picks
    also came from the representative library rather than the row's own, so a library-pinned row
    lost everything the pinned library didn't hold, and reported a green run.
    """

    RUN_DAY = date(2026, 6, 15).toordinal()

    def _ctx(self, ctx: EngineContext, *, library_keys=None) -> None:
        movies = MagicMock(type="movie", key="1", title="Movies")
        kids = MagicMock(type="movie", key="2", title="Kids Movies")
        shows = MagicMock(type="show", key="3", title="TV Shows")
        ctx.plex.sections.return_value = [movies, kids, shows]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: shows}
        ctx.delivery_sections = [movies, kids, shows]
        ctx.plex.build_library_index.return_value = {}

        def top_rated(section, n):
            base = {"1": 100, "2": 200, "3": 300}[str(section.key)]
            return [(base + i, MagicMock(ratingKey=9000 + base + i, title=f"L{base}-{i}")) for i in range(n)]

        ctx.plex.top_rated.side_effect = top_rated
        ctx.history_source.fetch.return_value = []  # thin history -> cold start
        ctx.config.min_history = 5
        ctx.config.rows = [
            RowSpec(
                slug="picked",
                name_template="Picked",
                size=10,
                media="both",
                library_keys=library_keys or [],
            )
        ]
        ctx.run_day = self.RUN_DAY

    def _by_library(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        # From `picks`, not `breakdown`: the breakdown is assembled by delivery, which needs far more
        # of the PMS mocked than this fixture provides. `picks` is what the engine actually chose.
        out: dict[str, list[int]] = {}
        for pick in report.users[0].picks:
            out.setdefault(pick.library, []).append(pick.tmdb_id)
        return out

    def test_every_library_gets_a_full_row(self, ctx: EngineContext, mock_plextv):
        """Not k split across media types — k per library, like a warm row."""
        self._ctx(ctx)

        by_library = self._by_library(ctx, mock_plextv)

        assert by_library, "the cold-start row delivered nothing at all"
        for title, ids in by_library.items():
            assert len(ids) == 10, f"{title} got {len(ids)} of 10 cold-start picks"

    def test_a_pinned_row_is_filled_from_the_library_it_is_pinned_to(self, ctx: EngineContext, mock_plextv):
        """Library 2's titles are the 200s. A row pinned there must not be filled from library 1."""
        self._ctx(ctx, library_keys=["2"])

        by_library = self._by_library(ctx, mock_plextv)

        assert set(by_library) == {"Kids Movies"}, f"delivered to the wrong libraries: {list(by_library)}"
        ids = by_library["Kids Movies"]
        assert all(200 <= i < 300 for i in ids), f"cold picks came from another library: {ids}"


class TestUnstartedOnlyIsRecheckedOnCarryForward:
    """An "only series they haven't started" row must drop a series the person has since begun, even
    on a night it isn't rebuilt — and at ANY watched cap.

    `_reusable_prior` only applied the started-shows filter when `pct <= 0`, but the row editor
    recommends this toggle alongside `pct > 0` ("this only changes anything if you've allowed
    already-watched titles above"). In its documented configuration, no filter ran at all.
    """

    RUN_DAY = date(2026, 6, 15).toordinal()
    KEY = ("sarah", "unstarted", "1")

    def _ctx(self, ctx: EngineContext, *, pct: float) -> None:
        shows = MagicMock(type="show", key="1", title="TV Shows")
        ctx.plex.sections.return_value = [shows]
        ctx.plex.sections_by_type.return_value = {MediaType.SHOW: shows}
        ctx.plex.build_library_index.return_value = {900: 999, 50: 2050, 51: 2051}
        ctx.tmdb.suggestions.return_value = [
            ({"id": 50, "name": "Started", "genre_ids": [], "vote_average": 9.0}, 1.0),
            ({"id": 51, "name": "Untouched", "genre_ids": [], "vote_average": 8.0}, 0.9),
        ]
        # `watched_shows` is filled from HISTORY (`load_watched_breakdown`), not from a plex call:
        # show 50 has been STARTED since the last run — 1 episode of 40.
        ctx.history_source.fetch.return_value = [
            make_watched("Seed", days_ago=1, rating_key=999, media_type=MediaType.SHOW),
            make_watched(
                "Started",
                days_ago=1,
                tmdb_id=50,
                media_type=MediaType.SHOW,
                viewed_leaf_count=1,
                leaf_count=40,
            ),
        ]
        ctx.config.rows = [
            RowSpec(
                slug="unstarted",
                name_template="Start something",
                size=2,
                media="show",
                unstarted_only=True,
                watched_pct=pct,
                refresh_days=0,
            )
        ]
        ctx.config.min_history = 1
        ctx.run_day = self.RUN_DAY

    def _prior(self):
        return [
            Pick(
                tmdb_id=t,
                rating_key=2000 + t,
                title=f"S{t}",
                rank=i + 1,
                reason="kept",
                media_type=MediaType.SHOW,
                collection_slug="unstarted",
                section_key="1",
                library="TV Shows",
            )
            for i, t in enumerate([50, 51])
        ]

    def _delivered(self, ctx, mock_plextv):
        ctx.previous_picks = {self.KEY: self._prior()}
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        return {p.tmdb_id for p in report.users[0].picks}

    def test_a_started_series_is_dropped_at_a_zero_cap(self, ctx: EngineContext, mock_plextv):
        """Already worked — the control that proves the fixture models 'started' correctly."""
        self._ctx(ctx, pct=0.0)
        assert 50 not in self._delivered(ctx, mock_plextv)

    def test_a_started_series_is_dropped_above_zero_too(self, ctx: EngineContext, mock_plextv):
        """The bug: the configuration the editor recommends for this toggle."""
        self._ctx(ctx, pct=0.5)
        assert 50 not in self._delivered(ctx, mock_plextv), "a started series survived carry-forward"


class TestTheTraceExplainsWhatHappenedToTheRow:
    """The run page could say what a row HOLDS but never why it holds it.

    Most nights a row is redelivered untouched, and the report looked identical to a rebuild — so
    "I changed a setting and nothing moved" was unanswerable without querying the database. It came
    up three times in one afternoon on a real server.
    """

    RUN_DAY = date(2026, 6, 15).toordinal()
    KEY = ("sarah", "picked", "1")

    def _ctx(self, ctx: EngineContext, *, refresh_days: int) -> None:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 2000 + i for i in range(10, 16)}}
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in range(10, 16)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=3, media="movie", refresh_days=refresh_days, recency=0.75)
        ]
        ctx.config.min_history = 1
        ctx.run_day = self.RUN_DAY

    def _selection(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        entries = report.users[0].trace.get("selection") or []
        assert entries, "the trace recorded no selection at all"
        return entries[0]

    def _prior(self):
        return [
            Pick(
                tmdb_id=t,
                rating_key=2000 + t,
                title=f"T{t}",
                rank=i + 1,
                reason="kept",
                media_type=MediaType.MOVIE,
                collection_slug="picked",
                section_key="1",
                library="Movies",
                recipe="same",
            )
            for i, t in enumerate([10, 11, 12])
        ]

    def test_a_first_build_is_recorded_as_rebuilt(self, ctx: EngineContext, mock_plextv):
        self._ctx(ctx, refresh_days=1)
        assert self._selection(ctx, mock_plextv)["decision"] == "rebuilt"

    def test_a_row_left_alone_says_so(self, ctx: EngineContext, mock_plextv):
        """The line that would have saved three rounds of confusion: this row was NOT re-picked."""
        self._ctx(ctx, refresh_days=0)
        ctx.previous_picks = {self.KEY: self._prior()}
        ctx.previous_recipes = {}  # unknown recipe = "nothing to compare", so no forced rebuild

        entry = self._selection(ctx, mock_plextv)

        assert entry["decision"] == "carried_forward"
        assert entry["refresh_night"] is False

    def test_a_settings_change_is_named_as_the_reason(self, ctx: EngineContext, mock_plextv):
        """Distinct from a plain rebuild — this is the row the owner's edit actually moved."""
        self._ctx(ctx, refresh_days=0)
        ctx.previous_picks = {self.KEY: self._prior()}
        ctx.previous_recipes = {self.KEY: "a-different-recipe"}

        assert self._selection(ctx, mock_plextv)["decision"] == "settings_changed"

    def _named_after(self, ctx: EngineContext, *, seed_tmdb_id: int, seed_title: str) -> None:
        """A `{top_seed}` row whose last run was named after the given watch."""
        self._ctx(ctx, refresh_days=1)
        ctx.config.rows = [replace(ctx.config.rows[0], name_template="Because you watched {top_seed}")]
        ctx.previous_picks = {
            self.KEY: [replace(p, seed_tmdb_id=seed_tmdb_id, seed_title=seed_title) for p in self._prior()]
        }
        ctx.previous_recipes = {}

    def test_a_rebuild_because_the_named_watch_changed_is_named_as_the_reason(self, ctx: EngineContext, mock_plextv):
        """Issue #133's leftover: the row is rebuilt from scratch because the watch its title names is no
        longer what it is built from — but the trace said "refreshed", the line for a row that kept its
        strongest picks. A nightly full rebuild then looked like a normal refresh to anyone reading it."""
        self._named_after(ctx, seed_tmdb_id=424242, seed_title="A Film They Un-watched")

        entry = self._selection(ctx, mock_plextv)

        assert entry["decision"] == "seed_moved"

    def test_a_named_row_whose_watch_is_unchanged_is_still_refreshed(self, ctx: EngineContext, mock_plextv):
        """The other cell: 900 is what "Fargo" resolves to here, so the named watch has not moved and the
        row keeps its strongest picks — `seed_moved` must not become the answer for every named row."""
        self._named_after(ctx, seed_tmdb_id=900, seed_title="Fargo")

        assert self._selection(ctx, mock_plextv)["decision"] == "refreshed"

    def test_it_carries_the_settings_that_decided_the_row(self, ctx: EngineContext, mock_plextv):
        """The settings are reported from the values the branch itself used, so the trace cannot
        claim one thing while the engine did another."""
        self._ctx(ctx, refresh_days=1)

        entry = self._selection(ctx, mock_plextv)

        assert entry["recency"] == 0.75
        assert entry["size"] == 3
        assert entry["candidates"] >= entry["delivered"] > 0
        assert entry["cut_cap"] == ctx.config.candidates_pre_rank


class TestTheTraceShowsWhyATitleWonOrLost:
    """A fate alone ("lost the ranking cut") does not answer "why this title over that one".

    The numbers that decided it — the release year, the score it was judged on, and the age
    multiplier the Recent releases setting applied — are what turn the trace from an outcome into an
    explanation. Recorded next to each returned title, including the ones that were dropped.
    """

    RUN_DAY = date(2026, 6, 15).toordinal()

    def _ctx(self, ctx: EngineContext, *, recency: float) -> None:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, 10: 2010, 20: 2020}
        ctx.tmdb.suggestions.return_value = [
            ({"id": 10, "title": "Old", "genre_ids": [], "vote_average": 8.6, "release_date": "1994-01-01"}, 1.0),
            ({"id": 20, "title": "New", "genre_ids": [], "vote_average": 7.1, "release_date": "2024-01-01"}, 0.9),
        ]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        # The GLOBAL, not a row override: the disposition is stamped once per shared pool, at
        # `ctx.config.recency`. A row that overrides it re-cuts afterwards, and the trace still shows
        # the pool's figures — the KNOWN GAP recorded on `_stamp_disposition`.
        ctx.config = replace(ctx.config, recency=recency)
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=2, media="movie")]
        ctx.config.min_history = 1
        ctx.run_day = self.RUN_DAY

    def _returns(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        out = {}
        for gather in report.users[0].trace.get("gathers") or []:
            for source in gather.get("sources") or []:
                for query in source.get("queries") or []:
                    for ret in query.get("returned") or []:
                        out[ret["tmdb_id"]] = ret
        assert out, "no returned titles recorded — fixture problem, not the feature"
        return out

    def test_each_returned_title_carries_its_year_and_rating(self, ctx: EngineContext, mock_plextv):
        self._ctx(ctx, recency=0.0)

        rets = self._returns(ctx, mock_plextv)

        assert rets[10]["year"] == 1994
        assert rets[20]["year"] == 2024
        assert rets[10]["rating"] == 8.6

    def test_the_age_multiplier_shows_what_the_setting_did_to_each_title(self, ctx, mock_plextv):
        """The number that makes "why did the 1994 one win?" answerable: at full strength the older
        title is scored at a fraction of the newer one."""
        self._ctx(ctx, recency=1.0)

        rets = self._returns(ctx, mock_plextv)

        assert rets[20]["age_weight"] > rets[10]["age_weight"], rets
        assert rets[10]["age_weight"] < 0.2, f"a 32-year-old title should be heavily weighted down: {rets[10]}"

    def test_the_multiplier_is_one_when_the_setting_is_off(self, ctx: EngineContext, mock_plextv):
        """Not absent, and not a made-up number — 1.0 is the truth when age was not consulted."""
        self._ctx(ctx, recency=0.0)

        rets = self._returns(ctx, mock_plextv)

        assert rets[10]["age_weight"] == 1.0
        assert rets[20]["age_weight"] == 1.0


class TestRowLimitsRecipe:
    """#138 phase 2: limits join the recipe only when set, so no existing row rebuilds on upgrade."""

    @staticmethod
    def _recipe(**spec_kw) -> str:
        from shortlist.engine.models import EngineConfig
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver, row_recipe

        cfg = EngineConfig()
        spec = RowSpec(slug="picked", name_template="", size=4, media="movie", **spec_kw)
        ctx = MagicMock()
        ctx.config = cfg
        policy = RowPolicy(
            ctx=ctx,
            user=make_profile("sarah", account_id=100),
            cfg=cfg,
            specs=[spec],
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )
        return row_recipe(policy, spec)

    def test_recipe_is_byte_identical_when_no_limit_is_set(self):
        # Captured from the parent commit, before row_recipe was touched.
        assert self._recipe() == "movie||tmdb_similar|0.0|0.0|False|False|30|1|popular"

    def test_a_limit_changes_the_recipe_and_names_it(self):
        recipe = self._recipe(max_runtime=120)
        assert recipe == "movie||tmdb_similar|0.0|0.0|False|False|30|1|popular|limits=rt<=120"

    def test_clearing_the_limits_restores_the_original_recipe(self):
        original = self._recipe()
        assert self._recipe(min_year=1990) != original
        assert self._recipe(min_year=None, max_runtime=None) == original

    def test_a_zero_min_rating_leaves_the_recipe_alone(self):
        assert self._recipe(min_rating=0.0) == self._recipe()


class TestRowLimitsInThePipeline:
    """A row's limits narrow the pool before ranking, so the picks are drawn only from titles inside them."""

    def _setup(self, ctx: EngineContext, mock_plextv, **spec_kw):
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 2000 + i for i in range(10, 20)}}
        pool = [
            {
                "id": i,
                "title": f"T{i}",
                "genre_ids": [],
                "vote_average": 8.0,
                "release_date": f"{1970 + (i - 10) * 6}-01-01",
            }
            for i in range(10, 20)
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.tmdb.details.side_effect = lambda tid, mt: {"runtime": 90 if tid <= 12 else 150}
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=4, media="movie", **spec_kw)]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        return sorted(p["tmdb_id"] for p in picks)

    def test_max_year_keeps_only_titles_released_by_then(self, ctx, mock_plextv):
        ids = self._setup(ctx, mock_plextv, max_year=2000)
        assert ids == [10, 11, 12, 13]
        ctx.tmdb.details.assert_not_called()

    def test_max_runtime_asks_details_only_for_titles_that_passed_the_year_limit(self, ctx, mock_plextv):
        ids = self._setup(ctx, mock_plextv, max_year=2000, max_runtime=100)
        assert ids == [10, 11, 12]
        asked = {c.args[0] for c in ctx.tmdb.details.call_args_list if c.args[0] >= 10}
        assert asked == {10, 11, 12, 13, 14, 15}

    def test_no_limits_still_fills_the_row_from_the_whole_pool(self, ctx, mock_plextv):
        ids = self._setup(ctx, mock_plextv)
        assert len(ids) == 4


class TestLimitsReachRewatchAndColdStart:
    """Rewatch titles and cold-start fillers are built outside the candidate pool, so the pool's limits never
    saw them: a "released by 2000" Watch-it-again row could lead with a 2021 film."""

    RUN_DAY = date(2026, 6, 15).toordinal()

    def _rewatch(self, ctx: EngineContext, mock_plextv, **spec_kw) -> list[int]:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, 10: 2010, 20: 2020, 30: 2030}
        ctx.tmdb.suggestions.return_value = _ranked(
            [{"id": 30, "title": "Fresh", "genre_ids": [], "vote_average": 7.0, "release_date": "1990-01-01"}]
        )
        ctx.tmdb.details.side_effect = lambda tid, mt: {"runtime": 90 if tid != 20 else 180}
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            make_watched("Old Favourite", days_ago=6, tmdb_id=10, year=1995),
            # Watched longest ago, so unlimited it leads the row.
            make_watched("New Film", days_ago=7, tmdb_id=20, year=2021),
        ]
        ctx.config.max_seeds = 1
        ctx.config.rows = [RowSpec(slug="again", name_template="Again", size=2, rewatch=True, **spec_kw)]
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        return [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]

    def test_a_rewatch_row_without_limits_still_leads_with_the_newer_film(self, ctx, mock_plextv):
        assert self._rewatch(ctx, mock_plextv)[0] == 20

    def test_a_rewatch_row_does_not_lead_with_a_film_past_its_max_year(self, ctx, mock_plextv):
        delivered = self._rewatch(ctx, mock_plextv, max_year=2000)
        assert 20 not in delivered
        assert 10 in delivered

    def test_a_rewatch_row_asks_details_to_judge_the_runtime_of_its_history(self, ctx, mock_plextv):
        delivered = self._rewatch(ctx, mock_plextv, max_runtime=120)
        assert 20 not in delivered
        assert 10 in delivered
        asked = {c.args for c in ctx.tmdb.details.call_args_list}
        assert (20, MediaType.MOVIE) in asked

    def _cold(self, ctx: EngineContext, mock_plextv, **spec_kw) -> list[int]:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.delivery_sections = [movies]
        ctx.plex.build_library_index.return_value = {}
        years = {100: 2021, 101: 1995, 102: 1990, 103: 1985, 104: 1980, 105: 1975}
        ctx.plex.top_rated.side_effect = lambda section, n: [
            (tid, fake_media_item(9000 + tid, f"Top{tid}", tmdb_id=tid, year=years[tid])) for tid in list(years)[:n]
        ]
        ctx.tmdb.details.side_effect = lambda tid, mt: {"runtime": 180 if tid == 101 else 100}
        ctx.history_source.fetch.return_value = []
        ctx.config.min_history = 5
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=2, media="movie", **spec_kw)]
        ctx.run_day = self.RUN_DAY
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        return [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]

    def test_a_newcomer_row_without_limits_gets_the_top_rated_titles(self, ctx, mock_plextv):
        assert self._cold(ctx, mock_plextv) == [100, 101]

    def test_a_newcomer_does_not_get_a_film_past_the_max_year(self, ctx, mock_plextv):
        delivered = self._cold(ctx, mock_plextv, max_year=2000)
        assert len(delivered) == 2
        assert 100 not in delivered

    def test_a_newcomer_does_not_get_a_film_past_the_max_runtime(self, ctx, mock_plextv):
        delivered = self._cold(ctx, mock_plextv, max_runtime=120)
        assert len(delivered) == 2
        assert 101 not in delivered
        assert (101, MediaType.MOVIE) in {c.args for c in ctx.tmdb.details.call_args_list}


class TestLimitsAndRequestDemand:
    """A title a row's limits would never show must not be credited to that row's request tag."""

    def _demand(self, ctx: EngineContext, mock_plextv, monkeypatch, **spec_kw):
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport

        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {
                    "id": 30,
                    "title": "Recent",
                    "genre_ids": [],
                    "vote_average": 8.4,
                    "vote_count": 800,
                    "release_date": "2024-01-01",
                },
                {
                    "id": 31,
                    "title": "Old",
                    "genre_ids": [],
                    "vote_average": 8.2,
                    "vote_count": 800,
                    "release_date": "1999-01-01",
                },
            ]
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, media="movie", **spec_kw)]
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
        )
        captured = {}

        def spy(cfg, tmdb, demand, **kw):
            captured["demand"] = demand
            return RequestReport()

        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", spy)
        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        return {row.slug: set(row.demand) for row in captured.get("demand", [])}.get("picked", set())

    def test_a_row_with_no_limits_credits_every_missing_title(self, ctx, mock_plextv, monkeypatch):
        assert self._demand(ctx, mock_plextv, monkeypatch) == {(30, MediaType.MOVIE), (31, MediaType.MOVIE)}

    def test_a_row_with_a_max_year_credits_only_the_missing_titles_it_could_show(self, ctx, mock_plextv, monkeypatch):
        assert self._demand(ctx, mock_plextv, monkeypatch, max_year=2000) == {(31, MediaType.MOVIE)}


class TestRowsDifferingOnlyByLimitsShareOneGather:
    """Native AI web search has no cache, so a limited row that gathered on its own cost an extra search."""

    def _run(self, ctx: EngineContext, mock_plextv, rows: list[RowSpec]) -> dict[str, list[int]]:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 2000 + i for i in range(10, 20)}}
        pool = [
            {
                "id": i,
                "title": f"T{i}",
                "genre_ids": [],
                "vote_average": 8.0,
                "release_date": f"{1970 + (i - 10) * 6}-01-01",
            }
            for i in range(10, 20)
        ]
        ctx.tmdb.suggestions.reset_mock()
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = rows
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        out: dict[str, list[int]] = {}
        for pick in report.users[0].picks:
            out.setdefault(pick.collection_slug, []).append(pick.tmdb_id)
        return {slug: sorted(ids) for slug, ids in out.items()}

    @staticmethod
    def _row(slug: str, **kw) -> RowSpec:
        return RowSpec(slug=slug, name_template=slug, size=10, media="movie", **kw)

    def test_a_limited_row_and_an_unlimited_one_gather_once(self, ctx, mock_plextv):
        self._run(ctx, mock_plextv, [self._row("all")])
        alone = ctx.tmdb.suggestions.call_count

        picks = self._run(ctx, mock_plextv, [self._row("all"), self._row("old", max_year=2000)])

        assert ctx.tmdb.suggestions.call_count == alone
        assert len(picks["all"]) == 10
        assert picks["old"] == [10, 11, 12, 13, 14, 15]

    def test_rows_with_different_limits_share_one_gather_and_get_different_pools(self, ctx, mock_plextv):
        self._run(ctx, mock_plextv, [self._row("all")])
        alone = ctx.tmdb.suggestions.call_count

        picks = self._run(ctx, mock_plextv, [self._row("old", max_year=2000), self._row("older", max_year=1985)])

        assert ctx.tmdb.suggestions.call_count == alone
        assert picks["old"] == [10, 11, 12, 13, 14, 15]
        assert picks["older"] == [10, 11, 12]

    def test_the_limited_pool_is_labelled_and_the_log_names_the_person_and_row(self, ctx, mock_plextv):
        from loguru import logger

        lines: list[str] = []
        sink = logger.add(lines.append, level="INFO", format="{message}")
        try:
            self._run(ctx, mock_plextv, [self._row("all"), self._row("old", max_year=2000)])
        finally:
            logger.remove(sink)

        assert any(line.startswith("sarah/old: limits: kept=6 dropped=4 unknown=0") for line in lines)

    def _two_row_run(self, ctx, mock_plextv, rows):
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 2000 + i for i in range(10, 20)}}
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 10, "title": "T", "genre_ids": [], "vote_average": 8.0, "release_date": "1970-01-01"}]
        )
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = rows
        ctx.config.min_history = 1
        mock_plextv.users = [plextv_user(100, "sarah")]
        return pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]

    def test_a_single_limited_row_labels_its_pool_limits(self, ctx, mock_plextv):
        user = self._two_row_run(ctx, mock_plextv, [self._row("old", max_year=2000)])
        assert [c["label"] for c in user.pool_costs] == ["movie · tmdb_similar · limits"]

    def test_rows_sharing_a_gather_file_one_trace_entry_and_one_cost_entry(self, ctx, mock_plextv):
        for rows in (
            [self._row("all"), self._row("old", max_year=2000)],
            [self._row("old", max_year=2000), self._row("all")],
        ):
            user = self._two_row_run(ctx, mock_plextv, rows)

            assert len(user.trace["gathers"]) == 1
            assert [c["label"] for c in user.pool_costs] == ["movie · tmdb_similar"]
            assert sorted(user.pool_costs[0]["rows"]) == ["all", "old"]


class TestSharedGatherIsRankedSafelyTwice:
    """Two rows sharing a gather rank the SAME candidate objects, so no ranking step may leave a mark the
    other row's ranking reads."""

    @staticmethod
    def _rows(order: str) -> list[RowSpec]:
        plain = RowSpec(slug="all", name_template="all", size=3, media="movie")
        limited = RowSpec(slug="old", name_template="old", size=3, media="movie", max_year=2000)
        return [plain, limited] if order == "plain-first" else [limited, plain]

    def _run(self, ctx, mock_plextv, rows):
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 2000 + i for i in range(10, 20)}}
        pool = [
            {
                "id": i,
                "title": f"T{i}",
                "genre_ids": [],
                "vote_average": (7.0 if i < 15 else 7.2) - 0.01 * i,
                "release_date": "1980-01-01" if i < 15 else "2010-01-01",
            }
            for i in range(10, 20)
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.tmdb.details.side_effect = lambda tid, mt: {
            "runtime": 90,
            "belongs_to_collection": {"id": 7, "name": "Dune Collection"},
        }
        ctx.tmdb.collection_members.side_effect = lambda cid: set(range(10, 20))
        casts = {900: ["Lead"], **{i: ["Lead", f"Extra{i}"] for i in range(10, 15)}}
        ctx.tmdb.top_cast.side_effect = lambda tid, mt, n=5: casts.get(tid, [f"Other{tid}"])
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = rows
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 3
        mock_plextv.users = [plextv_user(100, "sarah")]
        return pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]

    @pytest.mark.parametrize("order", ["plain-first", "limited-first"])
    def test_a_franchise_reason_is_stamped_once_however_many_rows_share_the_gather(self, ctx, mock_plextv, order):
        ctx.config.franchise = 1.0

        user = self._run(ctx, mock_plextv, self._rows(order))

        reasons = [p.reason for p in user.picks]
        assert reasons and all("also part of" in r for r in reasons)
        assert all(r.count("also part of") == 1 for r in reasons), reasons

    @pytest.mark.parametrize("order", ["plain-first", "limited-first"])
    def test_the_cast_dial_ranks_the_plain_row_the_same_with_or_without_a_limited_sibling(
        self, ctx, mock_plextv, order
    ):
        ctx.config.cast = 1.0
        alone = self._run(ctx, mock_plextv, self._rows("plain-first")[:1])
        alone_ids = sorted(p.tmdb_id for p in alone.picks if p.collection_slug == "all")

        shared = self._run(ctx, mock_plextv, self._rows(order))

        assert sorted(p.tmdb_id for p in shared.picks if p.collection_slug == "all") == alone_ids
