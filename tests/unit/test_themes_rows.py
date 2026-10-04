"""The theme source (#138): an AI row fills per person from a theme's titles, with no curator call."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import row_marker
from shortlist.engine.models import EngineConfig, MediaType, OverTime, Pick, RowLimits, RowOverride, RowSpec, TitleKey
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

    def test_recipe_is_byte_identical_with_controls_off(self):
        """Pinned from the code before the over-time controls: their part must not exist while they are unset."""
        themed = theme_row()
        policy = _policy(MagicMock(config=EngineConfig()), themed)
        assert row_recipe(policy, themed) == (
            "movie||tmdb_similar|0.0|0.0|False|False|30|1|popular|theme=twists#35ef9e486295e91a56dc34506a26496802107977"
        )
        assert policy.pool_key(themed) == (
            ("tmdb_similar",),
            "movie",
            (),
            True,
            False,
            0,
            (),
            "",
            "",
            "twists",
            "35ef9e486295e91a56dc34506a26496802107977",
        )

    @pytest.mark.parametrize(
        ("over_time", "part"),
        [
            (OverTime(refresh_share=0.5), "over_time=share=0.5"),
            (OverTime(repeat_cooldown_days=30), "over_time=cooldown=30"),
            (OverTime(avoid_rows=("b", "a")), "over_time=avoid=a,b"),
        ],
    )
    def test_each_control_changes_the_recipe_and_clearing_it_restores_it(self, over_time, part):
        plain = theme_row()
        controlled = theme_row(over_time=over_time)
        policy = _policy(MagicMock(config=EngineConfig()), plain)

        assert row_recipe(policy, controlled).endswith(part)
        assert row_recipe(policy, dataclasses.replace(controlled, over_time=OverTime())) == row_recipe(policy, plain)

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


class TestForPerson:
    def test_each_person_gets_their_own_theme_and_everyone_else_the_base(self):
        base, theirs = theme_spec(), theme_spec(slug="heists", name="Heists")
        row = theme_row(base, person_themes=(("sarah", theirs),))

        assert row.for_person("sarah").theme == theirs
        assert row.for_person("mike").theme == base
        assert row.for_person("nobody").theme == base

    def test_for_person_changes_nothing_but_the_theme(self):
        row = theme_row(
            person_themes=(("sarah", theme_spec(slug="heists")),), over_time=OverTime(repeat_cooldown_days=9)
        )

        assert row.for_person("sarah") == dataclasses.replace(row, theme=row.person_themes[0][1])
        assert row.for_person("mike") is row


class FakeHistory:
    def __init__(self, first_shown: set[TitleKey] | None = None):
        self.first_shown = first_shown or set()
        self.calls: list[tuple[str, str, date]] = []

    def first_shown_since(self, user_slug: str, row_slug: str, since: date) -> set[TitleKey]:
        self.calls.append((user_slug, row_slug, since))
        return self.first_shown

    def latest(self, user_slug: str, row_slug: str) -> set[TitleKey]:
        return set()


BIG = list(range(41, 61))  # twenty theme films, all in the library, best-rated first


@pytest.fixture
def big_ctx(ctx):
    """The theme holds BIG; vote_average falls with the id, so the ranking is 41, 42, ... and the pool keeps ten."""
    ctx.plex.build_library_index.return_value = {900: 999, **{t: 1000 + t for t in BIG}}

    def theme_list(media_type, params):
        if media_type is MediaType.MOVIE and params.get("with_keywords") == str(TAG):
            return [
                {"id": t, "title": f"Film {t}", "genre_ids": [28], "vote_average": 9.0 - (t - 41) * 0.1} for t in BIG
            ]
        return []

    ctx.tmdb.discover_all.side_effect = theme_list
    return ctx


def _prior(ids: list[int]) -> list[Pick]:
    return [
        Pick(
            tmdb_id=t,
            rating_key=1000 + t,
            title=f"Film {t}",
            rank=i + 1,
            reason="kept",
            media_type=MediaType.MOVIE,
            collection_slug="ai-twists",
            section_key="1",
            library="Movies",
        )
        for i, t in enumerate(ids)
    ]


def _ids(report, username: str, row_slug: str = "ai-twists") -> list[int]:
    return [p.tmdb_id for p in _picks(report, username, row_slug)]


class TestOverTimeControls:
    def test_refresh_share_changes_how_many_picks_swap(self, big_ctx):
        prior_ids = BIG[:4]
        big_ctx.previous_picks = {("sarah", "ai-twists", "1"): _prior(prior_ids)}
        big_ctx.config.rows = [theme_row(size=4, refresh_days=1, over_time=OverTime(refresh_share=0.5))]
        report = pipeline_mod.run(big_ctx, [make_profile("sarah", account_id=100)])

        ids = _ids(report, "sarah")
        assert set(ids) & set(prior_ids) == set(prior_ids[:2]), "kept titles are the strongest half by rank"
        assert len(ids) == 4

    def test_unset_share_keeps_two_thirds(self, big_ctx):
        big_ctx.previous_picks = {("sarah", "ai-twists", "1"): _prior(BIG[:6])}
        big_ctx.config.rows = [theme_row(size=6, refresh_days=1)]
        report = pipeline_mod.run(big_ctx, [make_profile("sarah", account_id=100)])

        assert set(_ids(report, "sarah")) & set(BIG[:6]) == set(BIG[:4])

    def test_cooldown_drops_recent_titles_from_new_picks(self, big_ctx):
        person = [make_profile("sarah", account_id=100)]
        big_ctx.config.rows = [theme_row(size=5)]
        baseline = _ids(pipeline_mod.run(big_ctx, person), "sarah")
        history = FakeHistory({(MediaType.MOVIE, t) for t in baseline[:2]})
        big_ctx.pick_history = history
        big_ctx.config.rows = [theme_row(size=5, over_time=OverTime(repeat_cooldown_days=14))]
        ids = _ids(pipeline_mod.run(big_ctx, person), "sarah")

        assert len(ids) == 5
        assert not set(ids) & set(baseline[:2])
        assert history.calls
        user_slug, row_slug, since = history.calls[0]
        assert (user_slug, row_slug) == ("sarah", "ai-twists")
        assert (date.today() - since).days == 14

    @pytest.mark.parametrize(
        ("share", "size", "swapped"),
        [(0.05, 5, 1), (1.5, 4, 4), (-0.2, 4, 0), (0.0, 4, 0)],
        ids=["small share swaps one", "above one clamps to all", "negative clamps to none", "zero never swaps"],
    )
    def test_refresh_share_edges(self, big_ctx, share, size, swapped):
        prior_ids = BIG[:size]
        big_ctx.previous_picks = {("sarah", "ai-twists", "1"): _prior(prior_ids)}
        big_ctx.config.rows = [theme_row(size=size, refresh_days=1, over_time=OverTime(refresh_share=share))]
        report = pipeline_mod.run(big_ctx, [make_profile("sarah", account_id=100)])

        assert len(set(_ids(report, "sarah")) - set(prior_ids)) == swapped

    def test_avoid_rows_drops_titles_from_the_named_row(self, big_ctx):
        big_ctx.config.rows = [
            theme_row(slug="ai-a", size=5),
            theme_row(slug="ai-b", size=5, over_time=OverTime(avoid_rows=("ai-a",))),
        ]
        report = pipeline_mod.run(big_ctx, [make_profile("sarah", account_id=100)])

        first, second = _ids(report, "sarah", "ai-a"), _ids(report, "sarah", "ai-b")
        assert len(first) == len(second) == 5
        assert not set(first) & set(second)

    def test_exclusions_skipped_event_field_is_set(self, ctx):
        ctx.pick_history = FakeHistory({(MediaType.MOVIE, t) for t in (20, 30, 31)})
        ctx.config.rows = [theme_row(over_time=OverTime(repeat_cooldown_days=30))]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.exclusions_skipped == ["ai-twists"]
        assert {p.tmdb_id for p in sarah.picks} == {20, 30, 31}

    def test_a_row_without_controls_reports_no_skipped_exclusions(self, ctx):
        ctx.config.rows = [theme_row()]
        report = pipeline_mod.run(ctx, _people())

        assert all(u.exclusions_skipped == [] for u in report.users)


class TestSiblingTitlesUsePersonThemes:
    def test_a_muted_rows_removal_leaves_the_collection_a_sibling_wears_for_this_person(self, ctx):
        """Base theme "Mystery", sarah's own theme "Twist endings": her sibling row's collection is the latter's."""
        ctx.plex.sections.return_value = ctx.plex.sections_by_type.return_value.values()
        collection = SimpleNamespace(
            title="Twist endings" + row_marker(100), ratingKey=5151, key="/library/metadata/5151", labels=[]
        )
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [collection] if label == "shortlist_sarah" else []
        )
        mystery = theme_spec(slug="mystery", name="Mystery", emoji=None)
        twists = theme_spec(slug="twists", name="Twist endings", emoji=None)
        ai = theme_row(mystery, name_template="{theme}", person_themes=(("sarah", twists),))
        plain = RowSpec(slug="plain", name_template="Twist endings", size=5, media="movie")
        ctx.config.rows = [plain, ai]
        sarah = make_profile("sarah", account_id=100, row_overrides={"plain": RowOverride(muted=True)})
        pipeline_mod.run(ctx, [sarah])

        assert ctx.plex.delete_owned_collection.call_args_list == []


class TestPersonThemes:
    @staticmethod
    def _themes_by_tag(ctx, tags_to_ids: dict[str, list[int]]) -> None:
        def theme_list(media_type, params):
            if media_type is not MediaType.MOVIE:
                return []
            return [
                {"id": t, "title": f"T{t}", "genre_ids": [28], "vote_average": 7.0}
                for t in tags_to_ids.get(params.get("with_keywords"), [])
            ]

        ctx.tmdb.discover_all.side_effect = theme_list

    def test_person_theme_resolved_per_person(self, ctx):
        self._themes_by_tag(ctx, {"601": [20, 30, 31], "602": [30]})
        mine, yours = theme_spec(slug="a", tags=(601,)), theme_spec(slug="b", tags=(602,))
        ctx.config.rows = [theme_row(person_themes=(("sarah", mine), ("mike", yours)))]
        report = pipeline_mod.run(ctx, _people())

        assert {p.tmdb_id for p in _picks(report, "sarah", "ai-twists")} == {20, 30, 31}
        assert {p.tmdb_id for p in _picks(report, "mike", "ai-twists")} == {30}
        assert {"a", "b"} <= set(ctx.theme_titles)

    def test_person_theme_is_loaded_once_per_distinct_slug(self, ctx):
        shared = theme_spec(slug="solo", tags=(601,))
        self._themes_by_tag(ctx, {"601": [20, 30]})
        row = RowSpec(
            slug="ai-twists",
            name_template="{theme}",
            size=5,
            media="movie",
            theme=None,
            person_themes=(("sarah", shared), ("mike", shared)),
        )
        ctx.config.rows = [row]
        with patch.object(pipeline_mod, "load_theme", wraps=load_theme) as loaded:
            report = pipeline_mod.run(ctx, _people())

        assert [call.args[2].slug for call in loaded.call_args_list] == ["solo"]
        assert "solo" in ctx.theme_titles
        assert {p.tmdb_id for p in _picks(report, "sarah", "ai-twists")} == {20, 30}

    def test_failing_person_theme_keeps_prior_picks(self, ctx):
        self._themes_by_tag(ctx, {"601": [20, 30], "602": [30, 31]})
        bad, good = theme_spec(slug="bad", tags=(601,)), theme_spec(slug="good", tags=(602,))
        ctx.previous_picks = {("sarah", "ai-twists", "1"): _prior([20, 30])}
        row = SimpleNamespace(title="Twist endings" + row_marker(100), ratingKey=5151, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: [row] if label == "shortlist_sarah" else []
        ctx.config.rows = [theme_row(person_themes=(("sarah", bad), ("mike", good)))]

        def load(tmdb, plex, theme, index):
            if theme.slug == "bad":
                raise RuntimeError("TMDB 503")
            return load_theme(tmdb, plex, theme, index)

        with patch.object(pipeline_mod, "load_theme", side_effect=load):
            report = pipeline_mod.run(ctx, _people())

        assert "bad" in ctx.theme_failures and "good" not in ctx.theme_failures
        assert _picks(report, "sarah", "ai-twists") == []
        assert {p.tmdb_id for p in _picks(report, "mike", "ai-twists")} == {30, 31}

    def test_person_themes_beyond_the_cap_keep_prior_picks(self, ctx, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "_MAX_PERSON_THEMES", 2)
        themes = [theme_spec(slug=f"t{i}", tags=(601,)) for i in range(3)]
        self._themes_by_tag(ctx, {"601": [20, 30]})
        ctx.config.rows = [theme_row(person_themes=tuple(zip(("sarah", "mike", "zed"), themes, strict=True)))]
        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            pipeline_mod.run(ctx, _people())
        finally:
            logger.remove(sink)

        assert set(ctx.theme_failures) == {"t2"}
        assert {"t0", "t1"} <= set(ctx.theme_titles) and "t2" not in ctx.theme_titles
        assert sum("per-run cap" in m for m in messages) == 1


class TestContextBuilderWiring:
    """The context builder hands the engine each person's own current theme, the over-time controls, and the history."""

    @pytest.fixture
    def db(self, tmp_path):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from shortlist.server.db.models import Base

        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        return sessionmaker(engine)

    @pytest.fixture
    def builder(self, db, tmp_path):
        from shortlist.server.services.context_builder import ContextBuilder
        from shortlist.server.services.secrets import SecretBox
        from shortlist.server.services.sse import EventBus

        return ContextBuilder(db, SecretBox(tmp_path), EventBus())

    @staticmethod
    def seed(db, *, mode: str = "explore", **row_fields) -> tuple[int, dict[str, int]]:
        from shortlist.server.db.models import Collection, Theme, User

        with db() as s:
            starter = Theme(slug="starter", name="Starter", media=["movie"], genres=["Drama"])
            s.add(starter)
            users = {
                slug: User(plex_account_id=i + 1, username=slug, slug=slug, enabled=True)
                for i, slug in enumerate(("ann", "bob"))
            }
            s.add_all(users.values())
            s.flush()
            row = Collection(slug="ai-row", name="AI row", theme_id=starter.id, theme_mode=mode, **row_fields)
            s.add(row)
            s.commit()
            return row.id, {slug: u.id for slug, u in users.items()}

    @staticmethod
    def own_theme(db, row_id, user_id, name, *, state="current", genres=("Horror",)):
        from shortlist.server.db.models import Theme, ThemeHistory

        with db() as s:
            theme = Theme(slug=name.lower(), name=name, media=["movie"], genres=list(genres))
            s.add(theme)
            s.flush()
            s.add(
                ThemeHistory(
                    collection_id=row_id,
                    user_id=user_id,
                    theme_id=theme.id,
                    theme_name=name,
                    state=state,
                    started_at=datetime(2026, 10, 1),
                )
            )
            s.commit()

    @staticmethod
    def specs(builder, db):
        from shortlist.server.services.season_catalogue import load_catalogue
        from shortlist.server.settings_store import SettingsStore

        with db() as session:
            store = SettingsStore(session, builder._secrets)
            return {s.slug: s for s in builder._build_rows(session, store, catalogue=load_catalogue(session))}

    def test_each_person_gets_their_own_current_theme(self, builder, db):
        row_id, users = self.seed(db)
        self.own_theme(db, row_id, users["ann"], "Scary")
        self.own_theme(db, row_id, users["bob"], "Cosy", genres=("Family",))

        spec = self.specs(builder, db)["ai-row"]

        assert {slug: theme.name for slug, theme in spec.person_themes} == {"ann": "Scary", "bob": "Cosy"}
        assert spec.for_person("ann").theme.genres == ("Horror",)
        assert spec.for_person("bob").theme.genres == ("Family",)

    def test_a_person_with_no_current_theme_falls_back_to_the_rows_own(self, builder, db):
        row_id, users = self.seed(db)
        self.own_theme(db, row_id, users["ann"], "Scary")

        spec = self.specs(builder, db)["ai-row"]

        assert [slug for slug, _ in spec.person_themes] == ["ann"]
        assert spec.for_person("bob").theme.name == "Starter"

    def test_an_up_next_theme_is_not_used_until_it_is_promoted(self, builder, db):
        from shortlist.server.services.theme_rotation import promote_next

        row_id, users = self.seed(db)
        self.own_theme(db, row_id, users["ann"], "Scary")
        self.own_theme(db, row_id, users["ann"], "Queued", state="next")
        before = self.specs(builder, db)["ai-row"]

        with db() as s:
            promote_next(s, row_id, users["ann"], datetime(2026, 10, 9, tzinfo=UTC))
            s.commit()
        after = self.specs(builder, db)["ai-row"]

        assert before.for_person("ann").theme.name == "Scary"
        assert after.for_person("ann").theme.name == "Queued"

    def test_a_fixed_row_ignores_history(self, builder, db):
        row_id, users = self.seed(db, mode="fixed")
        self.own_theme(db, row_id, users["ann"], "Scary")

        assert self.specs(builder, db)["ai-row"].person_themes == ()

    def test_over_time_is_default_when_the_columns_are_null(self, builder, db):
        self.seed(db)

        assert self.specs(builder, db)["ai-row"].over_time == OverTime()

    def test_over_time_carries_the_columns(self, builder, db):
        self.seed(db, refresh_share=0.5, repeat_cooldown_days=30, avoid_rows=["quiet", "loud"])

        assert self.specs(builder, db)["ai-row"].over_time == OverTime(0.5, 30, ("quiet", "loud"))

    @pytest.mark.parametrize("site", ["build", "build_plex_only"])
    def test_both_engine_contexts_carry_the_pick_history(self, builder, db, monkeypatch, site):
        from shortlist.server.services import context_builder as cb
        from shortlist.server.services.pick_history import DbPickHistory
        from shortlist.server.settings_store import SettingsStore

        with db() as s:
            store = SettingsStore(s, builder._secrets)
            store.set("plex.url", "http://plex.invalid")
            store.set("plex.token", "tok")
            s.commit()
        fake = SimpleNamespace(machine_id="m", _token_for=lambda p: None)
        monkeypatch.setattr(cb, "PlexClient", lambda *a, **k: fake)
        monkeypatch.setattr(cb, "PlexTvClient", lambda *a, **k: fake)
        monkeypatch.setattr(cb, "TmdbClient", lambda *a, **k: fake)
        monkeypatch.setattr(cb, "ShareTokenWatchSource", lambda *a, **k: fake)
        monkeypatch.setattr(cb, "make_curator", lambda *a, **k: fake)
        monkeypatch.setattr(cb, "make_search_client", lambda get: None)
        monkeypatch.setattr(cb, "_refuse_a_different_server", lambda session, machine_id: None)
        monkeypatch.setattr(cb, "EngineContext", lambda **kw: SimpleNamespace(**kw))

        ctx = builder.build(dry_run=True) if site == "build" else builder.build_plex_only(dry_run=True)

        assert isinstance(ctx.pick_history, DbPickHistory)
