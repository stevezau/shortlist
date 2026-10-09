"""Per-row overrides, settings changes, request wiring and when a row was built."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import (
    row_marker,
    strip_marker,
)
from shortlist.engine.models import (
    MediaType,
    Pick,
    PosterSpec,
    RowOverride,
    RowSpec,
)
from tests.conftest import NOW, fake_media_item, make_profile, make_watched, plextv_user
from tests.unit.pipeline_support import (
    _ranked,
    ctx,  # noqa: F401
    spy_build_picks,
)


class TestPerRowOverrides:
    """A per-user override can mute or resize one row without touching it for others."""

    def test_picks_are_tagged_with_their_row_slug(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = report.users[0].picks
        assert picks and all(p.collection_slug == "picked" for p in picks)  # the default row's slug
        # Each pick also carries the library it was delivered into, so the report can split a
        # multi-library row per library. section_key is the Plex key; library its display name.
        assert all(p.section_key and p.library for p in picks)

    def test_muting_the_only_row_delivers_nothing(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(muted=True)})
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].picks == []
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()

    def test_per_row_size_override_wins(self, ctx: EngineContext, mock_plextv):
        # The fixture pool has 2 candidates; an override of size 1 must cap this user's row at 1.
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(size=1)})
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert len(report.users[0].picks) == 1

    def test_per_user_recent_count_override_reaches_the_gather(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # recent_count caps how many recent watches the llm_web source searches. A per-user override
        # must reach the gather as its resolved recent_count — beating the row's own value AND the
        # global default. (That the gather then slices seeds[:recent_count] is test_candidates' job.)
        from shortlist.engine import candidates as candidates_mod

        seen: list[int] = []
        real_gather = candidates_mod.gather_candidates

        def spy_gather(*args, **kwargs):
            seen.append(kwargs["recent_count"])
            return real_gather(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod.rows.candidates_mod, "gather_candidates", spy_gather)
        ctx.config.recent_count = 10  # global default
        # The row sets its own recent_count too, so seen==[3] proves the user override beats BOTH the
        # row's value (8) and the global default (10) — not just the global.
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, candidate_sources=["llm_web"], recent_count=8)
        ]
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(recent_count=3)})
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        assert seen == [3]  # the person's override, beating the row's 8 and the global 10

    def test_per_row_max_seeds_caps_the_seeds_the_row_is_built_from(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # max_seeds decides how many watched titles a row is derived from — what EVERY source searches
        # from, not just the web one. A row's own value must beat the global (issue #57: a
        # `{top_seed}` row named after one watch was still built from thirty).
        seen: list[int] = []
        real_derive = pipeline_mod.rows.derive_seeds

        def spy_derive(*args, **kwargs):
            seen.append(kwargs["max_seeds"])
            return real_derive(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod.rows, "derive_seeds", spy_derive)
        ctx.config.max_seeds = 10  # the global budget this row must override
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, max_seeds=2)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert seen == [2]

    def test_two_rows_differing_only_in_max_seeds_do_not_share_seeds(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        # Both rows target the same media and libraries, so they hit the same memo key on every
        # other axis. If max_seeds were left out of that key the second row would silently reuse the
        # first row's seed set — and its own setting would do nothing at all.
        seen: list[int] = []
        real_derive = pipeline_mod.rows.derive_seeds

        def spy_derive(*args, **kwargs):
            seen.append(kwargs["max_seeds"])
            return real_derive(*args, **kwargs)

        monkeypatch.setattr(pipeline_mod.rows, "derive_seeds", spy_derive)
        ctx.config.max_seeds = 10
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, max_seeds=1),
            RowSpec(slug="deep", name_template="Deep", size=5, max_seeds=4),
            RowSpec(slug="default", name_template="Default", size=5),  # inherits the global 10
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert sorted(seen) == [1, 4, 10]

    def test_rows_with_different_max_seeds_gather_separately(self, ctx: EngineContext, mock_plextv):
        # The OUTCOME test, not a spy: the two above pass even with `max_seeds` removed from
        # `pool_key`, because the up-front `counts.seeds` loop calls seeds_for for every spec whatever
        # the pools then do. Without that key entry the rows SHARE one pool — whichever reaches
        # pools_for first builds it — and the second row's budget is silently inert. Which is issue
        # #57 shipping "fixed" and not fixed.
        ctx.history_source.fetch.return_value = [
            make_watched(f"Film{i}", days_ago=i + 1, rating_key=999) for i in range(5)
        ]
        ctx.config.max_seeds = 10
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, max_seeds=4),
            RowSpec(slug="because", name_template="Because {top_seed}", size=5, max_seeds=1),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        # One suggestions() call per seed: 4 + 1 across two pools. A shared pool would be 4.
        assert ctx.tmdb.suggestions.call_count == 5

    def test_a_rewatch_row_keeps_finished_titles_that_a_normal_row_drops(self, ctx: EngineContext, mock_plextv):
        """The OUTCOME test for `excludes_finished` in `pool_key`.

        A normal row at watched_pct 0 has finished titles removed from its POOL; a rewatch row must
        keep them. If the two rows shared one pool — whichever built it first would win — the rewatch
        row could never deliver a rewatch, and the flag would look implemented while doing nothing.
        """
        # Seeds come from the default Fargo watches (tmdb 900, via the library index). Candidate 10 is
        # ALSO something they have watched, but is not a seed — seeds are excluded from every pool, so a
        # title cannot be both the seed and the rewatch under test.
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            make_watched("Candidate Ten", days_ago=6, tmdb_id=10),
        ]
        # max_seeds 1: seeds are excluded from every pool, so the watched title under test must NOT be
        # one. Only the most recent watch (the Fargo/Seed row) seeds; the older one stays a candidate.
        ctx.config.max_seeds = 1
        ctx.config.rows = [
            RowSpec(slug="fresh", name_template="Fresh", size=2),
            RowSpec(slug="again", name_template="Again", size=2, rewatch=True),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        by_row: dict[str, set[int]] = {}
        for pick in report.users[0].picks:
            by_row.setdefault(pick.collection_slug, set()).add(pick.tmdb_id)
        assert by_row, "the run produced no picks at all — the test fixture, not the feature"
        assert 10 not in by_row.get("fresh", set()), "a normal row must not deliver a finished title"
        assert 10 in by_row.get("again", set()), "the rewatch row must be able to deliver one"

    def test_a_rewatch_row_leads_with_the_rewatch(self, ctx: EngineContext, mock_plextv):
        """Not just present — FIRST. `watched_pct` alone could admit it at the bottom of the row."""
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            # 20 is the lower-rated candidate, so ranking puts it AFTER 10 only if rewatch reorders.
            make_watched("Candidate Twenty", days_ago=6, tmdb_id=20),
        ]
        # max_seeds 1: seeds are excluded from every pool, so the watched title under test must NOT be
        # one. Only the most recent watch (the Fargo/Seed row) seeds; the older one stays a candidate.
        ctx.config.max_seeds = 1
        ctx.config.rows = [RowSpec(slug="again", name_template="Again", size=2, rewatch=True)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        assert delivered[0] == 20, f"the already-watched title must lead the row, got {delivered}"

    def test_an_unstarted_only_row_drops_a_barely_started_show(self, ctx: EngineContext, mock_plextv):
        """Stricter than the CAP, and its own pool.

        Since 1.2 a 0% row already drops started shows, so the contrast is no longer against a
        default row — it is against a row that PERMITS watched titles (`watched_pct > 0`). There, a
        show 1 episode into 40 is fair game; an "unstarted only" row must still refuse it, which is
        the whole claim of "a series to start".
        """
        show_section = MagicMock()
        show_section.type = "show"
        show_section.title = "TV Shows"
        show_section.collections.return_value = []
        ctx.plex.sections.return_value = [show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.SHOW: show_section}
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030, 40: 1040}
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 30, "name": "Started Show", "genre_ids": [], "vote_average": 8.0},
                {"id": 40, "name": "Never Opened", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        # The seed show (900) plus show 30 at ONE episode of forty: started, nowhere near finished.
        ctx.history_source.fetch.return_value = [
            *[
                make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
                for i in range(1, 5)
            ],
            make_watched(
                "Started Show",
                days_ago=6,
                media_type=MediaType.SHOW,
                tmdb_id=30,
                viewed_leaf_count=1,
                leaf_count=40,
            ),
        ]
        # max_seeds 1: seeds are excluded from every pool, so the watched title under test must NOT be
        # one. Only the most recent watch (the Fargo/Seed row) seeds; the older one stays a candidate.
        ctx.config.max_seeds = 1
        ctx.config.rows = [
            # watched_pct 1.0: this row permits watched titles, so it is the one that still offers a
            # part-watched show. At the 0% default it would now drop it too — see
            # `test_a_zero_pct_row_drops_a_barely_started_show`.
            RowSpec(slug="anything", name_template="Anything", size=2, media=MediaType.SHOW, watched_pct=1.0),
            RowSpec(slug="tostart", name_template="To start", size=2, media=MediaType.SHOW, unstarted_only=True),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        by_row: dict[str, set[int]] = {}
        for pick in report.users[0].picks:
            by_row.setdefault(pick.collection_slug, set()).add(pick.tmdb_id)
        assert by_row, "the run produced no picks at all — the test fixture, not the feature"
        assert 30 in by_row.get("anything", set()), "a part-watched show is fair game for a row that allows watched"
        assert 30 not in by_row.get("tostart", set()), "a started series must never reach an unstarted row"
        assert 40 in by_row.get("tostart", set()), "the never-opened one is exactly what it wants"

    def test_a_zero_pct_row_drops_a_barely_started_show(self, ctx: EngineContext, mock_plextv):
        """The Teacup fix, end to end through a real run.

        Reported 2026-08-04: a show the person had started kept appearing in a row set to 0%
        already-watched. It was doing what it was told — until 1.2, "already-watched" for a SHOW meant
        finished (>=80%, or a length-scaled floor of ~3 episodes), so one episode in was, to the row,
        a fresh discovery. Plex disagrees: its own watched filter returns a show from episode one.
        """
        show_section = MagicMock()
        show_section.type = "show"
        show_section.title = "TV Shows"
        show_section.collections.return_value = []
        ctx.plex.sections.return_value = [show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.SHOW: show_section}
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030, 40: 1040}
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 30, "name": "Teacup", "genre_ids": [], "vote_average": 8.0},
                {"id": 40, "name": "Never Opened", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        ctx.history_source.fetch.return_value = [
            *[
                make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
                for i in range(1, 5)
            ],
            # 2 of 8 — under the old 3-episode floor, and the exact shape of the report.
            make_watched(
                "Teacup", days_ago=6, media_type=MediaType.SHOW, tmdb_id=30, viewed_leaf_count=2, leaf_count=8
            ),
        ]
        ctx.config.max_seeds = 1
        ctx.config.watched_pct = 0.0
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=2, media=MediaType.SHOW)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = {pick.tmdb_id for pick in report.users[0].picks}
        assert 30 not in delivered, "a show they have started must not reach a 0% row"
        assert 40 in delivered, "the unwatched one still should — the rule must not empty the row"

    def test_a_rewatch_row_shares_the_pool_of_a_zero_pct_row(self, ctx: EngineContext, mock_plextv):
        """The OTHER direction of `excludes_watched`, which no membership assertion can catch.

        A rewatch row takes its finished titles from history (#114), so all it wants from the pool is
        the unseen top-up — exactly a 0% row's pool, so the two must share ONE gather. It used to share
        with the >0 rows instead, back when it drew finished titles from the pool itself. Without this,
        `excludes_watched` could regress to keying on the raw percentage — splitting the pool and paying
        a second time for every rate-limited/LLM source — and every other test still passes.
        """
        ctx.config.max_seeds = 1
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]
        ctx.config.rows = [
            RowSpec(slug="again", name_template="Again", size=2, rewatch=True, watched_pct=1.0),
            RowSpec(slug="fresh", name_template="Fresh", size=2, watched_pct=0.0),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        # One suggestions() call per seed per pool. One seed, one shared pool = exactly one call.
        assert ctx.tmdb.suggestions.call_count == 1, "a rewatch row and a 0% row must share one pool"

    def test_a_zero_pct_row_and_an_unstarted_only_row_share_one_pool(self, ctx: EngineContext, mock_plextv):
        """Found by architecture review 2026-08-05, and invisible to any membership assertion.

        Since 1.2 a 0% row's exclusion set already unions in the started shows, so a 0% row and a
        0% + `unstarted_only` row exclude byte-identical sets. `pool_key` still split them, which
        bought a second full TMDB/LLM gather per person per night for no difference in candidates —
        on the commonest pairing, now that the toggle is reachable on "films and shows" rows.

        The reverse must still split, which `test_an_unstarted_only_row_drops_a_barely_started_show`
        covers: a row that PERMITS watched titles genuinely differs from an unstarted-only one.
        """
        show_section = MagicMock()
        show_section.type = "show"
        show_section.title = "TV Shows"
        show_section.collections.return_value = []
        ctx.plex.sections.return_value = [show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.SHOW: show_section}
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030}
        ctx.config.max_seeds = 1
        ctx.config.watched_pct = 0.0
        ctx.history_source.fetch.return_value = [
            make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
            for i in range(1, 5)
        ]
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="Picked", size=2, media=MediaType.SHOW),
            RowSpec(slug="tostart", name_template="To start", size=2, media=MediaType.SHOW, unstarted_only=True),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        # One suggestions() call per seed per pool. One seed, one shared pool = exactly one call.
        assert ctx.tmdb.suggestions.call_count == 1, "a 0% row and a 0% unstarted-only row must share one pool"

    def test_rewatch_works_for_shows_where_finished_is_a_different_predicate(self, ctx: EngineContext, mock_plextv):
        """For movies "finished" is any watch; for shows it is the `watched_show_pct` fraction plus a
        length-scaled floor (`_watched_titles`) — a different predicate, so a different cell."""
        show_section = MagicMock()
        show_section.type = "show"
        show_section.title = "TV Shows"
        show_section.collections.return_value = []
        ctx.plex.sections.return_value = [show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.SHOW: show_section}
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030, 40: 1040}
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 30, "name": "Finished Show", "genre_ids": [], "vote_average": 6.0},
                {"id": 40, "name": "Never Opened", "genre_ids": [], "vote_average": 9.0},
            ]
        )
        ctx.config.max_seeds = 1
        ctx.history_source.fetch.return_value = [
            *[
                make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
                for i in range(1, 5)
            ],
            # 10 of 10 episodes: finished by any measure, so it belongs in a rewatch row.
            make_watched(
                "Finished Show",
                days_ago=6,
                media_type=MediaType.SHOW,
                tmdb_id=30,
                viewed_leaf_count=10,
                leaf_count=10,
            ),
        ]
        ctx.config.rows = [
            RowSpec(slug="again", name_template="Again", size=2, media=MediaType.SHOW, rewatch=True),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = [p.tmdb_id for p in sorted(report.users[0].picks, key=lambda p: p.rank)]
        # 40 is rated higher, so only the rewatch preference can put the finished show first.
        assert delivered and delivered[0] == 30, f"the finished SHOW must lead the row, got {delivered}"

    def test_unstarted_only_applies_on_a_both_media_row_not_just_a_shows_row(self, ctx: EngineContext, mock_plextv):
        """`media="both"` is its own cell: the filter must not be gated on the row being shows-only.

        Movie immunity is asserted at the unit level instead (`TestStartedShows` — `_started_shows`
        yields only SHOW keys, so nothing it returns can match a movie candidate). Doing it here would
        need a second seed of the other type, because candidates inherit their SEED's media type — so
        a movie-seeded gather types even a TV title as a movie and the test would pass for the wrong
        reason.
        """
        show_section, movie_section = MagicMock(), MagicMock()
        show_section.type, show_section.title = "show", "TV Shows"
        movie_section.type, movie_section.title = "movie", "Movies"
        for sec in (show_section, movie_section):
            sec.collections.return_value = []
        ctx.plex.sections.return_value = [movie_section, show_section]
        ctx.plex.sections_by_type.return_value = {
            MediaType.MOVIE: movie_section,
            MediaType.SHOW: show_section,
        }
        ctx.plex.build_library_index.return_value = {900: 999, 30: 1030, 40: 1040}
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 30, "name": "Started Show", "genre_ids": [], "vote_average": 9.0},
                {"id": 40, "name": "Never Opened", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        ctx.config.max_seeds = 1
        ctx.history_source.fetch.return_value = [
            *[
                make_watched("Seed Show", days_ago=i, rating_key=999, media_type=MediaType.SHOW, leaf_count=10)
                for i in range(1, 5)
            ],
            make_watched(
                "Started Show", days_ago=6, media_type=MediaType.SHOW, tmdb_id=30, viewed_leaf_count=1, leaf_count=40
            ),
        ]
        # media defaults to "both" — deliberately NOT narrowed to shows.
        ctx.config.rows = [RowSpec(slug="mixed", name_template="Mixed", size=3, unstarted_only=True)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        delivered = {p.tmdb_id for p in report.users[0].picks}
        assert delivered, "no picks at all — the fixture, not the feature"
        assert 30 not in delivered, "the started series must be excluded on a both-media row too"
        assert 40 in delivered

    def test_pools_that_differ_only_in_seed_count_are_labelled_apart(self, ctx: EngineContext, mock_plextv):
        # The trace labels a gather by media + sources. Two rows differing only in max_seeds share
        # both, so without the seed count they record under two IDENTICAL names and the trace cannot
        # say which gather belonged to which row. (The "How we picked" page doesn't render the label
        # today — it merges a library's gathers into one source list — so this is about the stored
        # record, not the screen.) The media prefix must stay first: `poolCoversMedia` splits on
        # " · " to place a gather in a library.
        ctx.history_source.fetch.return_value = [
            make_watched(f"Film{i}", days_ago=i + 1, rating_key=999) for i in range(5)
        ]
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, max_seeds=4),
            RowSpec(slug="because", name_template="Because {top_seed}", size=5, max_seeds=1),
        ]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        labels = [g["pool"] for g in report.users[0].trace["gathers"]]
        assert len(set(labels)) == 2, labels
        assert all(lbl.startswith("movie · ") or lbl.startswith("both · ") for lbl in labels), labels
        assert any("1 seed" in lbl for lbl in labels) and any("4 seeds" in lbl for lbl in labels), labels

    def test_a_single_pool_is_not_labelled_with_a_seed_count(self, ctx: EngineContext, mock_plextv):
        # The count is noise when nothing differs — every row inheriting the default is the common
        # case, and its trace should read exactly as it did before per-row budgets existed.
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5)]
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert all("seed" not in g["pool"] for g in report.users[0].trace["gathers"])

    def test_per_row_candidate_sources_gate_which_apis_run(self, ctx: EngineContext, mock_plextv):
        # A row pinned to tmdb_discover only must query discover and NOT the tmdb_similar endpoint —
        # per-row sources override the global set for that row.
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, candidate_sources=["tmdb_discover"])]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 20, "title": "Discovered", "genre_ids": [18], "vote_average": 8.5}
        ]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        assert ctx.tmdb.discover.called  # the row's own source ran
        assert not ctx.tmdb.suggestions.called  # tmdb_similar was NOT in this row's sources

    def test_same_sources_in_different_order_share_one_pool(self, ctx: EngineContext, mock_plextv):
        # Two rows list the same sources in a different order. gather is set-based, so they must
        # reuse ONE pool (keyed on the sorted set) — not rebuild it, re-hitting the source APIs.
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5, candidate_sources=["tmdb_similar", "tmdb_discover"]),
            RowSpec(slug="gems", name_template="Gems", size=5, candidate_sources=["tmdb_discover", "tmdb_similar"]),
        ]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: []

        pipeline_mod.run(ctx, [sarah])

        # One seed, one shared pool -> tmdb_similar queried once, not once per row.
        assert ctx.tmdb.suggestions.call_count == 1

    def test_row_pinned_to_a_non_lowest_key_library_is_delivered_and_promoted_there(
        self, ctx: EngineContext, mock_plextv
    ):
        # Regression: promotion is the only thing that GUARANTEES a collection is hidden from LIBRARY BROWSE
        # (share filters only cover Home/Recommended/Related), so a row delivered to a library that
        # isn't the lowest-key one of its type must still be promoted there — or it leaks into browse.
        lib1 = MagicMock()
        lib1.type = "movie"
        lib1.key = "1"
        lib1.title = "Movies"
        lib2 = MagicMock()
        lib2.type = "movie"
        lib2.key = "2"  # the SECOND movie library — never returned by sections_by_type()
        lib2.title = "4K Movies"
        ctx.plex.sections.return_value = [lib1, lib2]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: lib1}  # lowest-key only
        ctx.plex.build_library_index.side_effect = lambda s: (
            {900: 999, 10: 1010, 20: 1020} if s is lib1 else {900: 999, 10: 2010, 20: 2020}
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, library_keys=["2"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        made: list[MagicMock] = []

        def create_collection(section, title, items):
            c = MagicMock()
            c._section = section
            made.append(c)
            return c

        ctx.plex.create_collection.side_effect = create_collection
        ctx.plex.find_owned_collections.side_effect = lambda section, label: [c for c in made if c._section is section]

        pipeline_mod.run(ctx, [sarah])

        # Delivered into lib2 with lib2's ratingKeys (not lib1's 10xx), and PROMOTED there.
        assert ctx.plex.create_collection.call_args.args[0] is lib2
        assert ctx.plex.fetch_items.call_args.args[0] == [2010, 2020]
        promoted_sections = {getattr(call.args[0], "_section", None) for call in ctx.plex.promote.call_args_list}
        assert lib2 in promoted_sections, "the row in the non-lowest-key library was never promoted (leak)"

    def test_a_pinned_row_only_recommends_titles_its_own_library_holds(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """A row pinned to a library was selected against the UNION of every library of its type, and
        delivery then dropped every pick the pinned library didn't hold — a short row, or an empty
        one, reported as ok. The pool must be narrowed to the row's own libraries first."""
        lib1 = MagicMock()
        lib1.type = "movie"
        lib1.key = "1"
        lib1.title = "Movies"
        lib2 = MagicMock()
        lib2.type = "movie"
        lib2.key = "2"
        lib2.title = "4K Movies"
        ctx.plex.sections.return_value = [lib1, lib2]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: lib1}
        # Candidate 10 is in BOTH libraries; candidate 20 lives only in lib1.
        ctx.plex.build_library_index.side_effect = lambda s: (
            {900: 999, 10: 1010, 20: 1020} if s is lib1 else {900: 999, 10: 2010}
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, library_keys=["2"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        offered = spy_build_picks(monkeypatch)

        pipeline_mod.run(ctx, [sarah])

        # 20 isn't in lib2, so the pick builder must never have been offered it.
        offered_ids = {c.tmdb_id for call in offered for c in call}
        assert 10 in offered_ids
        assert 20 not in offered_ids, "the row was offered a title its own library doesn't hold"

    def test_a_shows_only_row_survives_a_movie_heavy_pool(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """The media filter used to run AFTER the pre-rank truncation, so a movie-heavy watcher's
        shows-only row could lose every show to the 40-candidate cut and deliver nothing."""
        movie_section = MagicMock()
        movie_section.type = "movie"
        movie_section.key = "1"
        movie_section.title = "Movies"
        show_section = MagicMock()
        show_section.type = "show"
        show_section.key = "2"
        show_section.title = "TV Shows"
        ctx.plex.sections.return_value = [movie_section, show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section, MediaType.SHOW: show_section}
        ctx.config.candidates_pre_rank = 5  # a tiny cut, so crowding-out is easy to trigger
        movies = {900: 999, **{i: 1000 + i for i in range(1, 60)}}
        shows = {5000: 5999, 5001: 5001}
        ctx.plex.build_library_index.side_effect = lambda s: movies if s is movie_section else shows
        ctx.config.rows = [RowSpec(slug="tv", name_template="TV Picks", size=2, media="show")]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        # 59 high-rated movies flood the pool and ONE lower-rated show — from the SAME source, so a
        # source quota can't rescue it. Only filtering by media BEFORE the cut can.
        def suggestions(tid, mt):  # returns (item, affinity) pairs
            if mt is MediaType.MOVIE:
                return _ranked(
                    [{"id": i, "title": f"Movie {i}", "genre_ids": [], "vote_average": 9.0} for i in range(1, 60)]
                )
            return _ranked([{"id": 5001, "title": "A Show", "genre_ids": [], "vote_average": 6.0}])

        ctx.tmdb.suggestions.side_effect = suggestions
        ctx.config.candidate_sources = ["tmdb_similar"]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        # A show seed so the SHOW media type is in play at all (typed as a SHOW, or no show seed is
        # derived and tmdb_discover is never asked for shows).
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            make_watched("Breaking Bad", days_ago=2, rating_key=5999, media_type=MediaType.SHOW),
        ]
        offered = spy_build_picks(monkeypatch)

        pipeline_mod.run(ctx, [sarah])

        offered_ids = [c.tmdb_id for call in offered for c in call]
        assert offered_ids, "the shows-only row was offered no candidates at all"
        assert all(i >= 5000 for i in offered_ids), f"a shows-only row was offered movies: {offered_ids}"

    def test_one_rows_dead_source_does_not_kill_the_users_other_rows(self, ctx: EngineContext, mock_plextv):
        """A row pinned to a single source (Trakt-only) whose source is down must fail alone. It used
        to raise out of the whole user, so their healthy rows delivered nothing either."""
        trakt = MagicMock()
        trakt.related.side_effect = RuntimeError("trakt 502")
        ctx.trakt = trakt
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5),  # inherits the (working) global sources
            RowSpec(slug="next", name_template="What to watch next", size=5, candidate_sources=["trakt"]),
        ]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].status == "ok", "a healthy row's user must not be failed by a dead sibling"
        assert {p.collection_slug for p in report.users[0].picks} == {"picked"}

    def test_a_user_whose_every_source_is_down_is_an_error_not_a_cheerful_ok(self, ctx: EngineContext, mock_plextv):
        """The other half: if nothing worked, we know nothing about this person — reporting ok would
        leave yesterday's row in place and call it a success."""
        ctx.tmdb.suggestions.side_effect = RuntimeError("tmdb 429")
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, candidate_sources=["tmdb_similar"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].status == "error"
        assert "429" in report.users[0].error

    def test_disabling_every_row_delivers_nothing(self, ctx: EngineContext, mock_plextv):
        """When the server manages rows (rows_defined=True), an empty row list means every row is
        DISABLED — deliver nothing. It used to resurrect the synthesized default for everyone."""
        ctx.config.rows = []
        ctx.config.rows_defined = True
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()

    def test_an_unconfigured_run_still_gets_a_default_row(self, ctx: EngineContext, mock_plextv):
        """A caller that doesn't manage rows (rows_defined=False) passing an empty list means
        'unconfigured' — synthesize the legacy default so a bare engine run still builds a row."""
        ctx.config.rows = []
        ctx.config.rows_defined = False
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        pipeline_mod.run(ctx, [sarah])

        ctx.plex.create_collection.assert_called_once()
        section, title, items = ctx.plex.create_collection.call_args.args
        assert section.type == "movie"  # the only library this ctx fixture configures
        assert title == "✨ Movies Picked for You" + row_marker(100)  # the synthesized default row's title
        assert len(items) == 2  # both mocked TMDB candidates (Candidate Ten, Candidate Twenty) — not empty, not more

    def test_a_both_row_fills_each_library_to_its_own_size(self, ctx: EngineContext, mock_plextv):
        """A 'both' row delivers a movie collection AND a show collection, and each library fills to
        its own size. One shared budget split by what the curator picked left a mostly-TV watcher with
        a full show row and a one-item movie row."""
        movie_section = MagicMock()
        movie_section.type = "movie"
        movie_section.key = "1"
        movie_section.title = "Movies"
        show_section = MagicMock()
        show_section.type = "show"
        show_section.key = "2"
        show_section.title = "TV Shows"
        ctx.plex.sections.return_value = [movie_section, show_section]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section, MediaType.SHOW: show_section}
        movies = {900: 999, **{i: 1000 + i for i in range(1, 40)}}
        shows = {5000: 5999, **{5000 + i: 6000 + i for i in range(1, 40)}}
        ctx.plex.build_library_index.side_effect = lambda sec: movies if sec is movie_section else shows

        def suggestions(tid, mt):  # returns (item, affinity) pairs
            # Plenty of BOTH movie and show candidates in the pool.
            base = 1 if mt is MediaType.MOVIE else 5000
            return _ranked(
                [{"id": base + i, "title": f"T{base + i}", "genre_ids": [], "vote_average": 8.0} for i in range(1, 40)]
            )

        ctx.tmdb.suggestions.side_effect = suggestions
        # A watcher of one movie + one show, so both media types seed.
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),
            make_watched("Breaking Bad", days_ago=2, rating_key=5999, media_type=MediaType.SHOW),
        ]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=10, media="both")]
        ctx.config.min_history = 1  # 2 watches is enough here — exercise the real curate path, not cold start
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        picks = report.users[0].picks
        movie_picks = [p for p in picks if p.media_type is MediaType.MOVIE]
        show_picks = [p for p in picks if p.media_type is MediaType.SHOW]
        assert len(movie_picks) == 10, f"movie row should fill to 10, got {len(movie_picks)}"
        assert len(show_picks) == 10, f"show row should fill to 10, got {len(show_picks)}"

    def test_a_row_builds_each_library_from_that_librarys_own_contents(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        """Two libraries of the SAME media type each get their OWN full row, built only from the
        titles that library holds — not one recommendation split between them. This is what makes a
        row 'per library': a server with a Movies and a 4K library fills both, from their own shelves.
        """
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        # Disjoint catalogues: Movies holds tmdb 10-15, 4K holds tmdb 50-55 (seed 900 in both).
        idx_std = {900: 999, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec: idx_std if sec is movies else idx_4k
        # The candidate pool spans BOTH libraries' titles; each library must pick only its own.
        pool = [
            {"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in [*range(10, 16), *range(50, 56)]
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50  # keep the whole 12-title pool; don't truncate either library
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        offered = spy_build_picks(monkeypatch)

        pipeline_mod.run(ctx, [sarah])

        # One build_picks call per library, each seeing ONLY that library's tmdb ids.
        seen = [{c.tmdb_id for c in call} for call in offered]
        assert {10, 11, 12, 13, 14, 15} in seen, f"Movies library should build from its own ids, saw {seen}"
        assert {50, 51, 52, 53, 54, 55} in seen, f"4K library should build from its own ids, saw {seen}"

    def test_run_records_a_breakdown_entry_per_library(self, ctx: EngineContext, mock_plextv):
        """The per-user report carries a per-(row, library) breakdown so the UI can show 'added X to
        Movies, Y to TV' with each library's own picks — not one merged list."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        idx_std = {900: 999, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec: idx_std if sec is movies else idx_4k
        pool = [
            {"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in [*range(10, 16), *range(50, 56)]
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        breakdown = report.users[0].breakdown
        by_library = {e["library_title"]: e for e in breakdown}
        assert set(by_library) == {"Movies", "4K Movies"}, f"one entry per library, got {list(by_library)}"
        for entry in breakdown:
            assert entry["row_slug"] == "picked"
            assert len(entry["picks"]) == 5, "each library's row has its own full set of picks"
            assert [p["rank"] for p in entry["picks"]] == [1, 2, 3, 4, 5], "picks ranked 1..k within the library"

    def test_the_run_log_says_what_each_library_is_about_to_get(self, ctx: EngineContext, mock_plextv):
        """Delivery narrates each library's pending change under the person, before the write — an
        in-place update on a big library runs for minutes, and "writing the row to Plex" alone could
        not say whether it was creating a row or swapping titles in one."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 1000 + i for i in range(10, 16)}}
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in range(10, 16)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="Picked", size=5, media="movie")]
        ctx.config.min_history = 1
        mock_plextv.users = [plextv_user(100, "sarah")]
        emitted: list[tuple[str, str, dict]] = []
        ctx.progress = lambda slug, stage, counts, reason=None: emitted.append((slug, stage, counts))

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        writes = [(slug, counts) for slug, stage, counts in emitted if stage == "delivering" and "library" in counts]
        assert writes == [("sarah", {"row": "Picked", "library": "Movies", "creating": 5})]

    def _movie_row_ctx(self, ctx, refresh_days, run_day):
        """A single Movies library holding tmdb 10-19, one 'picked' movie row at the given cadence."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        idx = {900: 999, **{i: 1000 + i for i in range(10, 20)}}
        ctx.plex.build_library_index.return_value = idx
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in range(10, 20)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie", refresh_days=refresh_days)]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        ctx.run_day = run_day  # a real day; 0 is the tests/direct "always refresh" sentinel

    def _prior_movies(self, tmdb_ids):
        return [
            Pick(
                tmdb_id=t,
                rating_key=0,
                title=f"T{t}",
                rank=i + 1,
                reason="kept",
                media_type=MediaType.MOVIE,
                collection_slug="picked",
                section_key="1",
                library="Movies",
            )
            for i, t in enumerate(tmdb_ids)
        ]

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

    def _prior_seeded_by(self, tmdb_ids, *, seed_tmdb_id: int, seed_title: str):
        return [replace(p, seed_tmdb_id=seed_tmdb_id, seed_title=seed_title) for p in self._prior_movies(tmdb_ids)]

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

    def _two_seed_named_row_ctx(self, ctx, pick_order: str, *, run_day: int = 5):
        """A `{top_seed}` row seeded by TWO watches, whose candidates sort differently by each order.

        One seed is required per distinct `seed_title` in the row — with a single seed every pick
        carries the same one and NO ordering could ever change the rendered title, which is exactly
        what made an earlier version of this test pass against the bug it was written to catch.

        Fargo's candidates are old and poorly rated; Chernobyl's are new and highly rated. So "rating"
        and "newest" both put a Chernobyl-seeded pick first, while the ranking does not.
        """
        self._movie_row_ctx(ctx, refresh_days=1, run_day=run_day)
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),
            make_watched("Chernobyl", days_ago=2, rating_key=998),
        ]
        ctx.plex.build_library_index.return_value = {900: 999, 555: 998, **{i: 1000 + i for i in range(10, 20)}}
        by_seed = {
            900: [
                {"id": i, "title": f"F{i}", "genre_ids": [], "vote_average": 5.0, "release_date": "1996-01-01"}
                for i in range(10, 15)
            ],
            555: [
                {"id": i, "title": f"C{i}", "genre_ids": [], "vote_average": 9.5, "release_date": "2024-01-01"}
                for i in range(15, 20)
            ],
        }
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(by_seed.get(tid, []))
        ctx.config.rows = [
            RowSpec(
                slug="picked",
                name_template="Because you watched {top_seed}",
                size=5,
                media="movie",
                refresh_days=1,
                pick_order=pick_order,
            )
        ]

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

    def test_a_shared_row_also_records_a_breakdown(self, ctx: EngineContext, mock_plextv):
        """A shared 'popular on this server' row records a per-library breakdown too, keyed by its own
        slug — so the run detail groups a public row the same way it groups a private one."""
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        # Both watch the same title, so it clears the 2-distinct-watchers floor for a public row.
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]

        report = pipeline_mod.run(ctx, [sarah, mike])

        shared_report = next(u for u in report.users if u.slug == "shared_popular")
        assert shared_report.breakdown, "the shared row records a breakdown"
        assert all(e["row_slug"] == "popular" for e in shared_report.breakdown)

    def test_a_shared_text_poster_renders_and_uploads_to_its_collection(self, ctx: EngineContext, mock_plextv):
        """Shared delivery needs the same poster artist as a private row.

        The synthetic shared profile is the renderer's identity: its poster must use that identity
        and the target library, then reach the collection that this shared delivery creates.
        """
        ctx.config.rows = [
            RowSpec(
                slug="popular",
                name_template="Popular",
                size=5,
                shared=True,
                min_watchers=2,
                poster=PosterSpec(mode="text", title="{user}'s {library_name} picks"),
            )
        ]
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.poster_artist = MagicMock()
        ctx.poster_artist.render.return_value = b"SHARED-TEXT"

        report = pipeline_mod.run(ctx, [sarah, mike])

        shared_report = next(user for user in report.users if user.slug == "shared_popular")
        assert shared_report.status == "ok"
        ctx.poster_artist.render.assert_called_once_with(
            title="Everyone's Movies picks", subtitle="", style="", engine="text"
        )
        ctx.plex.upload_poster.assert_called_once_with(ctx.plex.create_collection.return_value, b"SHARED-TEXT")

    def test_a_shared_row_narrates_its_writes_under_its_own_slug(self, ctx: EngineContext, mock_plextv):
        """A shared row is delivered by a separate path from a person's row, so it needs its own proof
        that the run log says what it is about to write."""
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        emitted: list[tuple[str, str, dict]] = []
        ctx.progress = lambda slug, stage, counts, reason=None: emitted.append((slug, stage, counts))

        pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        writes = [(slug, counts) for slug, stage, counts in emitted if stage == "delivering" and "library" in counts]
        assert writes, "the shared row announced no write"
        assert all(slug == "shared_popular" and counts["row"] == "Popular" for slug, counts in writes)
        assert all(counts.get("creating", 0) > 0 for _, counts in writes)

    def test_a_shared_row_honours_the_server_wide_block_list(self, ctx: EngineContext, mock_plextv):
        """A blocked title must not appear in a public row.

        Asserted on the PICKS now, not on the argument to `derive_seeds`. The old test had to spy on
        the call because the picks depended on what TMDB returned for the surviving seeds, so "no
        picks" passed for a dozen unrelated reasons. A shared row is the server's most-watched titles
        now — no search, no seeds — so the outcome is directly assertable, and the block does what
        the setting always claimed: it keeps the title out.
        """
        ctx.plex.build_library_index.return_value = {4242: 999, 77: 777}
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        ctx.config.blocked_shared_seeds = {4242}
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        watched = [
            make_watched("Fargo", days_ago=1, rating_key=999, tmdb_id=4242),
            make_watched("Heat", days_ago=2, rating_key=777, tmdb_id=77),
        ]
        ctx.history_source.fetch.return_value = watched

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        titles = {p.title for u in report.users for p in u.picks if p.collection_slug == "popular"}
        assert titles, "the shared row delivered nothing — fixture problem, not the feature"
        assert "Fargo" not in titles, "a blocked title reached a row everyone can see"
        assert "Heat" in titles, "blocking one title must not empty the row"

    def test_one_persons_block_does_not_reshape_the_shared_row(self, ctx: EngineContext, mock_plextv):
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        sarah = make_profile("sarah", account_id=100)
        sarah.blocked_seeds = {4242}  # sarah's own preference
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999, tmdb_id=4242)]

        report = pipeline_mod.run(ctx, [sarah, mike])

        shared_report = next(u for u in report.users if u.slug == "shared_popular")
        assert shared_report.status != "skipped", "sarah's private block silently emptied a public row"

    def test_per_person_tokens_come_from_the_web_search_source_and_land_under_its_step(
        self, ctx: EngineContext, mock_plextv
    ):
        """The ONLY AI cost now is finding titles: the ``llm_web`` source. A run using it records that
        source's tokens into the user total AND under its own step bucket. Ranking/pick selection is
        code (picker.build_picks) with no LLM, so there is no 'curate' step and no per-row token spend."""

        class _WebCurator:
            supports_native_web_search = True
            last_tokens = 0

            def recommend_web(self, profile, seeds, k, *, guidance=None):
                # Set BY the call, as every real provider does. A value left over from before the call
                # is exactly what a failed call reads back, and it is no longer billed.
                self.last_tokens = 50
                self.last_output_tokens = 5
                return [{"title": "Web Pick", "year": 2020, "media": "movie"}]

        ctx.curator = _WebCurator()
        ctx.config.candidate_sources = ["tmdb_similar", "llm_web"]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
                {"id": 20, "title": "Fresh Twenty", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        # The web source's proposed title resolves to a real TMDB id, so llm_web actually contributes.
        ctx.tmdb.search.side_effect = lambda title, mt, year=None: (
            {"id": 30, "title": "Web Pick", "genre_ids": [], "vote_average": 8.5} if title == "Web Pick" else None
        )
        ctx.plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020, 30: 1030}

        report = pipeline_mod.run(ctx, [sarah])

        u = report.users[0]
        assert u.llm_tokens == 50
        # Tokens are attributed to the SOURCE that spent them (llm_web), not a curate step.
        assert u.llm_tokens_by_step == {"llm_web": 50}
        assert u.llm_output_tokens == 5
        assert u.exa_searches == 0  # native web search, no external Exa backend
        # No per-row LLM spend anymore: breakdown entries carry no token key.
        assert u.breakdown and all("llm_tokens" not in e for e in u.breakdown)

    def test_a_cancelled_run_skips_every_remaining_user(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """A cancel signalled before delivery skips every user's gather/build/deliver — no pick work,
        no picks — and each is marked 'skipped'. An in-flight user isn't interrupted mid-work (the
        check is per-user), so this never leaves a half-applied user."""
        ctx.cancelled = lambda: True
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert [u.status for u in report.users] == ["skipped", "skipped"]
        assert not any(u.picks for u in report.users)
        assert built == []  # cancelled before any gather/build ran

    def test_a_partial_cancel_still_merges_filters_and_promotes_the_delivered_user(
        self, ctx: EngineContext, mock_plextv
    ):
        """Leak-safety under cancel: cancel firing AFTER the first user must still deliver that user,
        hide their row on every OTHER account, and promote it — while the rest are skipped. The
        merge covering a NON-delivered account is the exact guarantee that a cancel can't leave a
        delivered row visible to the wrong person."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        # Cancel becomes true the moment sarah's row is actually WRITTEN, so she delivers and mike
        # (and the shared row) skip. Anchored to the write rather than to a count of `cancelled()`
        # calls: the engine gained a cancel check at every boundary a row passes through, and a magic
        # number here would have to be re-tuned for each one while testing nothing about them.
        cancelled = {"yes": False}
        ctx.cancelled = lambda: cancelled["yes"]

        created_by_label: dict[str, MagicMock] = {}

        def stored_label(collection, label, *, extra=None):
            created_by_label[label.lower()] = collection
            if extra is not None:
                # Recorded too: the constant label goes on in the SAME write now, so a fake that
                # dropped it would show fewer labelled rows than the real client produces.
                created_by_label[extra.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        def create_collection(section, title, items):
            cancelled["yes"] = True
            return MagicMock()

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.create_collection.side_effect = create_collection
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [created_by_label[label.lower()]] if label.lower() in created_by_label else []
        )

        report = pipeline_mod.run(ctx, [sarah, mike])

        statuses = {u.slug: u.status for u in report.users}
        assert statuses["sarah"] == "ok" and statuses["mike"] == "skipped"
        assert ctx.plex.create_collection.call_count == 1  # only the delivered user built a row
        # Leak-safe: mike (NOT delivered this run) still had sarah's delivered row excluded from his
        # share — the privacy merge covered every account, not just the ones built.
        mike_filters = next(u for u in mock_plextv.users if u.id == 200).filters
        assert mike_filters["filterMovies"] == "label!=Shortlist_sarah"
        assert ctx.plex.promote.call_count == 1  # only the delivered user was promoted

    def test_default_watched_cap_excludes_finished_titles(self, ctx: EngineContext, mock_plextv):
        """watched_pct defaults to 0 (all fresh): a title the user has finished, even if it resurfaces
        as a candidate, is never recommended back. Guards the pool_key/pools_for `== 0` branch — an
        inversion there would recommend everyone their already-watched titles and pass every leaf test.
        """
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        # She finished movie 900 (the seed, ratingKey 999). It resurfaces as a candidate — must drop.
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 900, "title": "Already Finished", "genre_ids": [], "vote_average": 9.0},
                {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
                {"id": 20, "title": "Fresh Twenty", "genre_ids": [], "vote_average": 7.0},
            ]
        )
        ctx.plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020}

        report = pipeline_mod.run(ctx, [sarah])

        ids = {p.tmdb_id for p in report.users[0].picks}
        assert 900 not in ids, "a finished title must never be recommended at the 0% default"
        assert ids & {10, 20}, "fresh candidates still fill the row"

    def test_watched_pct_of_one_lets_finished_non_seed_titles_through(self, ctx: EngineContext, mock_plextv):
        """At 100% there is no filtering: a finished title (that isn't itself a seed) stays in the pool
        AND may be delivered. Guards the opposite inversion of the `== 0` branch. The seed is always
        excluded regardless — you don't re-recommend the exact thing just watched."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.config.max_seeds = 1  # only movie 900 becomes a seed; movie 50 stays a finished non-seed
        ctx.config.min_history = 1
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=10, media="both", watched_pct=1.0)]
        ctx.history_source.fetch.return_value = [
            make_watched("Seed Movie", days_ago=1, rating_key=999),  # tmdb 900 — the sole seed
            make_watched("Finished Extra", days_ago=9, rating_key=550),  # tmdb 50 — finished, not a seed
        ]
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 50, "title": "Finished Extra", "genre_ids": [], "vote_average": 9.0},  # finished, resurfaced
                {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
            ]
        )
        ctx.plex.build_library_index.return_value = {900: 999, 50: 550, 10: 1010}

        report = pipeline_mod.run(ctx, [sarah])

        ids = {p.tmdb_id for p in report.users[0].picks}
        assert 50 in ids, "at 100% a finished (non-seed) title may still be recommended"
        assert 900 not in ids, "the seed itself is always excluded"

    def test_muting_removes_an_already_delivered_row(self, ctx: EngineContext, mock_plextv):
        from shortlist.engine.delivery import render_row_name, row_marker

        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(muted=True)})
        mock_plextv.users = [plextv_user(100, "sarah")]
        # A collection already on the server for this row (title = display + the account's marker). The
        # default template renders {library_name} from the delivering library ("Movies" in this ctx).
        display = render_row_name(ctx.config.row_name_template, sarah, [], library_name="Movies")
        existing = MagicMock()
        existing.title = display + row_marker(100)
        ctx.plex.find_owned_collections.return_value = [existing]

        report = pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert display in report.users[0].diff.deleted
        ctx.plex.create_collection.assert_not_called()  # muted -> nothing rebuilt

    def test_a_disabled_rows_collection_is_removed_from_its_owners_home(self, ctx: EngineContext, mock_plextv):
        """A row switched OFF in the UI still sat on its owner's Home (excluded from everyone else, so
        private — just not gone). The server hands disabled rows to the engine as retired_rows, which
        removes them like a mute — so 'off' means gone, not merely 'not refreshed'."""
        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import RowSpec

        # No enabled rows at all — the user's every row was disabled. Removal must still happen (it
        # sits before the "no rows -> return" check).
        ctx.config.rows = []
        ctx.config.rows_defined = True
        ctx.config.retired_rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=5)]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        target = MagicMock()
        target.title = "Hidden Gems" + row_marker(100)
        # A DIFFERENT-titled collection under the same label must NOT be touched — removal matches by
        # title, so the guard has to be load-bearing.
        bystander = MagicMock()
        bystander.title = ctx.config.row_name_template + row_marker(100)
        ctx.plex.find_owned_collections.return_value = [target, bystander]

        report = pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is target  # exactly the retired row
        assert "Hidden Gems" in report.users[0].diff.deleted
        ctx.plex.create_collection.assert_not_called()

    def test_a_retired_top_seed_copy_is_removed_by_its_ledger_key_and_nothing_else(
        self, ctx: EngineContext, mock_plextv
    ):
        """A row switched to shared is retired as a per-person row even when it is named `{top_seed}` — each
        person's old copy wears a title nothing can recompute, so the ledger key is the only safe handle. A
        copy the ledger does not name (mike's) is left alone, however much its title looks like the row's."""
        ctx.config.rows = []
        ctx.config.rows_defined = True
        ctx.config.retired_rows = [RowSpec(slug="because", name_template="Because you watched {top_seed}", size=5)]
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        section_key = str(ctx.plex.sections.return_value[0].key)
        sarahs_copy = fake_media_item(4242, "Because you watched The Bear" + row_marker(100))
        mikes_copy = fake_media_item(4343, "Because you watched Fargo" + row_marker(200))
        ctx.plex.find_owned_collections.side_effect = lambda section, label: {
            "shortlist_sarah": [sarahs_copy],
            "shortlist_mike": [mikes_copy],
        }.get(label, [])
        ctx.delivered_keys = {("sarah", "because", section_key): 4242}

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.plex.delete_owned_collection.call_args.args[0] is sarahs_copy
        sarah = next(u for u in report.users if u.slug == "sarah")
        assert sarah.removed_deliveries == [{"row_slug": "because", "library_key": section_key}]


class TestRequestsWiring:
    """The request pass only runs when enabled, and it sees the titles the library lacks."""

    def _suggest_a_missing_title(self, ctx: EngineContext) -> None:
        # Candidate 30 is NOT in the library index (which holds only 10 and 20), so it's requestable.
        ctx.tmdb.suggestions.return_value = _ranked(
            [
                {"id": 10, "title": "In Library", "genre_ids": [], "vote_average": 8.0, "vote_count": 900},
                {"id": 30, "title": "Missing Title", "genre_ids": [], "vote_average": 8.4, "vote_count": 800},
            ]
        )

    def test_disabled_by_default_never_calls_the_request_pass(self, ctx: EngineContext, mock_plextv, monkeypatch):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._suggest_a_missing_title(ctx)
        called = []
        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", lambda *a, **k: called.append(a))

        report = pipeline_mod.run(ctx, [sarah])

        assert called == []  # requests is None on the config -> no bookkeeping, no pass
        assert report.requests is None

    def test_enabled_run_feeds_missing_titles_to_the_request_pass(self, ctx: EngineContext, mock_plextv, monkeypatch):
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport
        from shortlist.engine.models import MediaType as MT

        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._suggest_a_missing_title(ctx)
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
        )

        captured = {}
        sentinel = RequestReport(considered=1)

        def spy(cfg, tmdb, demand, *, dry_run, already_handled=None, **kw):
            captured["demand"] = demand
            captured["dry_run"] = dry_run
            captured["already_handled"] = already_handled
            return sentinel

        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", spy)

        report = pipeline_mod.run(ctx, [sarah])

        # One RowRequest per row that wanted something, carrying that row's OWN demand map.
        rows = {row.slug: row.demand for row in captured["demand"]}
        assert list(rows) == ["picked"]
        # The missing title reached the request pass; the in-library one did not.
        assert (30, MT.MOVIE) in rows["picked"]
        assert (10, MT.MOVIE) not in rows["picked"]
        assert rows["picked"][(30, MT.MOVIE)].demand == 1
        # Its provenance names the row per library: a missing MOVIE renders {library_name} as the
        # movie library ("Movies"), so the inbox shows the same name the row is actually called...
        why = rows["picked"][(30, MT.MOVIE)].why
        assert why and why[0].row == "✨ Movies Picked for You"
        # ...and the SLUG beside it, which is the stable identity an approval months later needs to
        # resolve this row's Sonarr/Radarr target. The rendered name cannot serve: it carries the
        # person's display name and their own seed title.
        assert why[0].row_slug == "picked"
        assert report.requests is sentinel

    def test_per_row_pool_attributes_tags_to_the_row_that_surfaced_the_title(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport, RowSpec
        from shortlist.engine.models import MediaType as MT

        # Two rows for one user: a default one on tmdb_similar (all in-library, nothing missing) and
        # a "Hidden Gems" row on tmdb_discover that surfaces a MISSING title (id 30). The missing
        # title must carry only the discover row's tag (plus the user's), not the default row's.
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=5),  # inherits global -> tmdb_similar
            RowSpec(
                slug="gems",
                name_template="Hidden Gems",
                size=5,
                candidate_sources=["tmdb_discover"],
                request_tag="gems",
            ),
        ]
        sarah = make_profile("sarah", account_id=100, request_tag="sarah")
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 30, "title": "Missing Gem", "genre_ids": [], "vote_average": 8.4}
        ]
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
        )
        captured = {}
        monkeypatch.setattr(
            pipeline_mod.requests_mod,
            "request_missing",
            lambda cfg, tmdb, demand, **kw: captured.setdefault("demand", demand) or RequestReport(),
        )

        report = pipeline_mod.run(ctx, [sarah])

        rows = {row.slug: row.demand for row in captured["demand"]}
        # Only the row whose pool actually surfaced a missing title is in the request pass at all.
        assert list(rows) == ["gems"]
        missing = rows["gems"][(30, MT.MOVIE)]
        assert missing.tags == {"sarah", "gems"}  # user tag + the row whose pool surfaced it, not "picked"
        assert missing.demand == 1  # counted once for this user despite multiple rows/pools
        # Distinct-union candidate count spans both pools: {10,20} (similar) and {30} (discover).
        assert report.users[0].counts.candidates == 3


class TestAutoUserTag:
    """The global "tag requests by person" switch and its per-row override.

    Tagging each request with the wanting person's slug is how the owner sees, from inside
    Sonarr/Radarr, WHO a title was added for — the inbox's why-line never reaches the Arr. Off by
    default, so an upgrade tags nothing until it is switched on. A row's own `auto_user_tag`
    overrides the global in either direction (None -> inherit), and an explicit per-user tag always
    beats the automatic slug: someone with a hand-set tag keeps it.
    """

    def _missing_tags(
        self,
        ctx: EngineContext,
        mock_plextv,
        monkeypatch,
        *,
        global_on: bool,
        row_override: bool | None,
        user_tag: str = "",
    ) -> set[str]:
        """Tags on the one missing title, for a single "gems" row that surfaced it."""
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport, RowSpec
        from shortlist.engine.models import MediaType as MT

        ctx.config.rows = [
            RowSpec(
                slug="gems",
                name_template="Hidden Gems",
                size=5,
                candidate_sources=["tmdb_discover"],
                request_tag="gems",
                auto_user_tag=row_override,
            )
        ]
        sarah = make_profile("sarah", account_id=100, request_tag=user_tag)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 30, "title": "Missing Gem", "genre_ids": [], "vote_average": 8.4}
        ]
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
            auto_user_tag=global_on,
        )
        captured = {}
        monkeypatch.setattr(
            pipeline_mod.requests_mod,
            "request_missing",
            lambda cfg, tmdb, demand, **kw: captured.setdefault("demand", demand) or RequestReport(),
        )

        pipeline_mod.run(ctx, [sarah])

        rows = {row.slug: row.demand for row in captured["demand"]}
        return rows["gems"][(30, MT.MOVIE)].tags

    def test_off_by_default_tags_no_username(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # The upgrade case: nobody has touched the setting, so the Arr sees only the row's own tag.
        tags = self._missing_tags(ctx, mock_plextv, monkeypatch, global_on=False, row_override=None)
        assert tags == {"gems"}

    def test_global_switch_tags_the_users_slug(self, ctx: EngineContext, mock_plextv, monkeypatch):
        tags = self._missing_tags(ctx, mock_plextv, monkeypatch, global_on=True, row_override=None)
        assert tags == {"gems", "sarah"}

    def test_row_override_off_beats_the_global_switch(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # A row opting out still keeps its OWN tag — only the person's slug is suppressed.
        tags = self._missing_tags(ctx, mock_plextv, monkeypatch, global_on=True, row_override=False)
        assert tags == {"gems"}

    def test_row_override_on_beats_the_global_switch(self, ctx: EngineContext, mock_plextv, monkeypatch):
        tags = self._missing_tags(ctx, mock_plextv, monkeypatch, global_on=False, row_override=True)
        assert tags == {"gems", "sarah"}

    def test_an_explicit_user_tag_replaces_the_automatic_slug(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # Not layered: someone who set "vip" by hand chose their tag, and getting "vip" AND "sarah"
        # is the clutter the automatic tag was dropped for in the first place.
        tags = self._missing_tags(ctx, mock_plextv, monkeypatch, global_on=True, row_override=None, user_tag="vip")
        assert tags == {"gems", "vip"}

    def test_the_slug_reaches_the_arr_client_as_a_real_tag(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """The join nothing else covers: the switch is read at one end of the run and the tag is sent
        at the other, and every test either side of this stops at `MissingTitle.tags`. Runs the REAL
        request pass so a break anywhere in between shows up here."""
        from shortlist.engine import requests as requests_mod
        from shortlist.engine.models import ArrTarget, RequestConfig, RowSpec
        from tests.unit.test_requests import FakeArr

        radarr = FakeArr()
        monkeypatch.setattr(requests_mod, "RadarrClient", lambda *a, **k: radarr)

        ctx.config.rows = [
            RowSpec(slug="gems", name_template="Hidden Gems", size=5, candidate_sources=["tmdb_discover"])
        ]
        # A slug with an underscore, because that is what a two-word Plex name produces. It reaches
        # the client verbatim; turning it into `moo-house` for the Arr's charset is the CLIENT's job
        # and is pinned separately (`test_arr.py::test_tags_are_sanitized_to_the_arr_charset`).
        steve = make_profile("Guest", account_id=100, slug="guest_user")
        mock_plextv.users = [plextv_user(100, "Guest")]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 30, "title": "Missing Gem", "genre_ids": [], "vote_average": 9.0, "vote_count": 900}
        ]
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
            auto_user_tag=True,
            auto_min_demand=1,  # one person is enough, or nothing is SENT and there is no call to assert
        )

        pipeline_mod.run(ctx, [steve])

        assert radarr.tag_calls == [{"guest_user"}], "the wanting person's slug never reached Radarr"

    def test_an_explicit_user_tag_survives_a_row_opting_out(self, ctx: EngineContext, mock_plextv, monkeypatch):
        # `auto_user_tag` governs the AUTOMATIC slug only. A tag the owner typed on a person is not
        # Shortlist's to drop, or turning the switch off on one row would silently unpick it.
        tags = self._missing_tags(ctx, mock_plextv, monkeypatch, global_on=True, row_override=False, user_tag="vip")
        assert tags == {"gems", "vip"}


class TestEffectiveRowSources:
    """candidate_sources (or the global default) is the single source of truth for every row —
    llm_web included, per-person or shared (a head-to-head showed it adds strong taste matches)."""

    def _spec(self, *, shared: bool, sources=None) -> RowSpec:
        return RowSpec(slug="r", name_template="", size=10, shared=shared, candidate_sources=sources or [])

    def test_llm_web_is_kept_for_a_per_person_row(self):
        from shortlist.engine.rows import effective_row_sources

        srcs = effective_row_sources(self._spec(shared=False), ["tmdb_similar", "llm_web", "llm_library"])
        assert set(srcs) == {"tmdb_similar", "llm_web", "llm_library"}

    def test_llm_web_is_kept_for_a_shared_row(self):
        from shortlist.engine.rows import effective_row_sources

        srcs = effective_row_sources(self._spec(shared=True), ["tmdb_similar", "llm_web"])
        assert "llm_web" in srcs

    def test_a_rows_own_sources_win_over_the_default(self):
        from shortlist.engine.rows import effective_row_sources

        srcs = effective_row_sources(self._spec(shared=False, sources=["tmdb_discover"]), ["tmdb_similar", "llm_web"])
        assert srcs == ("tmdb_discover",)


class TestASettingsChangeRebuildsTheRow:
    """Changing a setting that decides row contents must take effect on the next run.

    Freshness suppresses churn when nothing changed. It was also delaying changes made on purpose:
    raising "Recent releases" on a real server left 36 of 42 rows redelivering byte-identical picks
    for up to a fortnight, which reads as the setting being broken.
    """

    RUN_DAY = date(2026, 6, 15).toordinal()
    KEY = ("sarah", "picked", "1")

    def _ctx(self, ctx: EngineContext, *, recency: float) -> None:
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
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        # A cadence of 0 = frozen. Nothing but a recipe change may rebuild this row.
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="", size=4, media="movie", refresh_days=0, recency=recency)
        ]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        ctx.run_day = self.RUN_DAY

    def _prior(self, recipe: str):
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
                recipe=recipe,
            )
            for i, t in enumerate([10, 11, 12, 13])
        ]

    def _run(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        return [p["tmdb_id"] for p in picks]

    def test_a_frozen_row_still_redelivers_unchanged_when_nothing_changed(self, ctx, mock_plextv):
        """The control. Same settings, frozen row — the cadence must still do its job."""
        self._ctx(ctx, recency=0.0)
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver, row_recipe

        policy = RowPolicy(
            ctx=ctx,
            user=make_profile("sarah", account_id=100),
            cfg=ctx.config,
            specs=ctx.config.rows,
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )
        same = row_recipe(policy, ctx.config.rows[0])
        ctx.previous_picks = {self.KEY: self._prior(same)}
        ctx.previous_recipes = {self.KEY: same}

        assert self._run(ctx, mock_plextv) == [10, 11, 12, 13]

    def test_changing_a_setting_rebuilds_a_frozen_row_immediately(self, ctx, mock_plextv):
        """The fix. The stored row was built at recency 0; tonight the row is at 1.0, so it must
        rebuild now rather than wait — and a frozen row would otherwise wait for ever."""
        self._ctx(ctx, recency=1.0)
        ctx.previous_picks = {self.KEY: self._prior("media=movie|recency=0.0|stale")}
        ctx.previous_recipes = {self.KEY: "media=movie|recency=0.0|stale"}

        delivered = self._run(ctx, mock_plextv)

        assert delivered != [10, 11, 12, 13], "a changed setting did not rebuild the row"
        assert delivered == [19, 18, 17, 16], f"expected the newest four at full recency, got {delivered}"

    def test_a_row_with_no_recorded_recipe_is_left_alone(self, ctx, mock_plextv):
        """Picks predating this feature carry no recipe. Treating unknown as a mismatch would
        rebuild every row on every server the first night after an upgrade — the exact churn
        the cadence exists to prevent."""
        self._ctx(ctx, recency=1.0)
        ctx.previous_picks = {self.KEY: self._prior("")}
        ctx.previous_recipes = {}

        assert self._run(ctx, mock_plextv) == [10, 11, 12, 13]

    def test_the_delivered_picks_carry_tonights_recipe(self, ctx, mock_plextv):
        """Without this the row rebuilds on every run for ever, because the stored recipe never
        catches up with the settings."""
        self._ctx(ctx, recency=1.0)
        ctx.previous_picks = {self.KEY: self._prior("stale")}
        ctx.previous_recipes = {self.KEY: "stale"}
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        recipes = {p.recipe for p in report.users[0].picks}
        assert recipes and "stale" not in recipes, f"picks kept the old recipe: {recipes}"
        assert len(recipes) == 1, f"one row delivered two recipes: {recipes}"


class TestBuiltAtStamping:
    """`built_at` says when a row's CONTENTS were last chosen — the clock the idle hold measures.

    Deliberately not "when were these picks last written": a carried-forward row is re-persisted
    under every run, so a stamp that moved with delivery would report every row as newly built, the
    ceiling would never expire, and "watched since it was built" would be false for everyone.
    """

    KEY = ("sarah", "picked", "1")

    def _ctx(self, ctx: EngineContext, *, refresh_days: int) -> None:
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 2000 + i for i in range(10, 20)}}
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in range(10, 20)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=3, media="movie", refresh_days=refresh_days)]
        ctx.config.min_history = 1
        ctx.run_day = date(2026, 6, 15).toordinal()
        ctx.run_at = NOW

    def _run(self, ctx, mock_plextv):
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])
        picks = report.users[0].picks
        assert picks, "the run delivered no picks"
        return picks

    def test_a_rebuild_stamps_this_runs_time(self, ctx: EngineContext, mock_plextv):
        """Deliberately does NOT set `ctx.run_at` — `pipeline.run` deriving it from the report's own
        start is what has to make this pass. Every other test here assigns it, so without this one
        that assignment could be deleted and the whole feature would go inert (no clock = never held,
        every pick stamped None) with the suite still green."""
        self._ctx(ctx, refresh_days=1)
        ctx.run_at = None

        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)])

        assert report.users[0].picks
        assert {p.built_at for p in report.users[0].picks} == {report.started_at}

    def test_a_carried_forward_row_keeps_the_original_stamp(self, ctx: EngineContext, mock_plextv):
        """The load-bearing half. Re-stamping here would make every row permanently "just built":
        the ceiling could never expire and nothing would ever look watched-since."""
        built = NOW - timedelta(days=9)
        self._ctx(ctx, refresh_days=0)  # frozen: always a reuse night
        ctx.previous_picks = {
            self.KEY: [
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
                    built_at=built,
                )
                for i, t in enumerate([17, 18, 19])
            ]
        }
        ctx.previous_recipes = {}

        assert {p.built_at for p in self._run(ctx, mock_plextv)} == {built}

    def test_a_carry_forward_of_unstamped_picks_stays_unstamped(self, ctx: EngineContext, mock_plextv):
        """Unknown must stay unknown until something is actually re-picked.

        Stamping `now` here says "the contents were decided tonight" about a night on which nothing
        was decided. That stamp is then newer than a watch that really did happen after the true
        build, so the next due night holds a row that has new watches to answer to — for every row on
        the server the night after 0086 lands, and for everyone who has just graduated from cold
        start (the cold branch stamps nothing either).
        """
        self._ctx(ctx, refresh_days=0)  # frozen: always a reuse night
        ctx.previous_picks = {
            self.KEY: [
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
                )  # no built_at — a pre-0086 or cold-start row
                for i, t in enumerate([17, 18, 19])
            ]
        }
        ctx.previous_recipes = {}

        assert {p.built_at for p in self._run(ctx, mock_plextv)} == {None}

    def test_a_refresh_night_re_stamps_every_pick_including_the_survivors(self, ctx: EngineContext, mock_plextv):
        """A refresh re-decides the whole row — the two-thirds that survived were re-ranked against
        tonight's pool and kept on purpose. Leaving their old stamps would make the row read as
        older than its contents and expire the ceiling early."""
        self._ctx(ctx, refresh_days=1)
        ctx.previous_picks = {
            self.KEY: [
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
                    built_at=NOW - timedelta(days=9),
                )
                for i, t in enumerate([10, 11, 12])
            ]
        }
        ctx.previous_recipes = {}

        assert {p.built_at for p in self._run(ctx, mock_plextv)} == {NOW}
