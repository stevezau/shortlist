"""Row refresh cadence, named-row carry-forward, and pick order."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from dataclasses import replace

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import (
    strip_marker,
)
from shortlist.engine.models import (
    MediaType,
    Pick,
    RowSpec,
)
from tests.conftest import make_profile, make_watched, plextv_user
from tests.unit.pipeline_support import (
    _ranked,
    ctx,  # noqa: F401
    spy_build_picks,
)
from tests.unit.row_overrides_support import RowCtxHelpers


class TestRowRefreshAndNaming(RowCtxHelpers):
    """Which picks a row keeps, swaps or rebuilds from night to night, and when its seed-named title follows."""

    def test_non_refresh_night_reuses_prior_picks_without_rebuilding(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Freshness 0 = a frozen row: after the first build it redelivers last run's picks unchanged
        and never rebuilds the row (no wasted work, and delivery's unchanged-skip avoids the Plex
        write too) — the fix for nightly churn."""
        self._movie_row_ctx(ctx, refresh_days=0, run_day=5)
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built == []  # reused, not rebuilt
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert [p["tmdb_id"] for p in picks] == [12, 13, 14, 15, 16]  # exactly last run's row, in order

    def test_refresh_night_keeps_the_strong_two_thirds_and_swaps_the_rest(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """On a refresh night the strongest ~two-thirds carry over and the rest are swapped for titles
        NOT already in the row, so a just-rotated-out pick can't immediately bounce back."""
        self._movie_row_ctx(ctx, refresh_days=1, run_day=5)  # 1.0 = refresh every night
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built  # a refresh night DOES rebuild the swapped-in slots
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ids = [p["tmdb_id"] for p in picks]
        assert {12, 13, 14} <= set(ids), f"the strongest two-thirds of last run's row survive, got {ids}"
        assert {15, 16}.isdisjoint(ids), f"the weakest third is swapped out, got {ids}"
        assert not {15, 16} & set(ids), f"a just-rotated-out pick can't bounce straight back, got {ids}"

    def test_refresh_night_lets_a_newcomer_outrank_a_survivor(self, ctx: EngineContext, mock_plextv):
        """Survivors and newcomers are ranked TOGETHER against tonight's pool, so a better newcomer
        takes the head of the row. Concatenating `kept + new` instead pinned last run's top
        two-thirds to positions 1..keep_n for ever — on a 20-title row, 13 slots that never moved
        again however the candidates scored."""
        self._movie_row_ctx(ctx, refresh_days=1, run_day=5)
        # Last run held the pool's WEAKER half; 10 and 11 rank above all of them tonight.
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ids = [p["tmdb_id"] for p in picks]
        assert ids == [10, 11, 12, 13, 14], f"row ordered by tonight's ranking, not last run's, got {ids}"
        assert ids[0] not in {12, 13, 14, 15, 16}, f"a newcomer can reach position 1, got {ids}"
        assert [p["rank"] for p in picks] == [1, 2, 3, 4, 5], "ranks renumbered to the delivered order"

    def _named_row_ctx(self, ctx, *, refresh_days: int, max_seeds: int = 1):
        """The `_movie_row_ctx` world, but with a row NAMED after the watch it is built from."""
        self._movie_row_ctx(ctx, refresh_days=refresh_days, run_day=5)
        ctx.config.rows = [
            RowSpec(
                slug="picked",
                name_template="Because you watched {top_seed}",
                size=5,
                media="movie",
                refresh_days=refresh_days,
                max_seeds=max_seeds,
            )
        ]

    def test_a_named_row_rebuilds_when_the_seed_it_names_has_changed(self, ctx: EngineContext, mock_plextv):
        """A `{top_seed}` row's title renders from pick #1's seed, and the refresh branch always
        carries pick #1 forward — so without the seed check the row stays named after the FIRST watch
        that ever seeded it while its tail fills from newer ones. This person's only seed is Fargo."""
        self._named_row_ctx(ctx, refresh_days=1)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        titles = [strip_marker(t) for _library, t in report.users[0].placement_titles]
        assert titles == ["Because you watched Fargo"]
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert {p["seed_title"] for p in picks} == {"Fargo"}, "every pick answers to the seed the row names"

    def test_a_named_row_carries_forward_while_its_seed_is_unchanged(self, ctx: EngineContext, mock_plextv):
        """The seed check must not turn every refresh into a full rebuild: while the row is still
        built from the seed it is named after, the normal keep-two-thirds carry-forward applies."""
        self._named_row_ctx(ctx, refresh_days=1)
        # 900 is what "Fargo" resolves to in this fixture, so the seed has NOT moved.
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by([12, 13, 14, 15, 16], seed_tmdb_id=900, seed_title="Fargo")
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ids = [p["tmdb_id"] for p in picks]
        assert {12, 13, 14} <= set(ids), f"unchanged seed keeps the normal carry-forward, got {ids}"
        titles = [strip_marker(t) for _library, t in report.users[0].placement_titles]
        assert titles == ["Because you watched Fargo"]

    def test_a_named_row_rebuilds_when_RANKING_moves_the_seed_its_title_uses(self, ctx: EngineContext, mock_plextv):
        """The cell the single-seed tests could never reach: a `{top_seed}` row with MORE than one seed.

        `_seed_moved` asks whether the POOL still leads with the named seed. The title asks something
        subtly different — it renders from the best-matching DELIVERED pick — so re-ranking survivors
        against newcomers can put a differently-seeded newcomer first while the pool's top seed never
        moved. The row then renamed itself while still carrying the old seed's picks, which is the
        stale claim the whole mechanism exists to prevent.
        """
        self._two_seed_named_row_ctx(ctx, "best")
        # Last run's row is seeded by Fargo and carries Fargo's weaker (F1x) titles, so tonight's
        # ranking hands the lead to a Chernobyl-seeded newcomer.
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by([10, 11, 12, 13, 14], seed_tmdb_id=900, seed_title="Fargo")
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        lead = min(picks, key=lambda p: p["rank"])
        titles = [strip_marker(t) for _library, t in report.users[0].placement_titles]
        assert titles == [f"Because you watched {lead['seed_title']}"], f"got {titles}, lead {lead}"
        # Not "every pick shares that seed" — above one seed a `{top_seed}` row names its strongest
        # watch and legitimately holds others, which is the trade-off the seed-budget callout warns
        # about. The guarantee is narrower and is the one that was broken: the row never keeps
        # claiming a watch it is no longer led by.
        assert lead["seed_title"] in {p["seed_title"] for p in picks}
        assert titles != ["Because you watched Fargo"] or lead["seed_title"] == "Fargo", (
            f"the title cannot outlive the seed that earned it, got {titles} with lead {lead}"
        )

    def _unseeded_lead_ctx(self, ctx, *, discover_leads: bool = True, similar: bool = True):
        """A named one-seed row whose pool also holds UNSEEDED titles (10-12, from discover).

        Fargo (yesterday) is the only seed; Chernobyl (five days ago) is the older watch a stale row
        still names. Fargo's look-alikes (13-19) are weak matches, so with ``discover_leads`` the
        unseeded titles outscore them and lead the pool — the shape of the issue #133 reporter's
        rows, whose discover and web-search picks score affinity 1.0 and seed nothing.
        """
        self._named_row_ctx(ctx, refresh_days=1)
        ctx.config.rows = [replace(ctx.config.rows[0], candidate_sources=["tmdb_similar", "tmdb_discover"])]
        ctx.plex.build_library_index.return_value = {900: 999, 901: 998, **{i: 1000 + i for i in range(10, 20)}}
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),
            make_watched("Chernobyl", days_ago=5, rating_key=998),
        ]
        look_alikes = [{"id": i, "title": f"T{i}", "genre_ids": [18], "vote_average": 8.0} for i in range(13, 20)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: [(item, 0.2) for item in look_alikes] if similar else []
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        vote = 9.0 if discover_leads else 5.0
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": i, "title": f"T{i}", "genre_ids": [18], "vote_average": vote} for i in (10, 11, 12)
        ]

    def _prior_led_by(self, lead: int, seeded: list[int], *, seed_tmdb_id: int, seed_title: str):
        """Last run's row: ``lead`` at rank 1 carrying NO seed, then ``seeded`` carrying the given one."""
        unseeded, *rest = self._prior_movies([lead, *seeded])
        return [unseeded] + [replace(p, seed_tmdb_id=seed_tmdb_id, seed_title=seed_title) for p in rest]

    def _run_sarah(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        titles = [strip_marker(t) for _library, t in report.users[0].placement_titles]
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        return titles, {p["tmdb_id"] for p in picks}, {p["seed_title"] for p in picks}

    def test_a_named_row_rebuilds_when_its_seed_moved_behind_an_unseeded_lead(self, ctx: EngineContext, mock_plextv):
        """Issue #133: the title renders from the best pick that HAS a seed (`top_seed_of`, #84), so the
        check deciding whether that seed moved must skip unseeded picks too. It compared pick #1 with
        tonight's pool lead as they stood, and when both came from a source that seeds nothing it read
        "no seed" == "no seed" as unchanged — the refresh then carried Chernobyl's picks forward, and
        the row said "Because you watched Chernobyl" night after night about a watch that was no
        longer a seed at all."""
        self._unseeded_lead_ctx(ctx)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_led_by(10, [12, 13, 14, 15], seed_tmdb_id=901, seed_title="Chernobyl")
        }

        titles, _ids, seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert "Chernobyl" not in seeds, "no pick still answers to the old watch"

    def test_a_named_row_carries_forward_behind_an_unseeded_lead_while_its_seed_is_unchanged(
        self, ctx: EngineContext, mock_plextv
    ):
        """The other half of #133's cell: both leads unseeded and the named seed NOT moved must keep the
        normal carry-forward, or every refresh of such a row becomes a full rebuild. 19 and 18 are the
        weakest look-alikes: only a carry-forward keeps them over tonight's stronger 11-14."""
        self._unseeded_lead_ctx(ctx)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_led_by(10, [19, 18, 17, 16], seed_tmdb_id=900, seed_title="Fargo")
        }

        titles, ids, _seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert {19, 18} <= ids, f"an unchanged seed keeps the normal carry-forward, got {ids}"

    def test_a_named_row_carries_forward_when_only_the_pool_lead_is_unseeded(self, ctx: EngineContext, mock_plextv):
        """Last run's #1 carried the seed, tonight's pool lead carries none, the seed is unchanged. Comparing
        the two leads as they stood read Fargo != "no seed" as a moved seed and rebuilt every night."""
        self._unseeded_lead_ctx(ctx)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by([19, 18, 17, 16, 15], seed_tmdb_id=900, seed_title="Fargo")
        }

        titles, ids, _seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert {19, 18} <= ids, f"an unchanged seed keeps the normal carry-forward, got {ids}"

    def test_a_named_row_carries_forward_when_only_its_own_lead_is_unseeded(self, ctx: EngineContext, mock_plextv):
        """The mirror cell: last run's #1 carried no seed, tonight's pool leads with the unchanged one."""
        self._unseeded_lead_ctx(ctx, discover_leads=False)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_led_by(10, [19, 18, 17, 16], seed_tmdb_id=900, seed_title="Fargo")
        }

        titles, ids, _seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert {10, 19, 18} <= ids, f"an unchanged seed keeps the normal carry-forward, got {ids}"

    def test_a_named_row_rebuilds_when_no_candidate_carries_a_seed_any_more(self, ctx: EngineContext, mock_plextv):
        """The named seed has gone and nothing in tonight's pool is seeded (Fargo's look-alikes are not in
        the library). The rebuilt row is named after Fargo, the watch it was built from — never the old
        watch, and never nothing: an empty name left the old collection on Plex (issue #133)."""
        self._unseeded_lead_ctx(ctx, similar=False)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_led_by(10, [12, 13, 14, 15], seed_tmdb_id=901, seed_title="Chernobyl")
        }
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        titles = [strip_marker(t) for _library, t in report.users[0].placement_titles]
        assert titles == ["Because you watched Fargo"]

    def test_a_row_named_after_its_lead_seed_carries_forward_while_that_watch_is_unchanged(
        self, ctx: EngineContext, mock_plextv
    ):
        """Issue #133. Last run's row carried no seeded pick, so it was named after Fargo, the watch it was
        built from, and the stamp says so. Fargo is still the newest watch: keep the normal carry-forward."""
        self._unseeded_lead_ctx(ctx, similar=False)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): [
                replace(p, lead_seed_tmdb_id=900, lead_seed_title="Fargo")
                for p in self._prior_movies([13, 14, 15, 16, 17])
            ]
        }

        titles, ids, _seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert {13, 14} <= ids, f"an unchanged watch keeps the normal carry-forward, got {ids}"

    def test_a_row_named_after_its_lead_seed_rebuilds_when_that_watch_moves_on(self, ctx: EngineContext, mock_plextv):
        """The same row stamped with Chernobyl, which Fargo has since replaced: rebuild from tonight's pool
        rather than carry two-thirds of Chernobyl's row forward under Fargo's name."""
        self._unseeded_lead_ctx(ctx, similar=False)
        ctx.previous_picks = {
            ("sarah", "picked", "1"): [
                replace(p, lead_seed_tmdb_id=901, lead_seed_title="Chernobyl")
                for p in self._prior_movies([13, 14, 15, 16, 17])
            ]
        }

        titles, ids, _seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert not {13, 14, 15, 16, 17} & ids, f"the old watch's row outlived it, got {ids}"

    def test_a_row_with_no_recorded_lead_seed_carries_forward_as_before(self, ctx: EngineContext, mock_plextv):
        """Picks written before the stamp existed read as "unknown". While tonight's pool follows no watch
        either, that is no move, so the row carries forward rather than rebuilding every night. The title
        still renders from tonight's watch. (A seeded pool is a move — see the test below.)"""
        self._unseeded_lead_ctx(ctx, similar=False)
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([13, 14, 15, 16, 17])}

        titles, ids, _seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert {13, 14} <= ids, f"an unknown stamp must not force a rebuild, got {ids}"

    def test_an_unstamped_row_with_no_seeded_pick_rebuilds_once_its_pool_follows_a_watch(
        self, ctx: EngineContext, mock_plextv
    ):
        """A row whose last picks carry no seed AND no stamp — a cold-start row (the server's popular
        titles), or one #133 froze before the stamp existed — was rebuilt by 1.9.2 the night its pool
        first followed a watch. Reading "unknown" as "unmoved" instead kept most of those picks under a
        brand-new "Because you watched Fargo" title (found by the 1.9.3 release review)."""
        self._named_row_ctx(ctx, refresh_days=1)
        ctx.config.rows = [replace(ctx.config.rows[0], candidate_sources=["tmdb_similar"])]
        ctx.plex.build_library_index.return_value = {
            900: 999,
            **{i: 1000 + i for i in range(10, 20)},
            **{i: 2000 + i for i in range(30, 35)},
        }
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        look_alikes = [{"id": i, "title": f"T{i}", "genre_ids": [18], "vote_average": 8.0} for i in range(10, 20)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: [(item, 0.9) for item in look_alikes]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([30, 31, 32, 33, 34])}

        titles, ids, seeds = self._run_sarah(ctx, mock_plextv)

        assert titles == ["Because you watched Fargo"]
        assert not {30, 31, 32, 33, 34} & ids, f"the unseeded row was carried forward under Fargo's name, got {ids}"
        assert seeds == {"Fargo"}

    def _three_seed_named_row_ctx(self, ctx, *, twelve_affinity: float = 0.2, chernobyl_finds_twelve: bool = False):
        """A `{top_seed}` row built from up to three watches in one Movies library — that server's per-row budget.

        Fargo (900) is the newest watch; its look-alikes 20-22 are middling matches. Heat (902) has one
        look-alike, 12, a weak match that discover ALSO finds. Chernobyl (901) has weak look-alikes 25-27,
        and with ``chernobyl_finds_twelve`` finds 12 as well. Discover's 10-12 score above Fargo's
        look-alikes once they carry no seed of their own.
        """
        self._named_row_ctx(ctx, refresh_days=1, max_seeds=3)
        ctx.config.rows = [replace(ctx.config.rows[0], candidate_sources=["tmdb_similar", "tmdb_discover"])]
        ctx.plex.build_library_index.return_value = {
            900: 999,
            901: 998,
            902: 997,
            **{i: 1000 + i for i in range(10, 30)},
        }

        def item(tmdb_id: int, vote: float) -> dict:
            return {"id": tmdb_id, "title": f"T{tmdb_id}", "genre_ids": [18], "vote_average": vote}

        similar = {
            900: [(item(i, 8.0), 0.25) for i in (20, 21, 22)],
            902: [(item(12, 9.0), twelve_affinity)],
            901: [(item(i, 5.0), 0.25) for i in (25, 26, 27)],
        }
        if chernobyl_finds_twelve:
            similar[901].append((item(12, 9.0), twelve_affinity))
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: similar.get(tid, [])
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [item(i, 9.0) for i in (10, 11, 12)]

    def _night(self, ctx, mock_plextv, *watched: tuple[str, int, int]):
        """One run from ``(title, days_ago, rating_key)`` watches, carrying its picks and recipe into the
        next run the way `context_builder` does. Returns the titles, the picks, and the trace decision."""
        ctx.history_source.fetch.return_value = [make_watched(t, days_ago=d, rating_key=rk) for t, d, rk in watched]
        mock_plextv.users = [plextv_user(100, "sarah")]
        user = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]
        picks = sorted(user.picks, key=lambda p: p.rank)
        ctx.previous_picks = {("sarah", "picked", "1"): [replace(p, section_key=str(p.section_key)) for p in picks]}
        ctx.previous_recipes = {("sarah", "picked", "1"): picks[0].recipe}
        titles = [strip_marker(t) for _library, t in user.placement_titles]
        return titles, picks, (user.trace.get("selection") or [{}])[0].get("decision")

    def test_a_refresh_never_names_a_watch_that_left_the_seed_set(self, ctx: EngineContext, mock_plextv):
        """Above one seed per library, `_seed_moved` can pass while the title moves onto a dead watch.

        Night one is named after Fargo and carries 12 at rank 3, found as Heat's look-alike. Heat is then
        un-watched. Fargo still leads tonight's pool, so the row refreshes and keeps 12 — which is now a
        discover title with no seed, outranks Fargo's look-alikes, and still carries last night's seed. The
        title said "Because you watched Heat", a watch no longer in the seed set, until the next night's
        check saw that name and rebuilt the whole row.
        """
        self._three_seed_named_row_ctx(ctx)
        fargo, heat, chernobyl = ("Fargo", 1, 999), ("Heat", 2, 997), ("Chernobyl", 3, 998)
        first, first_picks, _ = self._night(ctx, mock_plextv, fargo, heat, chernobyl)
        assert first == ["Because you watched Fargo"]
        assert {p.tmdb_id: p.seed_title for p in first_picks}.get(12) == "Heat", "the scenario needs 12 from Heat"

        second, picks, decision = self._night(ctx, mock_plextv, fargo, chernobyl)
        _third, _, next_decision = self._night(ctx, mock_plextv, fargo, chernobyl)

        assert decision == "refreshed" and 12 in {p.tmdb_id for p in picks}, "a refresh keeps its strong picks"
        assert second == ["Because you watched Fargo"], f"named a watch outside tonight's seeds: {second}"
        assert next_decision == "refreshed", "an honest title leaves the next night nothing to rebuild"

    def test_a_survivor_whose_watch_left_answers_to_a_live_watch_that_still_finds_it(
        self, ctx: EngineContext, mock_plextv
    ):
        """The same refresh when a watch still in the seed set (Chernobyl) ALSO finds 12: the kept pick
        answers to Chernobyl, as a newcomer of the same title would — not to Heat, and not to nothing."""
        self._three_seed_named_row_ctx(ctx, twelve_affinity=0.1, chernobyl_finds_twelve=True)
        fargo, heat, chernobyl = ("Fargo", 1, 999), ("Heat", 2, 997), ("Chernobyl", 3, 998)
        _, first_picks, _ = self._night(ctx, mock_plextv, fargo, heat, chernobyl)
        assert {p.tmdb_id: p.seed_title for p in first_picks}.get(12) == "Heat", "the scenario needs 12 from Heat"

        titles, picks, decision = self._night(ctx, mock_plextv, fargo, chernobyl)

        assert decision == "refreshed"
        assert {p.tmdb_id: p.seed_title for p in picks}.get(12) == "Chernobyl"
        assert titles == ["Because you watched Fargo"]

    def test_an_unnamed_row_ignores_the_seed_check(self, ctx: EngineContext, mock_plextv):
        """A row that names no seed keeps the cheap carry-forward however far its seeds have drifted —
        re-deriving a normal 30-seed row on any seed change would make every refresh a full rebuild."""
        self._movie_row_ctx(ctx, refresh_days=1, run_day=5)  # name_template="" — names no seed
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert {12, 13, 14} <= {p["tmdb_id"] for p in picks}, "seed drift alone does not rebuild an unnamed row"

    def test_a_named_row_follows_its_seed_even_when_stored_frozen(self, ctx: EngineContext, mock_plextv):
        """A `{top_seed}` row ignores a stored cadence — even 0, which freezes any other row.

        A row whose title names a watch is ABOUT recency, so a slow cadence makes it claim a watch the
        person moved on from days ago (issue #57: "it still says Because you watched Little Brother",
        reported twice). Forced rather than merely defaulted because the row editor HIDES the cadence
        control for these rows — honouring a slow value saved before that would strand the row with
        nothing in the UI to explain it or undo it.
        """
        self._named_row_ctx(ctx, refresh_days=0)  # 0.0 freezes any row that does NOT name its seed
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        titles = [strip_marker(t) for _library, t in report.users[0].placement_titles]
        assert titles != ["Because you watched Chernobyl"], "a frozen cadence must not strand the title"
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        lead = min(picks, key=lambda p: p["rank"])
        assert titles == [f"Because you watched {lead['seed_title']}"], f"got {titles}, lead {lead}"

    def test_an_unnamed_row_still_freezes_at_zero(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """The nightly override is scoped to rows that name a seed. Everywhere else 0 still means
        "never refresh once built" — the control is still offered for those rows, so it must still work."""
        self._movie_row_ctx(ctx, refresh_days=0, run_day=5)  # name_template="" — names no seed
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by(
                [12, 13, 14, 15, 16], seed_tmdb_id=555, seed_title="Chernobyl"
            )
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built == [], "a frozen row is redelivered, never rebuilt"
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert [p["tmdb_id"] for p in picks] == [12, 13, 14, 15, 16]


class TestRowPickOrder(RowCtxHelpers):
    """The order a row is displayed in, and what that does not change."""

    def _ordered_row_ctx(self, ctx, pick_order: str, *, run_day: int = 5):
        """`_movie_row_ctx`, but the pool carries DISTINCT ratings and years so an order is visible.

        tmdb 10..19 get descending ratings (10 is best) and ascending years (19 is newest), so
        "rating" and "newest" produce opposite orders and neither can be confused with the ranking.
        """
        self._movie_row_ctx(ctx, refresh_days=1, run_day=run_day)
        pool = [
            {
                "id": i,
                "title": f"T{i}",
                "genre_ids": [],
                "vote_average": 9.5 - (i - 10) * 0.5,
                "release_date": f"{2000 + i}-01-01",
            }
            for i in range(10, 20)
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie", pick_order=pick_order)]

    def _delivered_ids(self, report):
        """The row as DELIVERED, in the order it is written to Plex.

        `rank` is deliberately NOT the delivered position: it is stamped from the selection order and
        means "how good a match", which is what names a `{top_seed}` row and what carry-forward keeps
        the strongest two-thirds by. So every pick still carries a distinct 1..n rank, but for any
        order other than "best" those ranks are a permutation of the delivered order, not equal to it.
        """
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ranks = [p["rank"] for p in picks]
        assert sorted(ranks) == list(range(1, len(picks) + 1)), f"each pick keeps a distinct match rank, got {ranks}"
        return [p["tmdb_id"] for p in picks]

    def test_pick_order_best_leaves_the_ranking_alone(self, ctx: EngineContext, mock_plextv):
        """The default must be a genuine no-op — it is what every existing row is migrated to."""
        self._ordered_row_ctx(ctx, "best")
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        assert self._delivered_ids(pipeline_mod.run(ctx, [sarah])) == [10, 11, 12, 13, 14]

    def test_pick_order_rating_puts_the_best_scored_first(self, ctx: EngineContext, mock_plextv):
        self._ordered_row_ctx(ctx, "rating")
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        assert ids == sorted(ids, key=lambda t: -(9.5 - (t - 10) * 0.5)), f"descending TMDB score, got {ids}"

    def test_pick_order_newest_puts_the_most_recent_release_first(self, ctx: EngineContext, mock_plextv):
        """Asserted as its own case, not just 'not the rating order': the two are deliberately
        opposite in this fixture, so a mix-up between them would otherwise pass one of the tests."""
        self._ordered_row_ctx(ctx, "newest")
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        assert ids == sorted(ids, reverse=True), f"newest release first, got {ids}"

    def test_pick_order_shuffle_is_stable_within_a_day_and_moves_between_days(self, ctx: EngineContext, mock_plextv):
        """Shuffle is a hash of (row, user, day), never `random`: a re-run the same night must
        reproduce the same row (or every retry rewrites the collection), and the next day must not."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._ordered_row_ctx(ctx, "shuffle", run_day=5)
        day5 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._ordered_row_ctx(ctx, "shuffle", run_day=5)
        day5_again = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._ordered_row_ctx(ctx, "shuffle", run_day=6)
        day6 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert day5 == day5_again, "a re-run on the same night reproduces the same order"
        assert day5 != day6, f"the order moves day to day, got {day5} both days"
        assert sorted(day5) == sorted(day6), "shuffling reorders the row, it never changes membership"

    def test_pick_order_shuffle_differs_between_two_users_on_the_same_day(self, ctx: EngineContext, mock_plextv):
        """Keyed on the user as well as the day, so two people's copies of one row don't shuffle in
        lockstep — otherwise the whole server shows the same 'random' order every night."""
        self._ordered_row_ctx(ctx, "shuffle")
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(101, "mike")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=101)])

        by_user = {
            u.slug: [p["tmdb_id"] for e in u.breakdown if e["library_title"] == "Movies" for p in e["picks"]]
            for u in report.users
        }
        assert by_user["sarah"] != by_user["mike"], f"per-user shuffle, got {by_user}"

    def test_pick_order_shuffle_reorders_a_frozen_row_without_rebuilding_it(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Shuffle on a row that never refreshes — the combination that makes the feature worth
        having, and the one that exercises delivery's unchanged-membership write-skip. Ordering is
        applied on the carry-forward path too, so the row moves without a single curator call; the
        deferred order pass then carries the new order to Plex."""
        prior = self._prior_movies([12, 13, 14, 15, 16])
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._ordered_row_ctx(ctx, "shuffle", run_day=5)
        ctx.config.rows[0] = replace(ctx.config.rows[0], refresh_days=0)  # 0.0 = never refresh
        ctx.previous_picks = {("sarah", "picked", "1"): prior}
        built = spy_build_picks(monkeypatch)
        day5 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        self._ordered_row_ctx(ctx, "shuffle", run_day=6)
        ctx.config.rows[0] = replace(ctx.config.rows[0], refresh_days=0)
        ctx.previous_picks = {("sarah", "picked", "1"): prior}
        day6 = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert built == [], "a frozen row still never rebuilds — ordering is presentation, not selection"
        assert sorted(day5) == sorted([12, 13, 14, 15, 16]), f"membership is exactly last run's, got {day5}"
        assert day5 != day6, f"the frozen row's ORDER still moves day to day, got {day5} both days"

    def test_pick_order_new_first_leads_with_the_titles_that_arrived_this_run(self, ctx: EngineContext, mock_plextv):
        """Issue #63's first ask. The prior row holds the pool's STRONGEST five, so the survivors are
        exactly what `best` would put in front — if this passed with the newcomers already sorting
        first, the order would be indistinguishable from the ranking and the test would prove nothing.

        On a refresh night the branch keeps 3 of 5 survivors (10, 11, 12) and swaps in the next two
        candidates (15, 16); `new_first` has to invert that.
        """
        self._ordered_row_ctx(ctx, "new_first")
        # `_ordered_row_ctx` rebuilds the RowSpec without a cadence, so it inherits the config's
        # 0.0 — "never refresh". This case is about the refresh branch, so ask for one.
        ctx.config.rows[0] = replace(ctx.config.rows[0], refresh_days=1)
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([10, 11, 12, 13, 14])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert ids == [15, 16, 10, 11, 12], f"newcomers first, survivors after, each in rank order — got {ids}"

    def test_pick_order_new_first_is_a_no_op_when_nothing_arrived(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """A carried-forward night has no newcomers, so the row must sit still rather than scramble.

        Without this, "new" defaulting to the whole row (or to none of it, sorted unstably) would
        reorder a row on nights nothing changed — the one thing the cadence exists to avoid, and a
        Plex write for no reason.
        """
        self._ordered_row_ctx(ctx, "new_first", run_day=5)
        ctx.config.rows[0] = replace(ctx.config.rows[0], refresh_days=0)  # 0.0 = never refresh
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert built == [], "a frozen row is redelivered, never rebuilt"
        assert ids == [12, 13, 14, 15, 16], f"nothing arrived, so nothing moves — got {ids}"

    def test_pick_order_rotate_advances_the_front_by_one_title_a_day(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Issue #63's second ask, and the property that makes it worth having: the front changes on a
        row that never rebuilds. Asserted against exact rotations, not just "day 5 != day 6", because
        the point is that the row stays in its ranking's relative order while the head advances — a
        shuffle would also pass an inequality check.

        Rotating rather than evicting is what keeps this in the display layer. Dropping the head
        instead would need a persisted position that `rank` (match quality) cannot carry without
        breaking `render_row_name` and `_seed_moved`.
        """
        prior = self._prior_movies([12, 13, 14, 15, 16])
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)
        seen = {}

        for day in (5, 6, 7):
            self._ordered_row_ctx(ctx, "rotate", run_day=day)
            ctx.config.rows[0] = replace(ctx.config.rows[0], refresh_days=0)
            ctx.previous_picks = {("sarah", "picked", "1"): prior}
            seen[day] = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert built == [], "the front moves without a rebuild — ordering is presentation, not selection"
        assert seen[5] == [12, 13, 14, 15, 16], f"day 5 (5 % 5 = 0) starts at the top, got {seen[5]}"
        assert seen[6] == [13, 14, 15, 16, 12], f"day 6 advances the front by one, got {seen[6]}"
        assert seen[7] == [14, 15, 16, 12, 13], f"day 7 advances it again, got {seen[7]}"

    def test_pick_order_rotate_reproduces_the_same_order_within_a_day(self, ctx: EngineContext, mock_plextv):
        """Same guarantee `shuffle` needs: a retry the same night must not rewrite the collection."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._ordered_row_ctx(ctx, "rotate", run_day=7)
        first = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._ordered_row_ctx(ctx, "rotate", run_day=7)
        second = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert first == second, f"a re-run on the same night reproduces the row, got {first} then {second}"

    def test_a_named_rows_title_is_the_same_whatever_order_it_is_displayed_in(self, ctx: EngineContext, mock_plextv):
        """The cell where display order and match quality could be confused: a `{top_seed}` row that
        also chooses its own order.

        The title renders from the BEST-MATCHING pick, never from whichever pick sorted first. A row
        is named after the watch it was built from, and that does not change because the owner asked
        for the titles in a different sequence. Reading `picks[0]` instead, this row renamed itself
        whenever the order put another seed's pick on top — and a shuffled one did so most nights,
        rewriting its title on Plex each time.

        Asserted as "all four agree" rather than against a hardcoded name, so the test states the
        invariant that matters and cannot be satisfied by one order happening to match a literal.
        """
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        titles = {}
        for pick_order in ("best", "rating", "newest", "shuffle"):
            self._two_seed_named_row_ctx(ctx, pick_order)
            report = pipeline_mod.run(ctx, [sarah])
            titles[pick_order] = [strip_marker(t) for _library, t in report.users[0].placement_titles]

        assert len({tuple(t) for t in titles.values()}) == 1, f"the order must not rename the row, got {titles}"
        # Tied back to the data rather than a literal name: whichever seed wins the ranking, the title
        # must be the one carried by the pick ranked #1 — that is what "named after its seed" means.
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        lead = min(picks, key=lambda p: p["rank"])
        assert titles["best"] == [f"Because you watched {lead['seed_title']}"], f"got {titles}, lead {lead}"

    def test_the_display_order_still_changes_which_pick_leads_the_row(self, ctx: EngineContext, mock_plextv):
        """The other half of the invariant above: the ORDER genuinely does change the delivered row,
        so 'the title never moves' is not passing merely because ordering did nothing here."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        self._two_seed_named_row_ctx(ctx, "best")
        best = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))
        self._two_seed_named_row_ctx(ctx, "rating")
        by_rating = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert best != by_rating, f"ordering changes the delivered row, got {best} vs {by_rating}"
        # The ranking interleaves seeds so each taste is represented; ordering by rating does not, so
        # the 9.5-rated (Chernobyl-seeded) picks group ahead of the 5.0-rated (Fargo-seeded) ones.
        assert by_rating[:3] == sorted(by_rating[:3]) and min(by_rating[:3]) >= 15, (
            f"the highly-rated picks lead as a block, got {by_rating}"
        )
        assert sorted(best) == sorted(by_rating), "ordering rearranges the row, it never changes membership"

    @pytest.mark.parametrize("pick_order", ["rating", "newest"])
    def test_rank_records_match_quality_not_the_delivered_position(self, pick_order, ctx: EngineContext, mock_plextv):
        """`rank` is stamped BEFORE the display order is applied, so for any order but "best" the
        delivered sequence and the ranks disagree.

        This is the guarantee the two `{top_seed}` bugs came from breaking. `rank` is what
        `render_row_name` names the row from and what `previous_picks` is ordered by — so if it were
        stamped after ordering, a shuffled row would rename itself nightly and `_seed_moved` would
        compare against an arbitrary pick and rebuild the row every refresh night.
        """
        self._two_seed_named_row_ctx(ctx, pick_order)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ranks = [p["rank"] for p in picks]
        assert sorted(ranks) == list(range(1, len(picks) + 1)), f"every pick keeps a distinct rank, got {ranks}"
        assert ranks != sorted(ranks), (
            f"{pick_order} reorders the row, so rank must NOT follow the delivered position — got {ranks}"
        )

    @pytest.mark.parametrize("pick_order", ["rating", "newest", "shuffle"])
    def test_a_named_row_carries_forward_whatever_order_it_is_displayed_in(
        self, pick_order, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """`_seed_moved` compares against the best-matching prior pick, which `previous_picks` returns
        first because it is ordered by the persisted rank column. Comparing against the DISPLAYED
        first pick instead made this row look reseeded every refresh night, so it rebuilt for ever and
        carry-forward silently stopped applying to every non-default order."""
        self._ordered_row_ctx(ctx, pick_order)
        ctx.config.rows[0] = replace(ctx.config.rows[0], name_template="Because you watched {top_seed}", max_seeds=1)
        # Seeded by 900 ("Fargo"), which is still this person's only seed — so the seed has NOT moved.
        ctx.previous_picks = {
            ("sarah", "picked", "1"): self._prior_seeded_by([12, 13, 14, 15, 16], seed_tmdb_id=900, seed_title="Fargo")
        }
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        ids = self._delivered_ids(pipeline_mod.run(ctx, [sarah]))

        assert {12, 13, 14} <= set(ids), f"{pick_order} row still carries its strongest two-thirds, got {ids}"

    def test_pick_order_sorts_picks_missing_the_value_last(self, ctx: EngineContext, mock_plextv):
        """Carried-forward picks delivered before 0056 have no rating or year. They must sort last
        and keep their order, so such a row degrades to its ranking for one cycle rather than
        scrambling — and the run must not raise on the None."""
        from shortlist.engine.rows import _apply_order

        picks = [
            Pick(tmdb_id=1, rating_key=0, title="no data A", rank=1, reason="", media_type=MediaType.MOVIE),
            Pick(tmdb_id=2, rating_key=0, title="rated", rank=2, reason="", media_type=MediaType.MOVIE, rating=8.0),
            Pick(tmdb_id=3, rating_key=0, title="no data B", rank=3, reason="", media_type=MediaType.MOVIE),
        ]

        by_rating = _apply_order(picks, "rating", row_slug="r", user_slug="u", run_day=5)
        by_year = _apply_order(picks, "newest", row_slug="r", user_slug="u", run_day=5)

        assert [p.tmdb_id for p in by_rating] == [2, 1, 3], "the rated pick leads; the rest keep their order"
        assert [p.tmdb_id for p in by_year] == [1, 2, 3], "no years at all leaves the order untouched"
