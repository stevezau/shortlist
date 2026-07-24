"""Pipeline orchestration: per-user isolation, code-based pick selection, cold start, dry-run,
and the leak-safe ordering (deliver unpromoted → sync filters → promote last)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import shortlist.engine.picker as picker_mod
import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.clients.tmdb import NullCache
from shortlist.engine.models import EngineConfig, MediaType, OwnedRow, Pick, RowOverride, RowSpec
from shortlist.engine.pipeline import EngineContext
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user


def _ranked(items: list[dict], affinity: float = 1.0) -> list[tuple[dict, float]]:
    """`TmdbClient.suggestions` returns (item, affinity) pairs. These tests predate affinity and
    don't exercise it, so everything sits at the neutral top-of-list 1.0."""
    return [(item, affinity) for item in items]


def spy_build_picks(monkeypatch) -> list[list]:
    """Record the candidate pools handed to ``picker.build_picks`` — the code-based pick-selection
    step that replaced the old LLM ``curate`` call. Returns one entry per call: the candidate list
    that row+library was offered. ``build_picks`` still runs for real, so the picks are unchanged.
    """
    calls: list[list] = []
    real = picker_mod.build_picks

    def spy(candidates, k):
        calls.append(list(candidates))
        return real(candidates, k)

    monkeypatch.setattr(picker_mod, "build_picks", spy)
    return calls


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
    plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020}
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []  # delivery finds by title; promotion enumerates rows
    plex.stored_label.side_effect = lambda collection, label: label.replace("shortlist", "Shortlist", 1)
    plex.fetch_items.side_effect = lambda keys: [fake_media_item(k, f"item{k}") for k in keys]

    history = MagicMock()
    history.fetch.return_value = [make_watched("Fargo", days_ago=i, rating_key=999) for i in range(1, 5)]

    # (item, affinity) pairs — see TmdbClient.suggestions. These predate affinity and don't
    # exercise it, so both sit at the neutral top-of-list 1.0.
    mock_tmdb.suggestions.return_value = [
        ({"id": 10, "title": "Candidate Ten", "genre_ids": [], "vote_average": 8.0}, 1.0),
        ({"id": 20, "title": "Candidate Twenty", "genre_ids": [], "vote_average": 7.0}, 1.0),
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


class TestRun:
    def test_happy_path_delivers_syncs_then_promotes(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]

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
        mock_plextv.update_user_filters.side_effect = RuntimeError("plex.tv down")

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        ctx.plex.promote.assert_not_called()

    def test_batched_readback_missing_exclude_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """The per-user read-back moved to one roster read after all writes (RANK 1). A write that
        returns fine but silently doesn't stick must still block promotion: the batched read-back
        finds the exclude missing, sets sync_failed, and nothing is promoted."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        mock_plextv.update_user_filters.side_effect = lambda *a: None  # write returns ok but doesn't persist

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        mock_plextv.update_user_filters.assert_called()  # the write WAS attempted
        assert "read-back missing" in (report.error or "") or any(
            "read-back missing" in (u.error or "") for u in report.users
        )
        ctx.plex.promote.assert_not_called()

    def test_verification_roster_read_raising_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """If the single post-write roster read (used to verify persistence) itself fails, we cannot
        confirm any exclude stuck -> fail safe, nothing promoted. list_users is called twice per run:
        once to build the roster, once to verify; only the second (verify) read raises here."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        calls = {"n": 0}

        def list_users():
            calls["n"] += 1
            if calls["n"] >= 2:  # the verification read-back
                raise RuntimeError("plex.tv roster read failed")
            return mock_plextv.users

        mock_plextv.list_users.side_effect = list_users

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        assert "could not verify filters" in (report.error or "")
        ctx.plex.promote.assert_not_called()

    def test_account_absent_from_verification_roster_blocks_promotion(self, ctx: EngineContext, mock_plextv):
        """A write happens, but the verification roster read-back no longer lists that account (its
        share vanished mid-run) -> its just-merged exclude cannot be confirmed -> fail safe, nothing
        promoted. Reproduces the `remote2 is None -> got=''` branch of the batched verify."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        full = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        mock_plextv.users = full
        calls = {"n": 0}

        def list_users():
            calls["n"] += 1
            if calls["n"] >= 2:  # verification read-back has lost sarah
                return [u for u in full if u.id != 100]
            return full

        mock_plextv.list_users.side_effect = list_users

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert not report.ok
        ctx.plex.promote.assert_not_called()

    def test_on_user_done_fires_once_per_user(self, ctx: EngineContext, mock_plextv):
        """The live-persist hook fires as each user finishes (so the UI fills in person by person),
        with that user's finished report."""
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
        seen: list[tuple[str, str]] = []
        ctx.on_user_done = lambda profile, report: seen.append((profile.slug, report.status))

        pipeline_mod.run(ctx, [sarah, mike])

        assert sorted(slug for slug, _ in seen) == ["mike", "sarah"]
        assert all(status in ("ok", "cold_start", "error") for _, status in seen)

    def test_on_user_done_error_never_sinks_the_run(self, ctx: EngineContext, mock_plextv):
        """A persistence hiccup in the hook must not fail the user or the run — the end-of-run persist
        is the backstop."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ran = []

        def boom(_profile, _report):
            ran.append(True)
            raise RuntimeError("db locked")

        ctx.on_user_done = boom
        report = pipeline_mod.run(ctx, [sarah])

        assert ran  # the hook DID run (and raised)
        assert any(u.slug == "sarah" for u in report.users)  # yet the user is still processed + reported

    def test_one_user_failing_never_stops_the_others(self, ctx: EngineContext, mock_plextv):
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
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

    def test_picks_are_built_in_code_with_because_you_watched_reasons(self, ctx: EngineContext, mock_plextv):
        """There is no LLM curate step: picks are selected and reasoned in code (picker.build_picks).
        A default run delivers a full row whose reasons point back at the seeding history."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

        report = pipeline_mod.run(ctx, [sarah])

        user_report = report.users[0]
        assert user_report.status == "ok"
        assert user_report.counts.picks > 0
        assert user_report.picks[0].reason.startswith("Because you watched")

    def test_a_pool_smaller_than_the_row_delivers_what_it_has_ranked_in_order(self, ctx: EngineContext, mock_plextv):
        """The row size is 5 but only two candidates exist in the library; the row fills to what the
        pool holds (no invented titles), ranked 1..n."""
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]

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

    def test_cold_start_files_a_trace_so_the_how_we_picked_button_appears(self, ctx: EngineContext, mock_plextv):
        # A cold user used to file picks but NO trace, so the run page showed no "How we picked" button
        # and they read as skipped (the reported Cassie bug). The cold path must file a history stage
        # (their thin watches, no seeds — nothing was searched) plus a synthetic cold_start gather.
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.history_source.fetch.return_value = [make_watched("Only One")]
        ctx.history_source.fetch.side_effect = None
        ctx.plex.top_rated.return_value = [(50, fake_media_item(1, "Top Rated", tmdb_id=50))]

        report = pipeline_mod.run(ctx, [sarah])

        trace = report.users[0].trace
        assert trace, "a cold user must file a trace — has_trace gates the 'How we picked' button"
        # History stage present with the honest full count, and NO seeds (nothing was searched from them).
        assert trace["history"]["total"] == 1
        assert trace["seeds"] == []
        # Exactly one synthetic cold_start gather, labelled by media, contributing the delivered picks.
        gathers = trace["gathers"]
        assert [g["pool"] for g in gathers] == ["movie · cold_start"]
        assert gathers[0]["sources"][0] == {
            "source": "cold_start",
            "status": "ok",
            "contributed": 1,
            "detail": "",
        }

    def test_dry_run_makes_zero_plex_writes(self, ctx: EngineContext, mock_plextv):
        ctx.config.dry_run = True
        sarah, mike = make_profile("sarah", account_id=100), make_profile("mike", account_id=200)
        mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]

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

        report = pipeline_mod.run(ctx, [sarah, mike])

        assert report.ok
        assert not any(u.privacy_synced for u in report.users)
        mock_plextv.update_user_filters.assert_not_called()

    def test_no_picks_leaves_existing_row_untouched(self, ctx: EngineContext, mock_plextv):
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        ctx.tmdb.suggestions.return_value = _ranked([])  # nothing suggested -> no candidates

        report = pipeline_mod.run(ctx, [sarah])

        assert report.users[0].counts.picks == 0
        ctx.plex.create_collection.assert_not_called()
        ctx.plex.promote.assert_not_called()


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

    def _movie_row_ctx(self, ctx, freshness, run_day):
        """A single Movies library holding tmdb 10-19, one 'picked' movie row at the given freshness."""
        movies = MagicMock(type="movie", key="1", title="Movies")
        ctx.plex.sections.return_value = [movies]
        ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
        idx = {900: 999, **{i: 1000 + i for i in range(10, 20)}}
        ctx.plex.build_library_index.return_value = idx
        pool = [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in range(10, 20)]
        ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked(pool)
        ctx.history_source.fetch.return_value = [make_watched("Fargo", days_ago=1, rating_key=999)]
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie", freshness=freshness)]
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
        self._movie_row_ctx(ctx, freshness=0.0, run_day=5)
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built == []  # reused, not rebuilt
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        assert [p["tmdb_id"] for p in picks] == [12, 13, 14, 15, 16]  # exactly last run's row, in order

    def test_refresh_night_keeps_the_strong_top_and_swaps_the_rest(self, ctx: EngineContext, mock_plextv, monkeypatch):
        """On a refresh night the strongest ~two-thirds carry over and the rest are swapped for titles
        NOT already in the row, so a just-rotated-out pick can't immediately bounce back."""
        self._movie_row_ctx(ctx, freshness=1.0, run_day=5)  # 1.0 = refresh every night
        ctx.previous_picks = {("sarah", "picked", "1"): self._prior_movies([12, 13, 14, 15, 16])}
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        built = spy_build_picks(monkeypatch)

        report = pipeline_mod.run(ctx, [sarah])

        assert built  # a refresh night DOES rebuild the swapped-in slots
        picks = next(e for e in report.users[0].breakdown if e["library_title"] == "Movies")["picks"]
        ids = [p["tmdb_id"] for p in picks]
        assert ids[:3] == [12, 13, 14], f"keep the strongest two-thirds of last run's row, got {ids}"
        assert set(ids[3:]).isdisjoint({12, 13, 14, 15, 16}), f"swapped slots are genuinely new, got {ids}"

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

    def test_per_person_tokens_come_from_the_web_search_source_and_land_under_its_step(
        self, ctx: EngineContext, mock_plextv
    ):
        """The ONLY AI cost now is finding titles: the ``llm_web`` source. A run using it records that
        source's tokens into the user total AND under its own step bucket. Ranking/pick selection is
        code (picker.build_picks) with no LLM, so there is no 'curate' step and no per-row token spend."""

        class _WebCurator:
            supports_native_web_search = True
            last_tokens = 50  # the tokens the one web-search LLM call reports

            def recommend_web(self, profile, seeds, k):
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
        # Cancel becomes true after the first per-user check: sarah delivers, mike (and shared) skip.
        seen = {"n": 0}

        def cancelled() -> bool:
            seen["n"] += 1
            return seen["n"] > 1

        ctx.cancelled = cancelled

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

        # The missing title reached the request pass; the in-library one did not.
        assert (30, MT.MOVIE) in captured["demand"]
        assert (10, MT.MOVIE) not in captured["demand"]
        assert captured["demand"][(30, MT.MOVIE)].demand == 1
        # Its provenance names the row per library: a missing MOVIE renders {library_name} as the
        # movie library ("Movies"), so the inbox shows the same name the row is actually called.
        why = captured["demand"][(30, MT.MOVIE)].why
        assert why and why[0].row == "✨ Movies Picked for You"
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
        ctx.plex.build_library_index.side_effect = lambda sec: idx_std if sec is movies else idx_4k

        def suggestions(tid, mt):  # returns (item, affinity) pairs
            base = 10 if tid == 900 else 50  # Fargo -> Movies ids, Heat -> 4K ids
            return _ranked(
                [{"id": base + i, "title": f"T{base + i}", "genre_ids": [], "vote_average": 8.0} for i in range(6)]
            )

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
        ctx.plex.build_library_index.side_effect = lambda sec: scanned.append(str(sec.key)) or {}

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
        ctx.plex.build_library_index.side_effect = lambda sec: scanned.append(str(sec.key)) or {}

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
        ctx.plex.build_library_index.return_value = {42: 1}
        return ctx

    def test_unchanged_library_serves_the_cached_index_without_re_scanning(self):
        ctx = self._ctx(_DictCache())
        section = MagicMock(key="1", title="Movies")
        first = pipeline_mod._library_index(ctx, section)
        second = pipeline_mod._library_index(ctx, section)
        assert first == second == {42: 1}
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


class TestPerDeliveryTimeoutRetry:
    """A PMS timeout retries JUST the idempotent delivery write, NOT the whole user — so a Plex hiccup
    never re-runs the expensive gather + pick selection (the amplifier that made SFLIX run 3
    catastrophic). A delivery that keeps timing out still fails only that user (rule 6 resume-safety)."""

    def _full_movie_pool(self, ctx: EngineContext) -> None:
        """Five in-library candidates for a size-5 row, so ``build_picks`` fires ONCE per section
        (no short-row padding second call) and its call count cleanly reflects the pick work."""
        ctx.config.rows = [RowSpec(slug="picked", name_template="", size=5, media="movie")]
        ids = [10, 11, 12, 13, 14]
        ctx.tmdb.suggestions.return_value = _ranked(
            [{"id": i, "title": f"T{i}", "genre_ids": [], "vote_average": 8.0} for i in ids]
        )
        ctx.plex.build_library_index.return_value = {900: 999, **{i: 1000 + i for i in ids}}

    def test_a_transient_delivery_timeout_retries_only_the_write_not_pick_selection(
        self, ctx: EngineContext, mock_plextv, monkeypatch
    ):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)  # no real backoff waits
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._full_movie_pool(ctx)
        built = spy_build_picks(monkeypatch)

        # Inject the timeout at the actual PMS WRITE (create_collection), NOT at our deliver_rows
        # helper — so real deliver_rows (and its idempotent re-read) runs on BOTH attempts.
        create_calls = {"n": 0}

        def flaky_create(section, title, items):
            create_calls["n"] += 1
            if create_calls["n"] == 1:
                raise requests.exceptions.ReadTimeout("busy PMS on the write")
            return MagicMock()

        ctx.plex.create_collection.side_effect = flaky_create

        report = pipeline_mod.run(ctx, [sarah])

        assert create_calls["n"] == 2  # the write was retried once, against real deliver_rows
        # Pick selection ran ONCE — the retry did not re-run the gather+build (the point of the change).
        assert len(built) == 1
        user = next(u for u in report.users if u.slug == "sarah")
        assert user.status != "error"
        # The retry did not double-count the per-library audit breakdown (idempotent report state).
        assert len(user.breakdown) == 1

    def test_a_persistent_delivery_timeout_fails_only_that_user(self, ctx: EngineContext, mock_plextv, monkeypatch):
        import requests

        from shortlist.engine.clients import plex_pms

        monkeypatch.setattr(plex_pms.time, "sleep", lambda _s: None)
        sarah = make_profile("sarah", account_id=100)
        mock_plextv.users = [plextv_user(100, "sarah")]
        self._full_movie_pool(ctx)
        built = spy_build_picks(monkeypatch)
        ctx.plex.create_collection.side_effect = requests.exceptions.ReadTimeout("down")

        report = pipeline_mod.run(ctx, [sarah])

        assert next(u for u in report.users if u.slug == "sarah").status == "error"
        assert len(built) == 1  # pick selection ran once, was not re-run on the failures
        ctx.plex.promote.assert_not_called()  # nothing delivered -> nothing promoted


class TestCollectionOrderPhase:
    """The deferred, post-promote item-ordering pass: best-effort, never fatal to an already-delivered run."""

    def test_orders_every_collection_with_its_keys_and_survives_a_failure(self, ctx: EngineContext):
        from unittest.mock import MagicMock as MM
        from unittest.mock import call

        from shortlist.engine.pipeline import _collection_order_phase

        c1, c2, c3 = MM(ratingKey=1), MM(ratingKey=2), MM(ratingKey=3)
        # Middle collection's ordering blows up (slow PMS) — the pass must keep going, not raise.
        ctx.plex.order_collection.side_effect = [4, RuntimeError("PMS timed out"), 2]
        _collection_order_phase(ctx, [(c1, [1, 2]), (c2, [3, 4]), (c3, [5, 6])])
        # Each collection ordered with ITS OWN ranked keys, in order (asserts the unpack, not just count).
        ctx.plex.order_collection.assert_has_calls([call(c1, [1, 2]), call(c2, [3, 4]), call(c3, [5, 6])])

    def test_duplicate_collection_from_a_retry_is_ordered_once(self, ctx: EngineContext):
        from unittest.mock import MagicMock as MM

        from shortlist.engine.pipeline import _collection_order_phase

        coll = MM(ratingKey=7)  # a retried user appended the same collection twice
        _collection_order_phase(ctx, [(coll, [1, 2]), (coll, [1, 2])])
        assert ctx.plex.order_collection.call_count == 1  # de-duped by ratingKey

    def test_dry_run_orders_nothing(self, ctx: EngineContext):
        from dataclasses import replace as dc_replace
        from unittest.mock import MagicMock as MM

        from shortlist.engine.pipeline import _collection_order_phase

        ctx.config = dc_replace(ctx.config, dry_run=True)
        _collection_order_phase(ctx, [(MM(ratingKey=1), [1, 2])])
        ctx.plex.order_collection.assert_not_called()

    def test_no_order_work_is_a_noop(self, ctx: EngineContext):
        from shortlist.engine.pipeline import _collection_order_phase

        _collection_order_phase(ctx, [])
        ctx.plex.order_collection.assert_not_called()

    def test_shelf_ordering_off_skips_all_reordering(self, ctx: EngineContext):
        """The agregarr/Kometa coexistence toggle: with manage_shelf_order=False the order phase must
        never touch the Recommended shelf, even when anchors are configured."""
        from types import SimpleNamespace

        from shortlist.engine.models import HubAnchor
        from shortlist.engine.pipeline import _order_phase

        ctx.config.hub_anchors = {"1": HubAnchor(anchor_title="Recently Added Movies", before=False)}
        ctx.config.manage_shelf_order = False
        report = SimpleNamespace(hub_orderings=[])

        _order_phase(ctx, report)

        ctx.plex.order_owned_hubs.assert_not_called()
        assert report.hub_orderings == []
