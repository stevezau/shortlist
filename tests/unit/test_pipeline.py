"""Pipeline orchestration: per-user isolation, curator fallback, cold start, dry-run,
and the leak-safe ordering (deliver unpromoted → sync filters → promote last)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.clients.tmdb import NullCache
from shortlist.engine.curator.base import CuratorError
from shortlist.engine.models import EngineConfig, MediaType, OwnedRow, RowOverride, RowSpec
from shortlist.engine.pipeline import EngineContext
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user


@pytest.fixture
def ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    plex = MagicMock()
    movie_section = MagicMock()
    movie_section.type = "movie"
    movie_section.title = "Movies"  # fills {library_name} in the default row title
    plex.sections.return_value = [movie_section]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section}
    movie_section.collections.return_value = []
    # Library: watched item 900 (ratingKey 999) + candidates 10 and 20.
    plex.build_library_index.return_value = ({900: 999, 10: 1010, 20: 1020}, {})
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []  # delivery finds by title; promotion enumerates rows
    plex.stored_label.side_effect = lambda collection, label: label.replace("shortlist", "Shortlist", 1)
    plex.fetch_items.side_effect = lambda keys: [fake_media_item(k, f"item{k}") for k in keys]

    history = MagicMock()
    history.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]

    mock_tmdb.suggestions.return_value = [
        {"id": 10, "title": "Candidate Ten", "genre_ids": [], "vote_average": 8.0},
        {"id": 20, "title": "Candidate Twenty", "genre_ids": [], "vote_average": 7.0},
    ]
    mock_tmdb.genre_names.return_value = {}

    def put(account_id, fields):
        for u in mock_plextv.users:
            if u.id == account_id:
                u.filters.update(fields)

    mock_plextv.update_user_filters.side_effect = put

    return EngineContext(
        config=engine_config,
        plex=plex,
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=history,
        curator=mock_curator,
        snapshots=MemorySnapshotStore(),
    )


def curated_picks(profile, ranked, k):
    from shortlist.engine.curator.null import NullCurator

    return NullCurator().curate(profile, ranked, k)


class TestRun:
    def test_happy_path_delivers_syncs_then_promotes(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.curator.curate.side_effect = curated_picks

        # A row does not exist until created (delivery takes the create path); capture each created
        # collection by the label it is stored under, so promotion — which enumerates a user's rows
        # by label — finds it.
        created_by_label: dict[str, MagicMock] = {}

        def stored_label(collection, label):
            created_by_label[label.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.create_collection.side_effect = lambda section, title, items: MagicMock()
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [created_by_label[label.lower()]] if label.lower() in created_by_label else []
        )

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.ok
        assert all(u.status == "ok" for u in report.users)
        assert all(u.privacy_synced for u in report.users)
        # Real deliver_row ran: collections created with the row title, stored labels title-cased.
        assert ctx.plex.create_collection.call_count == 2
        # Each user's filter excludes exactly the OTHER user's stored (title-cased) label.
        sarah_filters = next(u for u in mock_plextv.users if u.id == 100).filters
        assert sarah_filters["filterMovies"] == "label!=Shortlist_mike"
        mike_filters = next(u for u in mock_plextv.users if u.id == 200).filters
        assert mike_filters["filterMovies"] == "label!=Shortlist_sarah"
        # Promotion happened last, for both users' collections.
        assert ctx.plex.promote.call_count == 2

    def test_promotion_only_after_filters_are_merged(self, ctx: EngineContext, mock_plextv):
        """Leak-window regression: no promote call may precede the plex.tv filter writes."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.curator.curate.side_effect = curated_picks
        order = []
        original_put = mock_plextv.update_user_filters.side_effect

        def put(account_id, fields):
            order.append("filter")
            original_put(account_id, fields)

        mock_plextv.update_user_filters.side_effect = put
        ctx.plex.promote.side_effect = lambda *a, **k: order.append("promote")
        existing = MagicMock()
        existing.title = "✨ Picked for You"
        existing.items.return_value = []
        ctx.plex.find_owned_collections.return_value = [existing]

        pipeline_mod.run(ctx, [sarah, mike])

        assert "promote" in order and "filter" in order
        assert order.index("filter") < order.index("promote")
        first_promote = order.index("promote")
        assert all(entry == "promote" for entry in order[first_promote:])

    def test_sync_failure_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.curator.curate.side_effect = curated_picks
        mock_plextv.update_user_filters.side_effect = RuntimeError("plex.tv down")

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        ctx.plex.promote.assert_not_called()

    def test_one_user_failing_never_stops_the_others(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.curator.curate.side_effect = curated_picks
        good_history = ctx.history_source.fetch.return_value

        def fetch(user, *, min_completion):
            if user.slug == "sarah":
                raise RuntimeError("tautulli exploded")
            return good_history

        ctx.history_source.fetch.side_effect = fetch
        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        by_slug = {u.slug: u for u in report.users}
        assert by_slug["sarah"].status == "error"
        assert "tautulli exploded" in by_slug["sarah"].error
        assert by_slug["mike"].status == "ok"
        # Privacy sync still ran for the errored user (delivery and sync are independent).
        assert by_slug["sarah"].privacy_synced or by_slug["sarah"].error

    def test_curator_failure_degrades_to_heuristic(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = CuratorError("LLM down")

        report = pipeline_mod.run(ctx, [sarah])

        user_report = report.users[0]
        assert user_report.status == "ok"
        assert user_report.counts.picks > 0
        assert user_report.picks[0].reason.startswith("Because you watched")

    def test_short_curator_output_padded_from_heuristic_order(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = lambda profile, ranked, k: curated_picks(profile, ranked, 1)

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].counts.picks == 2  # both library candidates used
        assert [p.rank for p in report.users[0].picks] == [1, 2]

    def test_cold_start_uses_popular_row(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.history_source.fetch.return_value = [make_watched("Only One")]
        ctx.history_source.fetch.side_effect = None
        # The guid parse now lives in PlexClient.top_rated; cold start just consumes (tmdb_id, item)
        # pairs. A movies-only server yields one movie pick.
        ctx.plex.top_rated.return_value = [(50, fake_media_item(1, "Top Rated", tmdb_id=50))]

        report = pipeline_mod.run(ctx, [sarah])

        user_report = report.users[0]
        assert user_report.status == "cold_start"
        assert [p.title for p in user_report.picks] == ["Top Rated"]
        assert user_report.picks[0].reason == "Popular on this server"

    def test_dry_run_makes_zero_plex_writes(self, ctx: EngineContext, mock_plextv):
        ctx.config.dry_run = True
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.ok
        mock_plextv.update_user_filters.assert_not_called()
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()
        # No collections exist yet, so there is nothing to exclude — dry run says so honestly.
        assert not any(u.privacy_synced for u in report.users)

    def test_dry_run_steady_state_reports_no_filter_changes(self, ctx: EngineContext, mock_plextv):
        """With existing collections + correct filters, a dry run is a full no-op."""
        ctx.config.dry_run = True
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        ctx.plex.owned_collections.return_value = {
            "sarah": OwnedRow("Shortlist_sarah", [1]),
            "mike": OwnedRow("Shortlist_mike", [2]),
        }
        mock_plextv.users = [
            plextv_user(
                100,
                "sarah",
                filters={"filterMovies": "label!=Shortlist_mike", "filterTelevision": "label!=Shortlist_mike"},
            ),
            plextv_user(
                200,
                "mike",
                filters={"filterMovies": "label!=Shortlist_sarah", "filterTelevision": "label!=Shortlist_sarah"},
            ),
        ]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.ok
        assert not any(u.privacy_synced for u in report.users)
        mock_plextv.update_user_filters.assert_not_called()

    def test_no_picks_leaves_existing_row_untouched(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.suggestions.return_value = []  # nothing suggested -> no candidates
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].counts.picks == 0
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()


class TestPerRowOverrides:
    """A per-user override can mute, resize, or restyle one row without touching it for others."""

    def test_picks_are_tagged_with_their_row_slug(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah])

        picks = report.users[0].picks
        assert picks and all(p.collection_slug == "picked" for p in picks)  # the default row's slug
        # Each pick also carries the library it was delivered into, so the report can split a
        # multi-library row per library. section_key is the Plex key; library its display name.
        assert all(p.section_key and p.library for p in picks)

    def test_muting_the_only_row_delivers_nothing(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(muted=True)})
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].picks == []
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()

    def test_per_row_size_override_wins(self, ctx: EngineContext, mock_plextv):
        # The fixture pool has 2 candidates; an override of size 1 must cap this user's row at 1.
        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(size=1)})
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah])

        assert len(report.users[0].picks) == 1

    def test_a_tone_only_override_keeps_the_rows_guidance_and_template(self, ctx: EngineContext, mock_plextv):
        """The headline overlay bug: a per-person override REPLACED the row's whole recipe, so setting
        only a tone for one person wiped that row's guidance and custom prompt. It must overlay,
        field by field — blank means inherit."""
        from shortlist.engine.models import PromptConfig, RowSpec

        ctx.config.rows = [
            RowSpec(
                slug="picked",
                name_template="",
                size=5,
                prompt=PromptConfig(tone="cinephile", guidance="deep cuts", template="ROW TEMPLATE"),
            )
        ]
        sarah = make_profile(
            "sarah",
            account_id=100,
            row_overrides={"picked": RowOverride(prompt=PromptConfig(tone="playful"))},  # tone only
        )
        mock_plextv.users = [plextv_user(100, "sarah")]
        seen: dict[str, str] = {}

        def capture(profile, ranked, k):
            seen["tone"] = profile.prompt.tone
            seen["guidance"] = profile.prompt.guidance
            seen["template"] = profile.prompt.template
            return curated_picks(profile, ranked, k)

        ctx.curator.curate.side_effect = capture
        pipeline_mod.run(ctx, [sarah])

        assert seen["tone"] == "playful"  # the person's override
        assert "deep cuts" in seen["guidance"]  # the row's guidance SURVIVES
        assert seen["template"] == "ROW TEMPLATE"  # ...and its template

    def test_per_row_prompt_override_reaches_the_curator(self, ctx: EngineContext, mock_plextv):
        from shortlist.engine.models import PromptConfig

        sarah = make_profile(
            "sarah",
            account_id=100,
            prompt=PromptConfig(tone="balanced"),
            row_overrides={"picked": RowOverride(prompt=PromptConfig(tone="playful", guidance="be spooky"))},
        )
        mock_plextv.users = [plextv_user(100, "sarah")]
        seen: dict[str, str] = {}

        def capture(profile, ranked, k):
            seen["tone"] = profile.prompt.tone
            seen["guidance"] = profile.prompt.guidance
            return curated_picks(profile, ranked, k)

        ctx.curator.curate.side_effect = capture
        pipeline_mod.run(ctx, [sarah])

        assert seen == {"tone": "playful", "guidance": "be spooky"}  # the row override, not the base

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
        ctx.curator.curate.side_effect = curated_picks

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
        ctx.curator.curate.side_effect = curated_picks
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        ctx.tmdb.discover.side_effect = lambda mt, gids, **kw: []

        pipeline_mod.run(ctx, [sarah])

        # One seed, one shared pool -> tmdb_similar queried once, not once per row.
        assert ctx.tmdb.suggestions.call_count == 1

    def test_row_pinned_to_a_non_lowest_key_library_is_delivered_and_promoted_there(
        self, ctx: EngineContext, mock_plextv
    ):
        # Regression: promotion is the only thing that hides a collection from LIBRARY BROWSE
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
        ctx.plex.build_library_index.side_effect = lambda s, ep=None: (
            {900: 999, 10: 1010, 20: 1020} if s is lib1 else {900: 999, 10: 2010, 20: 2020},
            {},
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, library_keys=["2"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

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

    def test_a_pinned_row_only_recommends_titles_its_own_library_holds(self, ctx: EngineContext, mock_plextv):
        """A row pinned to a library was curated against the UNION of every library of its type, and
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
        ctx.plex.build_library_index.side_effect = lambda s, ep=None: (
            {900: 999, 10: 1010, 20: 1020} if s is lib1 else {900: 999, 10: 2010},
            {},
        )
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, library_keys=["2"])]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        offered: list[int] = []

        def curate(profile, candidates, k):
            offered.extend(c.tmdb_id for c in candidates)
            return curated_picks(profile, candidates, k)

        ctx.curator.curate.side_effect = curate

        pipeline_mod.run(ctx, [sarah])

        # 20 isn't in lib2, so the curator must never have been offered it.
        assert 10 in offered
        assert 20 not in offered, "the row was offered a title its own library doesn't hold"

    def test_a_shows_only_row_survives_a_movie_heavy_pool(self, ctx: EngineContext, mock_plextv):
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
        ctx.plex.build_library_index.side_effect = lambda s, ep=None: (movies if s is movie_section else shows, {})
        ctx.config.rows = [RowSpec(slug="tv", name_template="TV Picks", size=2, media="show")]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        # 59 high-rated movies flood the pool and ONE lower-rated show — from the SAME source, so a
        # source quota can't rescue it. Only filtering by media BEFORE the cut can.
        def suggestions(tid, mt):
            if mt is MediaType.MOVIE:
                return [{"id": i, "title": f"Movie {i}", "genre_ids": [], "vote_average": 9.0} for i in range(1, 60)]
            return [{"id": 5001, "title": "A Show", "genre_ids": [], "vote_average": 6.0}]

        ctx.tmdb.suggestions.side_effect = suggestions
        ctx.config.candidate_sources = ["tmdb_similar"]
        ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        # A show seed so the SHOW media type is in play at all (typed as a SHOW, or no show seed is
        # derived and tmdb_discover is never asked for shows).
        ctx.history_source.fetch.return_value = [
            *[make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)],
            make_watched("Breaking Bad", days_ago=2, rating_key=5999, media_type=MediaType.SHOW),
        ]
        offered: list[int] = []

        def curate(profile, candidates, k):
            offered.extend(c.tmdb_id for c in candidates)
            return curated_picks(profile, candidates, k)

        ctx.curator.curate.side_effect = curate

        pipeline_mod.run(ctx, [sarah])

        assert offered, "the shows-only row was offered no candidates at all"
        assert all(i >= 5000 for i in offered), f"a shows-only row was offered movies: {offered}"

    def test_the_ai_library_catalog_is_built_when_only_a_ROW_asks_for_it(self, ctx: EngineContext, mock_plextv):
        """A row overriding its sources to llm_library found an empty catalog — it was only built when
        the GLOBAL setting listed the source — so it produced nothing, forever, reporting ok."""
        ctx.config.candidate_sources = ["tmdb_similar"]  # global set does NOT include llm_library
        ctx.config.rows = [RowSpec(slug="gems", name_template="Gems", size=5, candidate_sources=["llm_library"])]
        ctx.plex.build_library_catalog.return_value = [
            {"tmdb_id": 10, "rating_key": 1010, "title": "Owned Ten", "year": 2010, "genres": []},
        ]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

        pipeline_mod.run(ctx, [sarah])

        assert ctx.plex.build_library_catalog.called, "the AI-from-library catalog was never built"

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
        ctx.curator.curate.side_effect = curated_picks

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
        ctx.curator.curate.side_effect = curated_picks

        pipeline_mod.run(ctx, [sarah])

        ctx.plex.create_collection.assert_called()  # the synthesized "Picked for You"

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
        ctx.plex.build_library_index.side_effect = lambda sec, ep=None: (movies if sec is movie_section else shows, {})

        def suggestions(tid, mt):
            # Plenty of BOTH movie and show candidates in the pool.
            base = 1 if mt is MediaType.MOVIE else 5000
            return [
                {"id": base + i, "title": f"T{base + i}", "genre_ids": [], "vote_average": 8.0} for i in range(1, 40)
            ]

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
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah])

        picks = report.users[0].picks
        movie_picks = [p for p in picks if p.media_type is MediaType.MOVIE]
        show_picks = [p for p in picks if p.media_type is MediaType.SHOW]
        assert len(movie_picks) == 10, f"movie row should fill to 10, got {len(movie_picks)}"
        assert len(show_picks) == 10, f"show row should fill to 10, got {len(show_picks)}"

    def test_a_row_curates_each_library_from_that_librarys_own_contents(self, ctx: EngineContext, mock_plextv):
        """Two libraries of the SAME media type each get their OWN full row, curated only from the
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
        ctx.plex.build_library_index.side_effect = lambda sec, ep=None: (idx_std if sec is movies else idx_4k, {})
        # The candidate pool spans BOTH libraries' titles; each library must pick only its own.
        pool = [
            {"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in [*range(10, 16), *range(50, 56)]
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: pool
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50  # keep the whole 12-title pool; don't truncate either library
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

        pipeline_mod.run(ctx, [sarah])

        # One curate call per library, each seeing ONLY that library's tmdb ids.
        seen = [{c.tmdb_id for c in call.args[1]} for call in ctx.curator.curate.call_args_list]
        assert {10, 11, 12, 13, 14, 15} in seen, f"Movies library should curate from its own ids, saw {seen}"
        assert {50, 51, 52, 53, 54, 55} in seen, f"4K library should curate from its own ids, saw {seen}"

    def test_run_records_a_breakdown_entry_per_library(self, ctx: EngineContext, mock_plextv):
        """The per-user report carries a per-(row, library) breakdown so the UI can show 'added X to
        Movies, Y to TV' with each library's own picks — not one merged list."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        idx_std = {900: 999, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec, ep=None: (idx_std if sec is movies else idx_4k, {})
        pool = [
            {"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in [*range(10, 16), *range(50, 56)]
        ]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: pool
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah])

        breakdown = report.users[0].breakdown
        by_library = {e["library_title"]: e for e in breakdown}
        assert set(by_library) == {"Movies", "4K Movies"}, f"one entry per library, got {list(by_library)}"
        for entry in breakdown:
            assert entry["row_slug"] == "picked"
            assert len(entry["picks"]) == 5, "each library's row has its own full set of picks"
            assert [p["rank"] for p in entry["picks"]] == [1, 2, 3, 4, 5], "picks ranked 1..k within the library"

    def test_freshness_rotates_which_candidates_the_curator_sees(self, ctx: EngineContext, mock_plextv):
        """A freshness>0 row rotates its candidates by the run's day, so the curator leads with a
        different (still strong) title than the raw #1 — the mechanism behind day-to-day variety."""
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=4, media="movie", freshness=1.0)]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        ctx.run_day = 2  # a non-zero phase, so rotation is active and reproducible
        idx = {900: 999, **{i: 1000 + i for i in range(1, 21)}}
        ctx.plex.build_library_index.return_value = (idx, {})
        # Distinct descending ratings so the pre-rank order is deterministic: id 1 is the strongest.
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 9.9 - i * 0.1} for i in range(1, 21)]
        ctx.tmdb.suggestions.return_value = pool
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        seen: dict[str, int] = {}

        def capture(profile, ranked, k):
            seen.setdefault("first", ranked[0].tmdb_id)
            return curated_picks(profile, ranked, k)

        ctx.curator.curate.side_effect = capture

        pipeline_mod.run(ctx, [sarah])

        # Exact, not just "changed": freshness 1.0 with run_day 2 and k 4 rotates by (2*4) % 20 = 8,
        # so the curator's first candidate is the rank-9 title (id 9) — pinning the phase/direction.
        assert seen["first"] == 9, f"expected the day-2 rotation to lead with id 9, saw {seen['first']}"

    def test_a_shared_row_also_records_a_breakdown(self, ctx: EngineContext, mock_plextv):
        """A shared 'popular on this server' row records a per-library breakdown too, keyed by its own
        slug — so the run detail groups a public row the same way it groups a private one."""
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        # Both watch the same title, so it clears the 2-distinct-watchers floor for a public row.
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah, mike])

        shared_report = next(u for u in report.users if u.slug == "shared_popular")
        assert shared_report.breakdown, "the shared row records a breakdown"
        assert all(e["row_slug"] == "popular" for e in shared_report.breakdown)

    def test_a_shared_row_accounts_its_llm_tokens(self, ctx: EngineContext, mock_plextv):
        """Shared-row LLM spend used to vanish — only the per-person path accumulated tokens. Each
        curated library section adds its curator's token count to the shared report."""
        ctx.config.rows = [RowSpec(slug="popular", name_template="Popular", size=5, shared=True, min_watchers=2)]
        sarah = make_profile("sarah", account_id=100)
        mike = make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.curator.curate.side_effect = curated_picks
        ctx.curator.last_tokens = 37  # each curated section reports this

        report = pipeline_mod.run(ctx, [sarah, mike])

        shared_report = next(u for u in report.users if u.slug == "shared_popular")
        sections = len({e["library_key"] for e in shared_report.breakdown})
        assert shared_report.llm_tokens == 37 * sections, "shared-row tokens sum across curated libraries"

    def test_default_watched_cap_excludes_finished_titles(self, ctx: EngineContext, mock_plextv):
        """watched_pct defaults to 0 (all fresh): a title the user has finished, even if it resurfaces
        as a candidate, is never recommended back. Guards the pool_key/pools_for `== 0` branch — an
        inversion there would recommend everyone their already-watched titles and pass every leaf test.
        """
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        # She finished movie 900 (the seed, ratingKey 999). It resurfaces as a candidate — must drop.
        ctx.tmdb.suggestions.return_value = [
            {"id": 900, "title": "Already Finished", "genre_ids": [], "vote_average": 9.0},
            {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
            {"id": 20, "title": "Fresh Twenty", "genre_ids": [], "vote_average": 7.0},
        ]
        ctx.plex.build_library_index.return_value = ({900: 999, 10: 1010, 20: 1020}, {})
        ctx.curator.curate.side_effect = curated_picks

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
        ctx.tmdb.suggestions.return_value = [
            {"id": 50, "title": "Finished Extra", "genre_ids": [], "vote_average": 9.0},  # finished, resurfaced
            {"id": 10, "title": "Fresh Ten", "genre_ids": [], "vote_average": 8.0},
        ]
        ctx.plex.build_library_index.return_value = ({900: 999, 50: 550, 10: 1010}, {})
        ctx.curator.curate.side_effect = curated_picks

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


class TestRequestsWiring:
    """The request pass only runs when enabled, and it sees the titles the library lacks."""

    def _suggest_a_missing_title(self, ctx: EngineContext) -> None:
        # Candidate 30 is NOT in the library index (which holds only 10 and 20), so it's requestable.
        ctx.tmdb.suggestions.return_value = [
            {"id": 10, "title": "In Library", "genre_ids": [], "vote_average": 8.0, "vote_count": 900},
            {"id": 30, "title": "Missing Title", "genre_ids": [], "vote_average": 8.4, "vote_count": 800},
        ]

    def test_disabled_by_default_never_calls_the_request_pass(self, ctx: EngineContext, mock_plextv, monkeypatch):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks
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
        ctx.curator.curate.side_effect = curated_picks
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

        # The missing title reached the request pass; the in-library one did not.
        assert (30, MT.MOVIE) in captured["demand"]
        assert (10, MT.MOVIE) not in captured["demand"]
        assert captured["demand"][(30, MT.MOVIE)].demand == 1
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
        ctx.curator.curate.side_effect = curated_picks
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

        missing = captured["demand"][(30, MT.MOVIE)]
        assert missing.tags == {"sarah", "gems"}  # user tag + the row whose pool surfaced it, not "picked"
        assert missing.demand == 1  # counted once for this user despite multiple rows/pools
        # Distinct-union candidate count spans both pools: {10,20} (similar) and {30} (discover).
        assert report.users[0].counts.candidates == 3


class TestPlacement:
    """Per-row placement (Home / Library / Both) and pin-to-top reach promote() with the right flags."""

    def _pick(self, slug: str):
        from shortlist.engine.models import MediaType, Pick

        return Pick(
            tmdb_id=1, rating_key=10, title="t1", rank=1, reason="", media_type=MediaType.MOVIE, collection_slug=slug
        )

    def test_library_placement_promotes_recommended_only(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        _promote_one(ctx, MagicMock(), RowSpec(slug="x", name_template="", size=10, placement="library"))
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": True,
            "pin_top": False,
        }

    def test_home_placement_with_pin_shows_on_home_and_pins(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec
        from shortlist.engine.pipeline import _promote_one

        _promote_one(ctx, MagicMock(), RowSpec(slug="x", name_template="", size=10, placement="home", pin_top=True))
        assert ctx.plex.promote.call_args.kwargs == {
            "shared": True,
            "home": True,
            "recommended": False,
            "pin_top": True,
        }

    def test_unmatched_collection_falls_back_to_everywhere(self, ctx: EngineContext):
        """A collection whose title we can't map to a row must still be hidden from browse and shown —
        never left half-promoted (browse-visible). The legacy everywhere-visible call is the safe default."""
        from shortlist.engine.pipeline import _promote_one

        collection = MagicMock()
        _promote_one(ctx, collection, None)
        ctx.plex.promote.assert_called_once_with(collection, shared=True)

    def test_undelivered_static_library_only_row_keeps_its_placement(self, ctx: EngineContext):
        """INT-3: a STATIC-titled 'Library only' row that exists but got no picks this run keeps its
        library-only placement — it must NOT fall to the everywhere-visible default and pop onto Home
        for that one run (the promote-phase fallback maps it to its spec by its stable title)."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=10, placement="library")]
        ctx.config.dry_run = False
        section = MagicMock(type="movie", key="1", title="Movies")
        ctx.delivery_sections = [section]
        coll = MagicMock(title=render_row_name("Hidden Gems", user, []) + row_marker(100))  # exists, no picks
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert ctx.plex.promote.call_args.kwargs == {
            "shared": False,
            "home": False,
            "recommended": True,
            "pin_top": False,
        }

    def test_undelivered_library_name_row_maps_each_library_to_its_spec(self, ctx: EngineContext):
        """The default {library_name} row renders a DIFFERENT title per library, so an undelivered but
        still-lingering copy must map to its spec in EACH library — not fall to the everywhere-visible
        default in the libraries the fallback didn't render. Both keep the row's library-only placement."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        tpl = "✨ {library_name} Picked for You"
        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [RowSpec(slug="picked", name_template=tpl, size=10, placement="library")]
        ctx.config.dry_run = False
        movies = MagicMock(type="movie", key="1", title="Movies")
        shows = MagicMock(type="show", key="2", title="TV Shows")
        ctx.delivery_sections = [movies, shows]
        colls = {
            movies: MagicMock(title=render_row_name(tpl, user, [], library_name="Movies") + row_marker(100)),
            shows: MagicMock(title=render_row_name(tpl, user, [], library_name="TV Shows") + row_marker(100)),
        }
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [colls[s]] if s in colls else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        assert ctx.plex.promote.call_count == 2  # each library's lingering row mapped to its spec
        for call in ctx.plex.promote.call_args_list:
            # placement="library" -> hidden from Home, shown only in the library's Recommended shelf.
            assert call.kwargs == {"shared": False, "home": False, "recommended": True, "pin_top": False}

    def test_undelivered_dynamic_titled_row_keeps_the_safe_everywhere_fallback(self, ctx: EngineContext):
        """A {top_seed} row's title can't be predicted without picks, so an un-delivered one keeps the
        hide-everywhere fallback (privacy-safe) rather than risk mis-mapping to the wrong placement."""
        from datetime import UTC, datetime

        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="dyn", name_template="Because you watched {top_seed}", size=10, placement="library")
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        coll = MagicMock(title="Because you watched Dune (from a prior run)")
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(coll, shared=True)  # unmapped dynamic → safe fallback

    def test_fallback_skips_a_row_this_user_is_not_in_the_audience_for(self, ctx: EngineContext):
        """Audience is honoured by the no-picks fallback: a per-person row this user is excluded from
        must never be handed a Home/Library placement for them. It stays on the unmapped safe fallback
        (per-person rows share the same marker, so this audience skip is the ONLY thing protecting it)."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="gems", name_template="Hidden Gems", size=10, placement="library", audience={999})
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        coll = MagicMock(title=render_row_name("Hidden Gems", user, []) + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(coll, shared=True)  # excluded → NOT mapped

    def test_fallback_leaves_shared_rows_to_the_shared_promote_loop(self, ctx: EngineContext):
        """A shared row must never be picked up by the PER-PERSON fallback (it promotes in the separate
        shared loop). Even if a collection under this user's label matched the title the fallback would
        compute, the `spec.shared` skip keeps it on the unmapped safe fallback, not the shared spec's
        Home placement."""
        from datetime import UTC, datetime

        from shortlist.engine.delivery import render_row_name, row_marker
        from shortlist.engine.models import RowSpec, RunReport, UserProfile, UserRunReport, UserType
        from shortlist.engine.pipeline import _promote_phase

        user = UserProfile(username="sarah", plex_account_id=100, user_type=UserType.SHARED, slug="sarah")
        ctx.config.rows = [
            RowSpec(slug="all", name_template="Everyone's Picks", size=10, placement="home", shared=True)
        ]
        ctx.config.dry_run = False
        section = MagicMock()
        ctx.delivery_sections = [section]
        coll = MagicMock(title=render_row_name("Everyone's Picks", user, []) + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [coll] if s is section else []
        report = RunReport(started_at=datetime.now(UTC), users=[UserRunReport(username="sarah", slug="sarah")])

        _promote_phase(ctx, [user], [], filters_ok=True, report=report)

        ctx.plex.promote.assert_called_once_with(coll, shared=True)  # shared spec skipped → NOT mapped

    def test_a_top_seed_row_records_a_placement_title_per_library(self, ctx: EngineContext, mock_plextv):
        """A {top_seed} row spanning two libraries writes a DIFFERENT title in each (each curated from
        its own contents), so promotion must know both — not just the first. The recorded titles must
        match what the collections are actually delivered as, or every library but the first would fall
        back to the legacy everywhere-visible placement."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        movies_4k = MagicMock(type="movie", key="2", title="4K Movies")
        ctx.plex.sections.return_value = [movies, movies_4k]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        # Two seeds; each library holds candidates from a DIFFERENT seed, so its {top_seed} differs:
        # Movies is fed by Fargo (ids 10-15), 4K by Heat (ids 50-55).
        idx_std = {900: 999, 800: 888, **{i: 1000 + i for i in range(10, 16)}}
        idx_4k = {900: 999, 800: 888, **{i: 2000 + i for i in range(50, 56)}}
        ctx.plex.build_library_index.side_effect = lambda sec, ep=None: (idx_std if sec is movies else idx_4k, {})

        def suggestions(tid, mt):
            base = 10 if tid == 900 else 50  # Fargo -> Movies ids, Heat -> 4K ids
            return [{"id": base + i, "title": f"T{base + i}", "genre_ids": [], "vote_average": 8.0} for i in range(6)]

        ctx.tmdb.suggestions.side_effect = suggestions
        ctx.history_source.fetch.return_value = [
            make_watched("Fargo", days_ago=1, rating_key=999),  # tmdb 900
            make_watched("Heat", days_ago=2, rating_key=888),  # tmdb 800
        ]
        ctx.config.rows = [
            RowSpec(slug="picked", name_template="Because you watched {top_seed}", size=5, media="movie")
        ]
        ctx.config.min_history = 1
        ctx.config.candidates_pre_rank = 50
        # Capture the titles delivery actually writes so we can compare to what was recorded.
        delivered_titles: list[str] = []
        ctx.plex.create_collection.side_effect = lambda section, title, items: (
            delivered_titles.append(title) or MagicMock()
        )
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.curator.curate.side_effect = curated_picks

        report = pipeline_mod.run(ctx, [sarah])

        recorded = set(report.users[0].placement_titles)
        # Two libraries with different top seeds -> two distinct titles; the pre-fix code recorded ONE
        # (union) and left the 4K collection unmatched. Every delivered title must be recorded.
        assert len(recorded) == 2, f"expected a distinct title per library, got {recorded}"
        assert set(delivered_titles) == recorded, "recorded titles must match what delivery wrote"
        assert all(slug == "picked" for slug in report.users[0].placement_titles.values())


class _DictCache:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


class TestLibraryScoping:
    """Only the libraries a row targets are read — an unselected/off-type library is never scanned."""

    def test_reads_only_libraries_a_row_targets(self, ctx: EngineContext):
        from shortlist.engine.models import RowSpec

        movies = MagicMock(type="movie", key="1", title="Movies")
        sports = MagicMock(type="movie", key="2", title="Sports")  # unselected by the row below
        shows = MagicMock(type="show", key="3", title="TV Shows")  # wrong media for a movie row
        ctx.config.rows = [RowSpec(slug="m", name_template="Movies", size=5, media="movie", library_keys=["1"])]
        ctx.config.rows_defined = True
        ctx.plex.section_signature.return_value = None  # force a scan (no cache)
        scanned: list[str] = []
        ctx.plex.build_library_index.side_effect = lambda sec, ep=None: scanned.append(str(sec.key)) or ({}, {})

        pipeline_mod._build_indexes(ctx, [make_profile("sarah", account_id=100)], [movies, sports, shows])

        assert scanned == ["1"]  # Movies only — Sports and TV Shows never read
        assert [str(s.key) for s in ctx.delivery_sections] == ["1"]

    def test_unconfigured_run_still_reads_every_library(self, ctx: EngineContext):
        """No rows configured -> the synthesized default row targets everything, so all libraries read."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        shows = MagicMock(type="show", key="2", title="TV Shows")
        ctx.config.rows = []
        ctx.config.rows_defined = False
        ctx.plex.section_signature.return_value = None
        scanned: list[str] = []
        ctx.plex.build_library_index.side_effect = lambda sec, ep=None: scanned.append(str(sec.key)) or ({}, {})

        pipeline_mod._build_indexes(ctx, [make_profile("sarah", account_id=100)], [movies, shows])

        assert sorted(scanned) == ["1", "2"]

    def test_muted_row_cleanup_scans_a_library_the_run_scoped_out(self, ctx: EngineContext):
        """A muted row whose stale copy lives in a de-targeted library is still removed — cleanup scans
        EVERY library, not the run's (targeting-scoped) delivery_sections."""
        from shortlist.engine.delivery import row_marker
        from shortlist.engine.models import CollectionDiff, RowOverride, RowSpec
        from shortlist.engine.rows import _remove_muted_and_retired

        movies = MagicMock(type="movie", key="1", title="Movies")
        old_lib = MagicMock(type="movie", key="2", title="4K Movies")  # row no longer targets this
        ctx.plex.sections.return_value = [movies, old_lib]
        ctx.delivery_sections = [movies]  # this run only scoped in Movies
        ctx.config.rows = [RowSpec(slug="gems", name_template="Hidden Gems", size=5, media="movie", library_keys=["1"])]
        ctx.config.rows_defined = True
        ctx.config.dry_run = False
        sarah = make_profile("sarah", account_id=100, row_overrides={"gems": RowOverride(muted=True)})
        stale = MagicMock(title="Hidden Gems" + row_marker(100))
        ctx.plex.find_owned_collections.side_effect = lambda s, label: [stale] if s is old_lib else []

        _remove_muted_and_retired(ctx, sarah, ctx.config, CollectionDiff())

        ctx.plex.delete_owned_collection.assert_called_once()  # removed from 4K Movies despite the scope


class TestLibraryIndexCache:
    """The cross-run tmdb_id -> ratingKey index cache in _library_index."""

    def _ctx(self, cache):
        ctx = MagicMock()
        ctx.index_cache = cache
        ctx.progress = None  # _emit only logs
        ctx.plex.section_signature.return_value = "100:200"
        ctx.plex.build_library_index.return_value = ({42: 1}, {42: 10})
        return ctx

    def test_unchanged_library_serves_the_cached_index_without_re_scanning(self):
        ctx = self._ctx(_DictCache())
        section = MagicMock(key="1", title="Movies")
        first = pipeline_mod._library_index(ctx, section)
        second = pipeline_mod._library_index(ctx, section)
        assert first == second == ({42: 1}, {42: 10})
        assert ctx.plex.build_library_index.call_count == 1  # second run served from cache

    def test_a_changed_signature_re_scans(self):
        ctx = self._ctx(_DictCache())
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        ctx.plex.section_signature.return_value = "101:200"  # a title was added/removed/edited
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2

    def test_nullcache_always_scans(self):
        ctx = self._ctx(NullCache())
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2

    def test_a_missing_signature_disables_the_cache(self):
        ctx = self._ctx(_DictCache())
        ctx.plex.section_signature.return_value = None  # neither totalSize nor updatedAt available
        section = MagicMock(key="1", title="Movies")
        pipeline_mod._library_index(ctx, section)
        pipeline_mod._library_index(ctx, section)
        assert ctx.plex.build_library_index.call_count == 2


class TestParallelRuns:
    """Stage 3: users processed concurrently, but every Plex write serialized by ctx.write_lock."""

    def _users(self, mock_plextv, names=("sarah", "mike", "canary")):
        users = [make_profile(n, account_id=(i + 1) * 100) for i, n in enumerate(names)]
        mock_plextv.users = [plextv_user((i + 1) * 100, n) for i, n in enumerate(names)]
        return users

    def test_writes_never_run_concurrently_under_the_lock(self, ctx: EngineContext, mock_plextv):
        import threading
        import time

        users = self._users(mock_plextv)
        ctx.concurrency = 3
        ctx.curator.curate.side_effect = curated_picks

        created: dict[str, object] = {}

        def stored_label(collection, label):
            created[label.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.find_owned_collections.side_effect = lambda s, label: (
            [created[label.lower()]] if label.lower() in created else []
        )

        counter = {"now": 0, "max": 0}
        guard = threading.Lock()

        def guarded_create(section, title, items):
            with guard:
                counter["now"] += 1
                counter["max"] = max(counter["max"], counter["now"])
            time.sleep(0.02)  # widen the window a race would slip through
            with guard:
                counter["now"] -= 1
            return MagicMock()

        ctx.plex.create_collection.side_effect = guarded_create

        report = pipeline_mod.run(ctx, users)

        assert all(u.status == "ok" for u in report.users)
        assert ctx.plex.create_collection.call_count == 3  # every user delivered
        assert counter["max"] == 1, "deliver writes ran concurrently — the write_lock is not holding"

    def test_concurrency_preserves_user_order_and_excludes(self, ctx: EngineContext, mock_plextv):
        users = self._users(mock_plextv)
        ctx.concurrency = 3
        ctx.curator.curate.side_effect = curated_picks
        created: dict[str, object] = {}

        def stored_label(collection, label):
            created[label.lower()] = collection
            return label.replace("shortlist", "Shortlist", 1)

        ctx.plex.stored_label.side_effect = stored_label
        ctx.plex.create_collection.side_effect = lambda section, title, items: MagicMock()
        ctx.plex.find_owned_collections.side_effect = lambda s, label: (
            [created[label.lower()]] if label.lower() in created else []
        )

        report = pipeline_mod.run(ctx, users)

        assert [u.slug for u in report.users] == ["sarah", "mike", "canary"]  # input order preserved
        # Each user's share filter excludes the OTHER two users' rows — same privacy result as serial.
        sarah_filters = next(u for u in mock_plextv.users if u.id == 100).filters
        assert "Shortlist_mike" in sarah_filters["filterMovies"]
        assert "Shortlist_canary" in sarah_filters["filterMovies"]
        assert "Shortlist_sarah" not in sarah_filters["filterMovies"]
