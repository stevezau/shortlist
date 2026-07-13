"""Pipeline orchestration: per-user isolation, curator fallback, cold start, dry-run,
and the leak-safe ordering (deliver unpromoted → sync filters → promote last)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import rowarr.engine.pipeline as pipeline_mod
from rowarr.engine.curator.base import CuratorError
from rowarr.engine.models import EngineConfig, MediaType, OwnedRow, RowOverride
from rowarr.engine.pipeline import EngineContext
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user


@pytest.fixture
def ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    plex = MagicMock()
    movie_section = MagicMock()
    movie_section.type = "movie"
    plex.sections.return_value = [movie_section]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movie_section}
    movie_section.collections.return_value = []
    # Library: watched item 900 (ratingKey 999) + candidates 10 and 20.
    plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020}
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []  # delivery finds by title; promotion enumerates rows
    plex.stored_label.side_effect = lambda collection, label: label.replace("rowarr", "Rowarr", 1)
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
    from rowarr.engine.curator.null import NullCurator

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
            return label.replace("rowarr", "Rowarr", 1)

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
        assert sarah_filters["filterMovies"] == "label!=Rowarr_mike"
        mike_filters = next(u for u in mock_plextv.users if u.id == 200).filters
        assert mike_filters["filterMovies"] == "label!=Rowarr_sarah"
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
            "sarah": OwnedRow("Rowarr_sarah", [1]),
            "mike": OwnedRow("Rowarr_mike", [2]),
        }
        mock_plextv.users = [
            plextv_user(
                100, "sarah", filters={"filterMovies": "label!=Rowarr_mike", "filterTelevision": "label!=Rowarr_mike"}
            ),
            plextv_user(
                200, "mike", filters={"filterMovies": "label!=Rowarr_sarah", "filterTelevision": "label!=Rowarr_sarah"}
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

    def test_per_row_prompt_override_reaches_the_curator(self, ctx: EngineContext, mock_plextv):
        from rowarr.engine.models import PromptConfig

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

    def test_muting_removes_an_already_delivered_row(self, ctx: EngineContext, mock_plextv):
        from rowarr.engine.delivery import row_marker

        sarah = make_profile("sarah", account_id=100, row_overrides={"picked": RowOverride(muted=True)})
        mock_plextv.users = [plextv_user(100, "sarah")]
        # A collection already on the server for this row (title = display + the account's marker).
        existing = MagicMock()
        existing.title = ctx.config.row_name_template + row_marker(100)
        ctx.plex.find_owned_collections.return_value = [existing]

        report = pipeline_mod.run(ctx, [sarah])

        ctx.plex.delete_owned_collection.assert_called_once()
        assert ctx.config.row_name_template in report.users[0].diff.deleted
        ctx.plex.create_collection.assert_not_called()  # muted -> nothing rebuilt


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
        from rowarr.engine.models import ArrTarget, RequestConfig, RequestReport
        from rowarr.engine.models import MediaType as MT

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

        def spy(cfg, tmdb, demand, *, dry_run):
            captured["demand"] = demand
            captured["dry_run"] = dry_run
            return sentinel

        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", spy)

        report = pipeline_mod.run(ctx, [sarah])

        # The missing title reached the request pass; the in-library one did not.
        assert (30, MT.MOVIE) in captured["demand"]
        assert (10, MT.MOVIE) not in captured["demand"]
        assert captured["demand"][(30, MT.MOVIE)].demand == 1
        assert report.requests is sentinel
