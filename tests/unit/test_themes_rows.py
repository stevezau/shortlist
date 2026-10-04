"""The theme source (#138): an AI row fills per person from a theme's titles, with no curator call."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import row_marker
from shortlist.engine.models import EngineConfig, MediaType, Pick, RowLimits, RowSpec
from shortlist.engine.picker import sanitise_ai_reason
from shortlist.engine.placeholders import needs_a_run, uses_theme
from shortlist.engine.rows import RowPolicy, _rating_key_resolver, effective_row_sources, row_recipe
from shortlist.engine.themes import ThemePick, ThemeSpec, load_theme, theme_content_hash
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user

TAG = 555


def theme_spec(**overrides) -> ThemeSpec:
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


def theme_row(theme: ThemeSpec | None = None, **overrides) -> RowSpec:
    values = {
        "slug": "ai-twists",
        "name_template": "{theme_emoji} {theme}",
        "size": 5,
        "media": "movie",
        "theme": theme or theme_spec(),
    }
    return RowSpec(**{**values, **overrides})


@pytest.fixture
def ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    """One movie library holding the watched seed (900) and films 10, 20, 30, 31.

    TMDB's similar titles for every seed are 10 (not in the theme) and 20 (in it). The theme lists 20, 30
    and 31, plus 77, which the library does not hold.
    """
    plex = MagicMock()
    movies = MagicMock(type="movie", key="1", title="Movies")
    movies.collections.return_value = []
    plex.sections.return_value = [movies]
    plex.sections_by_type.return_value = {MediaType.MOVIE: movies}
    plex.build_library_index.return_value = {900: 999, 10: 1010, 20: 1020, 30: 1030, 31: 1031}
    plex.owned_collections.return_value = {}
    plex.find_owned_collections.return_value = []
    plex.stored_label.side_effect = lambda collection, label, *, extra=None: label
    plex.fetch_items.side_effect = lambda keys: ([fake_media_item(k, f"item{k}") for k in keys], [])

    history = MagicMock()
    history.fetch.return_value = [make_watched("Die Hard", days_ago=i, rating_key=999) for i in range(1, 5)]

    mock_tmdb.suggestions.return_value = [
        ({"id": 10, "title": "Not In Theme", "genre_ids": [28], "vote_average": 9.0}, 1.0),
        ({"id": 20, "title": "Die Hard 2", "genre_ids": [28], "vote_average": 7.0}, 1.0),
    ]
    mock_tmdb.genre_names.return_value = {28: "Action", 35: "Comedy"}
    mock_tmdb.genre_ids_for.return_value = [28]

    def theme_list(media_type, params):
        if media_type is MediaType.MOVIE and params.get("with_keywords") == str(TAG):
            return [
                {"id": 20, "title": "Die Hard 2", "genre_ids": [28], "vote_average": 7.0},
                {"id": 30, "title": "Elf", "genre_ids": [35], "vote_average": 6.8},
                {"id": 31, "title": "Violent Night", "genre_ids": [28, 35], "vote_average": 6.5},
                {"id": 77, "title": "Not On This Server", "genre_ids": [28], "vote_average": 8.0},
            ]
        return []

    mock_tmdb.discover_all.side_effect = theme_list

    def put(account_id, fields):
        for user in mock_plextv.users:
            if user.id == account_id:
                user.filters.update(fields)

    mock_plextv.update_user_filters.side_effect = put
    mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
    return EngineContext(
        config=engine_config,
        plex=plex,
        plextv=mock_plextv,
        tmdb=mock_tmdb,
        history_source=history,
        curator=mock_curator,
        snapshots=MemorySnapshotStore(),
    )


def _people() -> list:
    return [make_profile("sarah", account_id=100), make_profile("mike", account_id=200)]


def _picks(report, username: str, row_slug: str):
    person = next(u for u in report.users if u.username == username)
    return [p for p in person.picks if p.collection_slug == row_slug]


def _policy(ctx, spec: RowSpec) -> RowPolicy:
    return RowPolicy(
        ctx=ctx,
        user=make_profile("sarah", account_id=100),
        cfg=ctx.config,
        specs=[spec],
        library_index={},
        report=MagicMock(),
        resolve=_rating_key_resolver({}),
    )


class TestTheRecipe:
    def test_recipe_unchanged_for_rows_without_a_theme(self):
        """Pinned from a row as it stood before themes: a part added unconditionally would rebuild every row."""
        plain = RowSpec(slug="plain", name_template="Plain", size=5)
        policy = RowPolicy(
            ctx=MagicMock(),
            user=make_profile("sarah", account_id=100),
            cfg=EngineConfig(),
            specs=[plain],
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )
        assert row_recipe(policy, plain) == "both||tmdb_similar|0.0|0.0|False|False|30|1|popular"
        assert policy.pool_key(plain) == (("tmdb_similar",), "both", (), True, False, 0, (), "", "")

    def test_recipe_carries_slug_and_content_hash(self, ctx):
        row = theme_row()
        assert row_recipe(_policy(ctx, row), row).endswith(f"theme=twists#{theme_content_hash(row.theme)}")

    def test_recipe_changes_when_theme_content_changes(self, ctx):
        before, after = theme_row(), theme_row(theme_spec(tags=(TAG, 556)))
        assert row_recipe(_policy(ctx, before), before) != row_recipe(_policy(ctx, after), after)

    def test_recipe_does_not_change_when_only_name_changes(self, ctx):
        before, after = theme_row(), theme_row(theme_spec(name="Plot twists", emoji="🔀"))
        assert row_recipe(_policy(ctx, before), before) == row_recipe(_policy(ctx, after), after)

    def test_pool_key_differs_for_two_themes(self, ctx):
        one, two = theme_row(), theme_row(theme_spec(slug="heists", tags=(777,)))
        policy = _policy(ctx, one)
        assert policy.pool_key(one) != policy.pool_key(two)

    def test_pool_key_differs_from_a_row_with_no_theme(self, ctx):
        themed, plain = theme_row(), RowSpec(slug="plain", name_template="Plain", size=5, media="movie")
        assert _policy(ctx, themed).pool_key(themed) != _policy(ctx, themed).pool_key(plain)


class TestTheRow:
    def test_theme_row_pool_limited_to_theme_members_and_ranked_by_genre_coherence(self, ctx):
        ctx.config.rows = [theme_row()]
        report = pipeline_mod.run(ctx, _people())

        ids = [p.tmdb_id for p in _picks(report, "sarah", "ai-twists")]
        assert 10 not in ids and 77 not in ids, "a title outside the theme, or off the server, reached the row"
        assert set(ids) == {20, 30, 31}
        # They watch action: the action title outranks the comedy that shares nothing with it.
        assert ids.index(20) < ids.index(30)

    def test_theme_row_fills_with_no_curator_calls(self, ctx):
        """Web search is in the server's sources, and still never runs for an AI row."""
        ctx.config.candidate_sources = ["tmdb_similar", "llm_web"]
        ctx.search = MagicMock()
        ctx.config.rows = [theme_row()]
        pipeline_mod.run(ctx, _people())

        ctx.curator.complete.assert_not_called()
        assert ctx.search.mock_calls == []
        assert effective_row_sources(theme_row(), ["tmdb_similar", "llm_web"]) == ("tmdb_similar",)

    def test_the_theme_is_read_once_for_the_whole_run(self, ctx):
        ctx.config.rows = [theme_row()]
        with patch.object(pipeline_mod, "load_theme", wraps=load_theme) as loaded:
            pipeline_mod.run(ctx, _people())

        assert loaded.call_count == 1
        assert loaded.call_args.args[0] is ctx.tmdb
        assert loaded.call_args.args[1] is ctx.plex
        assert loaded.call_args.args[2] == ctx.config.rows[0].theme
        assert set(loaded.call_args.args[3][MediaType.MOVIE]) == {900, 10, 20, 30, 31}

    def test_theme_row_keeps_current_picks_when_theme_load_raises(self, ctx):
        prior = [
            Pick(tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE)
            for i, t in enumerate([20, 30])
        ]
        ctx.previous_picks = {("sarah", "ai-twists", "1"): prior}
        row = SimpleNamespace(title="Twist endings" + row_marker(100), ratingKey=5151, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: [row] if label == "shortlist_sarah" else []
        ctx.config.rows = [theme_row(), RowSpec(slug="plain", name_template="Plain picks", size=5, media="movie")]
        with patch.object(pipeline_mod, "load_theme", side_effect=RuntimeError("TMDB 503")):
            report = pipeline_mod.run(ctx, _people())

        assert _picks(report, "sarah", "ai-twists") == []
        assert _picks(report, "sarah", "plain") != []
        assert next(u for u in report.users if u.username == "sarah").status == "ok"
        assert ctx.previous_picks[("sarah", "ai-twists", "1")] == prior
        ctx.plex.delete_owned_collection.assert_not_called()

    def test_seedless_pick_says_it_fits_the_theme_in_genres_they_watch(self, ctx):
        ctx.config.rows = [theme_row()]
        report = pipeline_mod.run(ctx, _people())

        elf = next(p for p in _picks(report, "sarah", "ai-twists") if p.tmdb_id == 30)
        assert elf.reason == "Fits Twist endings"

    def test_seeded_pick_names_the_watch(self, ctx):
        ctx.config.rows = [theme_row()]
        report = pipeline_mod.run(ctx, _people())

        die_hard_2 = next(p for p in _picks(report, "sarah", "ai-twists") if p.tmdb_id == 20)
        assert die_hard_2.reason == "Fits Twist endings — like Die Hard, which you watched"

    def test_theme_pick_reason_includes_ai_line_and_hook(self, ctx):
        theme = theme_spec(picks=(ThemePick(30, MediaType.MOVIE, "ai", "A *holiday* {twist}\nat the end"),))
        ctx.config.rows = [theme_row(theme)]
        report = pipeline_mod.run(ctx, _people())

        elf = next(p for p in _picks(report, "sarah", "ai-twists") if p.tmdb_id == 30)
        assert elf.reason == "A holiday twist at the end · Fits Twist endings"

    def test_theme_pick_reason_truncates_long_ai_reason(self, ctx):
        theme = theme_spec(picks=(ThemePick(30, MediaType.MOVIE, "ai", "word " * 100),))
        ctx.config.rows = [theme_row(theme)]
        report = pipeline_mod.run(ctx, _people())

        elf = next(p for p in _picks(report, "sarah", "ai-twists") if p.tmdb_id == 30)
        assert len(elf.reason) == 160 and elf.reason.endswith("…")


class TestColdStart:
    def test_cold_start_pick_carries_the_theme_wording_and_ai_reason(self, ctx):
        theme = theme_spec(picks=(ThemePick(30, MediaType.MOVIE, "ai", "A festive turn"),))
        ctx.history_source.fetch.return_value = [make_watched("One Film", days_ago=1, rating_key=999)]
        ctx.config.rows = [theme_row(theme)]
        report = pipeline_mod.run(ctx, _people())

        by_id = {p.tmdb_id: p for p in _picks(report, "sarah", "ai-twists")}
        assert by_id[30].reason == "A festive turn \u00b7 Fits Twist endings"
        assert by_id[20].reason == "Fits Twist endings"
        assert by_id[20].sources == ["theme"]


class TestAnEmptyThemeRowSaysSo:
    def test_a_theme_row_with_nothing_unwatched_says_so_rather_than_blaming_the_schedule(self, ctx):
        """The person has seen every title of the theme that the library holds."""
        ctx.history_source.fetch.return_value = [
            make_watched(f"Seen {key}", days_ago=key % 5 + 1, rating_key=key) for key in (999, 1020, 1030, 1031, 1010)
        ]
        ctx.config.rows = [theme_row()]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.picks == []
        assert sarah.reason is not None and "Twist endings" in sarah.reason
        assert "night to rebuild" not in sarah.reason

    def test_a_theme_with_no_title_in_the_library_says_so(self, ctx):
        ctx.config.rows = [theme_row(theme_spec(tags=(TAG,)), library_keys=["1"])]
        ctx.plex.build_library_index.return_value = {900: 999}
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.reason is not None and "are in this row's libraries" in sarah.reason


class TestASharedRowWithATheme:
    def test_it_is_skipped_with_a_warning_never_built_as_an_ordinary_shared_row(self, ctx):
        ctx.config.rows = [theme_row(shared=True, min_watchers=1, slug="ai-shared")]
        report = pipeline_mod.run(ctx, _people())

        shared = next(u for u in report.users if u.username == "Shared \u00b7 ai-shared")
        assert shared.status == "skipped" and "per person" in (shared.reason or "")
        assert shared.picks == []


class TestAiReasonText:
    def test_markdown_braces_and_newlines_are_stripped(self):
        assert sanitise_ai_reason("**Bold** `code` # head\n\n[link](x) {top_seed}") == "Bold code head link(x) top_seed"


class TestTheName:
    def test_theme_title_placeholder_needs_a_run_to_render(self):
        assert uses_theme("{theme_emoji} {theme}") and needs_a_run("{theme} picks")

    def test_the_row_is_named_for_its_theme(self, ctx):
        ctx.config.rows = [theme_row()]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert {title for (_library, title) in sarah.placement_titles} == {"\U0001f300 Twist endings" + row_marker(100)}


class TestAnAiRowNeverRequests:
    def test_an_ai_row_with_a_title_the_server_lacks_produces_no_request_demand(self, ctx, monkeypatch):
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport

        ctx.tmdb.suggestions.return_value = [
            ({"id": 77, "title": "Not On This Server", "genre_ids": [28], "vote_average": 8.0}, 1.0),
            ({"id": 20, "title": "Die Hard 2", "genre_ids": [28], "vote_average": 7.0}, 1.0),
        ]
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
        )
        ctx.config.rows = [theme_row(theme_spec(picks=(ThemePick(77, MediaType.MOVIE, "ai", "Named, not owned"),)))]
        captured: dict = {}

        def spy(cfg, tmdb, demand, *, dry_run, already_handled=None, **kw):
            captured["demand"] = demand
            return RequestReport()

        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", spy)

        pipeline_mod.run(ctx, _people())

        assert [row for row in captured.get("demand", []) if row.demand] == []


class TestOtherRowsClaimAThemedTitle:
    """`remove_row` of one row must never take the collection of an AI row that wears the same title."""

    @staticmethod
    def _plex_with(ai_collection, movies, tv):
        deleted: list[str] = []

        class FakePlex:
            def find_owned_collections(self, section, label):
                return [ai_collection] if section.key == tv.key else []

            def delete_owned_collection(self, collection, prefix):
                deleted.append(collection.title)

            def sections(self):
                return [movies, tv]

        return FakePlex(), deleted

    def test_the_themed_row_claims_its_filled_title(self):
        from shortlist.engine.delivery import titles_other_rows_build

        movies = SimpleNamespace(key="1", title="Movies", type="movie")
        ai = theme_row(theme_spec(emoji=None, name="Twist endings"), name_template="{theme}", media="both")
        claimed = titles_other_rows_build([movies], make_profile("sarah"), EngineConfig(), [ai], "plain")
        assert claimed == {("1", "Twist endings")}

    def test_removing_a_muted_plain_row_leaves_the_same_titled_ai_row(self):
        from shortlist.engine.delivery import remove_row
        from shortlist.engine.models import CollectionDiff

        movies = SimpleNamespace(key="1", title="Movies", type="movie")
        tv = SimpleNamespace(key="2", title="TV Shows", type="show")
        profile = make_profile("sarah")
        plain = RowSpec(slug="plain", name_template="Twist endings", size=10, media="movie")
        ai = theme_row(theme_spec(emoji=None, name="Twist endings"), name_template="{theme}", media="show")
        config = EngineConfig(rows=[plain, ai], rows_defined=True)
        collection = SimpleNamespace(
            title="Twist endings" + row_marker(profile.plex_account_id), ratingKey=900, key="/library/metadata/900"
        )
        plex, deleted = self._plex_with(collection, movies, tv)

        remove_row(
            plex,
            profile,
            config,
            plain,
            dry_run=False,
            diff=CollectionDiff(),
            sections=[movies, tv],
            delivered_keys={},
            other_rows=config.per_person_rows(),
        )

        assert deleted == []

    def test_retiring_a_shared_row_leaves_the_same_titled_ai_row(self):
        from shortlist.engine.delivery import remove_row
        from shortlist.engine.models import CollectionDiff

        movies = SimpleNamespace(key="1", title="Movies", type="movie")
        tv = SimpleNamespace(key="2", title="TV Shows", type="show")
        profile = make_profile("sarah")
        ai = theme_row(theme_spec(emoji="🎄", name="Christmas"), media="both")
        retired = RowSpec(slug="xmas", name_template="🎄 Christmas", size=10, media="both")
        config = EngineConfig(rows=[ai], rows_defined=True, retired_rows=[retired])
        collection = SimpleNamespace(
            title="🎄 Christmas" + row_marker(profile.plex_account_id), ratingKey=900, key="/library/metadata/900"
        )
        plex, deleted = self._plex_with(collection, movies, tv)

        remove_row(
            plex,
            profile,
            config,
            retired,
            dry_run=False,
            diff=CollectionDiff(),
            sections=[movies, tv],
            delivered_keys={},
            other_rows=config.per_person_rows(),
        )

        assert deleted == []
