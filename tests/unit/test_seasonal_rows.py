"""Seasonal rows in the engine (discussion #124): names, pools, dormancy, shared rows.

The date arithmetic lives in test_seasons.py. Everything here takes a spec the server has already
resolved — ``seasons`` configured, ``season`` tonight's (or None between seasons) — because the engine
never reads a clock.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.candidates import GatherStats, gather_candidates
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import (
    remove_row,
    render_description,
    render_row_name,
    resolve_row_template,
    row_marker,
)
from shortlist.engine.models import CollectionDiff, EngineConfig, MediaType, Pick, RowSeason, RowSpec, Seed, UserType
from shortlist.engine.placeholders import uses_season
from shortlist.engine.rows import effective_row_sources, row_recipe
from shortlist.engine.seasons import BUILTIN_SEASONS
from tests.conftest import MemorySnapshotStore, fake_media_item, make_profile, make_watched, plextv_user

CHRISTMAS = RowSeason(slug="christmas", name="Christmas", emoji="🎄", anchor=date(2026, 12, 25))
HALLOWEEN = RowSeason(slug="halloween", name="Halloween", emoji="🎃", anchor=date(2026, 10, 31))
SEASONAL_NAME = "{season_emoji} {season} picks"


def seasonal_spec(**overrides) -> RowSpec:
    values = {
        "slug": "seasonal",
        "name_template": SEASONAL_NAME,
        "size": 5,
        "media": "movie",
        "seasons": ["halloween", "christmas"],
        "season": CHRISTMAS,
    }
    return RowSpec(**{**values, **overrides})


class TestTheNameFollowsTheSeason:
    def test_the_season_tonight_fills_the_name(self):
        profile = make_profile("sarah")
        template = resolve_row_template(seasonal_spec(), profile, EngineConfig())
        assert render_row_name(template, profile, []) == "🎄 Christmas picks"

    def test_another_season_renames_the_same_row(self):
        profile = make_profile("sarah")
        template = resolve_row_template(seasonal_spec(season=HALLOWEEN), profile, EngineConfig())
        assert render_row_name(template, profile, []) == "🎃 Halloween picks"

    def test_without_a_season_the_name_cannot_be_rendered(self):
        """Between seasons, and in every server path that renders a title without a run: the collection
        wears whichever season it was last built for, so no title computed here is its title."""
        profile = make_profile("sarah")
        template = resolve_row_template(seasonal_spec(season=None), profile, EngineConfig())
        assert render_row_name(template, profile, []) == ""

    def test_a_fallback_name_cannot_use_a_season_either(self):
        profile = make_profile("sarah")
        assert render_row_name("{top_seed} and more", profile, [], fallback_name="{season} for you") == ""

    def test_a_row_without_seasons_is_untouched(self):
        profile = make_profile("sarah")
        spec = RowSpec(slug="plain", name_template="✨ Picks for {user}", size=5)
        assert resolve_row_template(spec, profile, EngineConfig()) == "✨ Picks for {user}"

    def test_uses_season_spots_either_placeholder(self):
        assert uses_season("{season} picks") is True
        assert uses_season("{season_emoji} picks") is True
        assert uses_season("{top_seed} picks") is False

    def test_a_description_left_with_a_season_placeholder_is_no_description(self):
        profile = make_profile("sarah")
        assert render_description("Films for {season}", profile, [], "Movies") == ""


class TestOtherRowsClaimEverySeasonsTitle:
    def test_a_seasonal_row_claims_the_title_of_every_season(self):
        """Removing a plain row must never take a seasonal row's collection, whichever season it wears — and
        out of season it wears the last one, which tonight's spec cannot render. Every season in the
        catalogue, not only the row's own: a season taken off the row since leaves its title on the
        collection until the next rebuild, and claiming more only ever removes less."""
        from shortlist.engine.delivery import titles_other_rows_build

        section = SimpleNamespace(title="Movies", key="1", type="movie")
        claimed = titles_other_rows_build(
            [section],
            make_profile("sarah"),
            EngineConfig(seasons=dict(BUILTIN_SEASONS)),
            [seasonal_spec(season=None)],
            slug="plain",
        )
        assert claimed == {
            ("1", "🎃 Halloween picks"),
            ("1", "🎄 Christmas picks"),
            ("1", "💘 Valentine's Day picks"),
        }

    def test_a_config_without_a_catalogue_refuses_rather_than_claiming_nothing(self):
        """An empty default once made this claim silently empty, so a plain row's removal could take a seasonal
        row's collection (#137). A config nobody gave a catalogue says so instead."""
        from shortlist.engine.delivery import titles_other_rows_build

        section = SimpleNamespace(title="Movies", key="1", type="movie")
        with pytest.raises(RuntimeError, match=r"EngineConfig\.seasons was not set"):
            titles_other_rows_build(
                [section], make_profile("sarah"), EngineConfig(), [seasonal_spec(season=None)], slug="plain"
            )

    def test_a_config_without_a_catalogue_still_claims_plain_titles(self):
        """Only a name that uses the season needs the catalogue; every other caller keeps working without one."""
        from shortlist.engine.delivery import titles_other_rows_build

        section = SimpleNamespace(title="Movies", key="1", type="movie")
        plain = RowSpec(slug="friday", name_template="Friday", size=5, media="movie")
        claimed = titles_other_rows_build([section], make_profile("sarah"), EngineConfig(), [plain], slug="plain")
        assert claimed == {("1", "Friday")}


class TestRemovingASeasonalRow:
    """A seasonal row's collection wears whichever season it was last built for — possibly not tonight's —
    so, like a `{top_seed}` row, only its delivery-ledger key may identify it for deletion."""

    def _remove(self, collections, spec: RowSpec, delivered_keys):
        deleted: list[str] = []
        section = SimpleNamespace(title="Movies", key="1", type="movie")
        plex = MagicMock()
        plex.find_owned_collections.return_value = collections
        plex.delete_owned_collection.side_effect = lambda c, prefix: deleted.append(c.title)
        remove_row(
            plex,
            make_profile("sarah", slug="sarah"),
            EngineConfig(),
            spec,
            dry_run=False,
            diff=CollectionDiff(),
            sections=[section],
            delivered_keys=delivered_keys,
        )
        return deleted

    def test_it_is_removed_by_its_ledger_key_whatever_season_it_wears(self):
        halloween_copy = SimpleNamespace(title="🎃 Halloween picks" + row_marker(100), ratingKey=4242)
        assert self._remove([halloween_copy], seasonal_spec(), {"1": 4242}) == [halloween_copy.title]

    def test_a_title_match_alone_never_removes_it(self):
        """Tonight's title can be worn by a different row of theirs that happens to render it; without the
        ledger nothing proves the collection is this row's."""
        christmas_copy = SimpleNamespace(title="🎄 Christmas picks" + row_marker(100), ratingKey=4242)
        assert self._remove([christmas_copy], seasonal_spec(), {}) == []

    def test_a_fallback_title_never_removes_it(self):
        lookalike = SimpleNamespace(title="Picked for you" + row_marker(100), ratingKey=7)
        spec = seasonal_spec(season=None, fallback_name="Picked for you")
        assert self._remove([lookalike], spec, {}) == []


def _seed(tmdb_id: int = 1, media_type: MediaType = MediaType.MOVIE) -> Seed:
    return Seed(tmdb_id=tmdb_id, title=f"Seed {tmdb_id}", media_type=media_type, weight=1.0)


class TestTheSeasonSource:
    """The season's titles in the server's libraries, added to a person's pool with a genre-fit weight.

    Measured on a real server: a person's similar-titles pool holds a median of 6 Christmas films, so a
    seasonal row cannot be built by filtering it. And ranked on rating alone, two people's Christmas rows
    overlapped 48%; weighting each title by how well its genres fit their watching cut that to 13%.
    """

    def test_its_titles_join_the_pool_with_no_seed(self, mock_tmdb):
        mock_tmdb.genre_ids_for.return_value = [28, 53]
        items = [{"id": 30, "title": "Violent Night", "genre_ids": [28, 35], "vote_average": 6.5}]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: items, MediaType.SHOW: []})
        (candidate,) = pool
        assert candidate.sources == {"season"}
        assert candidate.seeds == []

    def test_a_title_in_their_genres_outweighs_one_outside_them(self, mock_tmdb):
        """A thriller watcher's Christmas: Die Hard's kind of film before a Christmas romance."""
        mock_tmdb.genre_ids_for.return_value = [28, 53]  # Action, Thriller
        items = [
            {"id": 30, "title": "Violent Night", "genre_ids": [28, 35]},  # half theirs
            {"id": 31, "title": "A Christmas Prince", "genre_ids": [10749, 35]},  # none theirs
            {"id": 32, "title": "Die Hard", "genre_ids": [28, 53]},  # all theirs
        ]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: items, MediaType.SHOW: []})
        affinity = {c.tmdb_id: c.affinity for c in pool}
        assert affinity == {30: 0.75, 31: 0.5, 32: 1.0}

    def test_with_nothing_watched_of_that_kind_there_is_no_opinion(self, mock_tmdb):
        mock_tmdb.genre_ids_for.return_value = [28]
        shows = [{"id": 40, "name": "The Santa Clauses", "genre_ids": [35]}]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: [], MediaType.SHOW: shows})
        assert [(c.tmdb_id, c.media_type, c.affinity) for c in pool] == [(40, MediaType.SHOW, 1.0)]

    def test_a_title_the_similar_search_also_found_keeps_its_seed(self, mock_tmdb):
        mock_tmdb.genre_ids_for.return_value = [28]
        mock_tmdb.suggestions.return_value = [({"id": 30, "title": "Violent Night", "genre_ids": [28]}, 0.9)]
        items = [{"id": 30, "title": "Violent Night", "genre_ids": [28]}]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: items, MediaType.SHOW: []})
        (candidate,) = pool
        assert candidate.sources == {"tmdb_similar", "season"}
        assert [s.tmdb_id for s in candidate.seeds] == [1]

    def test_the_season_never_strengthens_a_match_the_similar_search_already_measured(self, mock_tmdb):
        """Measured live: a kids' animation watcher's weak similar-title match for "Fresh" (a cannibal horror)
        was raised to the season's genre fit and then multiplied by its seed, topping their Halloween row
        over Casper and Hocus Pocus. The season weighs titles only a season found."""
        mock_tmdb.genre_ids_for.return_value = [16, 10751, 35]  # animation, family, comedy
        mock_tmdb.suggestions.return_value = [({"id": 30, "title": "Fresh", "genre_ids": [27, 53, 35]}, 0.4)]
        items = [{"id": 30, "title": "Fresh", "genre_ids": [27, 53, 35]}]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: items, MediaType.SHOW: []})
        (candidate,) = pool
        similar_only = gather_candidates(mock_tmdb, [_seed()])
        assert candidate.affinity == similar_only[0].affinity

    def test_a_genre_lookup_failure_leaves_the_titles_unweighted_rather_than_lost(self, mock_tmdb):
        mock_tmdb.genre_ids_for.side_effect = RuntimeError("TMDB 503")
        items = [{"id": 30, "title": "Violent Night", "genre_ids": [28]}]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: items, MediaType.SHOW: []})
        assert [(c.tmdb_id, c.affinity) for c in pool] == [(30, 1.0)]

    def test_a_genre_name_lookup_failure_leaves_out_that_kind_of_title_and_keeps_the_rest(self, mock_tmdb):
        """Naming a title's genres reads TMDB's genre list for its media type, and those names are what a
        person's excluded genres are checked against. For a type no seed has, that list was never fetched up
        front, and a failed read raised out of the gather and took every season title with it. Unnamed titles
        would slip past an excluded genre, so that type sits the night out; the other type still builds."""

        def genre_names(media_type):
            if media_type is MediaType.SHOW:
                raise RuntimeError("TMDB 503")
            return {28: "Action"}

        mock_tmdb.genre_names.side_effect = genre_names
        mock_tmdb.genre_ids_for.return_value = [28]
        films = [{"id": 30, "title": "Violent Night", "genre_ids": [28]}]
        shows = [{"id": 40, "name": "The Santa Clauses", "genre_ids": [35]}]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: films, MediaType.SHOW: shows})
        assert [(c.tmdb_id, c.genres) for c in pool] == [(30, ["Action"])]

    def test_a_season_left_out_for_want_of_genre_names_counts_as_a_failed_source(self, mock_tmdb):
        """Titles it could not name were left out, so the season offered nothing — because it failed, which the
        all-sources-failed error must say rather than pass off as a season with nothing in it."""

        def genre_names(media_type):
            if media_type is MediaType.SHOW:
                raise RuntimeError("TMDB 503")
            return {28: "Action"}

        mock_tmdb.genre_names.side_effect = genre_names
        mock_tmdb.suggestions.side_effect = RuntimeError("TMDB 503")
        shows = [{"id": 40, "name": "The Santa Clauses", "genre_ids": [35]}]
        with pytest.raises(RuntimeError, match=r"season: .*genre list"):
            gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: [], MediaType.SHOW: shows})

    def test_with_every_other_source_down_and_nothing_in_season_it_fails_like_any_row(self, mock_tmdb):
        """A season with nothing on this server gathered nothing, and nothing is not a working source: with the
        similar search down too, the pool is empty for a reason and must say so, not report a quiet success."""
        mock_tmdb.suggestions.side_effect = RuntimeError("TMDB 503")
        with pytest.raises(RuntimeError, match="every candidate source failed"):
            gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: [], MediaType.SHOW: []})

    def test_the_season_alone_keeps_the_pool_when_the_similar_search_is_down(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = RuntimeError("TMDB 503")
        mock_tmdb.genre_ids_for.return_value = []
        items = [{"id": 30, "title": "Violent Night", "genre_ids": []}]
        pool = gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: items, MediaType.SHOW: []})
        assert [c.tmdb_id for c in pool] == [30]

    def test_the_trace_names_the_source(self, mock_tmdb):
        mock_tmdb.genre_ids_for.return_value = []
        stats = GatherStats()
        items = [{"id": 30, "title": "Violent Night", "genre_ids": []}]
        gather_candidates(mock_tmdb, [_seed()], season_items={MediaType.MOVIE: items, MediaType.SHOW: []}, stats=stats)
        season = next(entry for entry in stats.trace["sources"] if entry["source"] == "season")
        assert season["status"] == "ok" and season["contributed"] == 1


CHRISTMAS_KEYWORDS = "207317|272698|193048|255088|186933|5570|196450|1991|260365"


@pytest.fixture
def ctx(engine_config: EngineConfig, mock_plextv, mock_tmdb, mock_curator) -> EngineContext:
    """One movie library holding the watched seed (900) and films 10, 20, 30, 31.

    TMDB's similar titles for every seed are 10 (not a Christmas film) and 20 (one). The Christmas list is
    20, 30 and 31, plus 77, which the library does not hold.
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
        ({"id": 10, "title": "Not Christmas", "genre_ids": [28], "vote_average": 9.0}, 1.0),
        ({"id": 20, "title": "Die Hard 2", "genre_ids": [28], "vote_average": 7.0}, 1.0),
    ]
    mock_tmdb.genre_names.return_value = {28: "Action", 35: "Comedy"}
    mock_tmdb.genre_ids_for.return_value = [28]

    def christmas_list(media_type, params):
        if media_type is MediaType.MOVIE and params.get("with_keywords") == CHRISTMAS_KEYWORDS:
            return [
                {"id": 20, "title": "Die Hard 2", "genre_ids": [28], "vote_average": 7.0},
                {"id": 30, "title": "Elf", "genre_ids": [35], "vote_average": 6.8},
                {"id": 31, "title": "Violent Night", "genre_ids": [28, 35], "vote_average": 6.5},
                {"id": 77, "title": "Not On This Server", "genre_ids": [28], "vote_average": 8.0},
            ]
        return []

    mock_tmdb.discover_all.side_effect = christmas_list

    def put(account_id, fields):
        for user in mock_plextv.users:
            if user.id == account_id:
                user.filters.update(fields)

    mock_plextv.update_user_filters.side_effect = put
    mock_plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike")]
    engine_config.seasons = dict(BUILTIN_SEASONS)
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


def _picks(report, username: str, row_slug: str) -> list[int]:
    person = next(u for u in report.users if u.username == username)
    return [p.tmdb_id for p in person.picks if p.collection_slug == row_slug]


class TestASeasonalRowInARun:
    def test_it_holds_only_the_seasons_titles_and_reaches_beyond_their_similar_titles(self, ctx):
        ctx.config.rows = [seasonal_spec()]
        report = pipeline_mod.run(ctx, _people())

        picks = _picks(report, "sarah", "seasonal")
        assert 10 not in picks, "a film outside the season reached a seasonal row"
        assert set(picks) == {20, 30, 31}

    def test_the_seasons_list_is_read_once_for_the_whole_run(self, ctx):
        ctx.config.rows = [seasonal_spec(), seasonal_spec(slug="seasonal-two", name_template="{season} too")]
        pipeline_mod.run(ctx, _people())

        christmas_reads = [c for c in ctx.tmdb.discover_all.call_args_list if c.args[1].get("with_keywords")]
        # Films and shows, once each — not once per person per row.
        assert len(christmas_reads) == 2

    def test_the_row_is_named_for_its_season(self, ctx):
        ctx.config.rows = [seasonal_spec()]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert {title for (_library, title) in sarah.placement_titles} == {"🎄 Christmas picks" + row_marker(100)}

    def test_its_picks_say_they_are_for_the_season(self, ctx):
        ctx.config.rows = [seasonal_spec()]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        elf = next(p for p in sarah.picks if p.tmdb_id == 30)
        assert elf.reason == "Right for the season"

    def test_titles_dropped_for_being_out_of_season_say_so_in_the_trace(self, ctx):
        ctx.config.rows = [seasonal_spec()]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        similar = next(s for g in sarah.trace["gathers"] for s in g["sources"] if s["source"] == "tmdb_similar")
        fates = {r["tmdb_id"]: r.get("fate") for q in similar["queries"] for r in q["returned"]}
        assert fates[10] == "not_in_season"

    def test_a_failed_list_leaves_the_seasonal_row_alone_and_the_other_rows_deliver(self, ctx):
        ctx.tmdb.discover_all.side_effect = RuntimeError("TMDB API error HTTP 503 for /discover/movie")
        ctx.config.rows = [seasonal_spec(), RowSpec(slug="plain", name_template="Plain picks", size=5)]
        report = pipeline_mod.run(ctx, _people())

        assert _picks(report, "sarah", "seasonal") == []
        assert _picks(report, "sarah", "plain") != []
        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.status == "ok"

    def test_a_films_row_does_not_count_the_seasons_shows_as_something_to_build_from(self, ctx):
        """The season's shows are not a films row's to offer. Counted, they kept its gather from failing when
        the similar search was down: the pool held only shows, the media filter emptied it, and the row went
        on as if it had had a working source."""
        from shortlist.engine.rows import _candidate_pool
        from shortlist.engine.seasons import SeasonTitles

        ctx.tmdb.suggestions.side_effect = RuntimeError("TMDB 503")
        christmas_show = {"id": 10, "name": "A Christmas Show", "genre_ids": []}
        season = SeasonTitles(
            ids={MediaType.MOVIE: frozenset(), MediaType.SHOW: frozenset({10})},
            in_library={MediaType.MOVIE: [], MediaType.SHOW: [christmas_show]},
        )

        with pytest.raises(RuntimeError, match="every candidate source failed"):
            _candidate_pool(
                ctx,
                [_seed()],
                {MediaType.MOVIE: {900: 999}, MediaType.SHOW: {10: 1010}},
                excluded_genres=set(),
                sources=["tmdb_similar"],
                media="movie",
                season=season,
            )

    def test_ai_web_search_is_not_used_on_a_seasonal_row(self):
        """Its searches are per watched title, not seasonal: nearly all it proposes (and is paid for) would be
        filtered out."""
        assert effective_row_sources(seasonal_spec(), ["tmdb_similar", "llm_web"]) == ("tmdb_similar",)
        assert effective_row_sources(seasonal_spec(candidate_sources=["llm_web"]), ["tmdb_similar"]) == ()

    def test_web_search_stays_on_rows_that_are_not_seasonal(self):
        plain = RowSpec(slug="plain", name_template="Plain", size=5)
        assert effective_row_sources(plain, ["tmdb_similar", "llm_web"]) == ("llm_web", "tmdb_similar")


class TestADormantRow:
    """Between seasons a seasonal row is kept hidden and untouched until its next season."""

    def _dormant(self) -> RowSpec:
        return seasonal_spec(season=None, placement="off", placement_friends="off")

    def test_it_reads_no_list_and_builds_nothing(self, ctx):
        ctx.config.rows = [self._dormant()]
        report = pipeline_mod.run(ctx, _people())

        assert ctx.tmdb.discover_all.called is False
        assert ctx.tmdb.suggestions.called is False
        assert ctx.plex.create_collection.called is False
        assert _picks(report, "sarah", "seasonal") == []

    def test_the_run_says_it_is_out_of_season(self, ctx):
        ctx.config.rows = [self._dormant()]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.rows_considered == {"seasonal": "out_of_season"}
        assert sarah.status == "skipped"
        assert "out of season" in sarah.reason

    def test_its_collection_is_still_hidden_by_the_run(self, ctx):
        """Someone whose only row is out of season delivers nothing, and a person who delivers nothing was
        never promoted — so the row they had in season kept showing all year."""
        halloween_copy = SimpleNamespace(title="🎃 Halloween picks" + row_marker(100), ratingKey=4242, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [halloween_copy] if label.lower() == "shortlist_sarah" else []
        )
        ctx.delivered_keys = {("sarah", "seasonal", "1"): 4242}
        ctx.config.rows = [self._dormant()]

        pipeline_mod.run(ctx, _people())

        ctx.plex.promote.assert_any_call(halloween_copy, shared=False, home=False, recommended=False)

    def test_other_rows_still_build_beside_it(self, ctx):
        ctx.config.rows = [self._dormant(), RowSpec(slug="plain", name_template="Plain picks", size=5)]
        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.rows_considered == {"seasonal": "out_of_season", "plain": "due"}
        assert _picks(report, "sarah", "plain") != []

    def test_a_run_scoped_to_other_rows_leaves_it_to_its_own_runs(self, ctx):
        """Between seasons a dormant row is hidden by its own runs and the midnight pass. Counting it in every
        scoped run re-promoted each person's whole shelf for months, for a row already off."""
        halloween_copy = SimpleNamespace(title="🎃 Halloween picks" + row_marker(100), ratingKey=4242, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [halloween_copy] if label.lower() == "shortlist_sarah" else []
        )
        ctx.delivered_keys = {("sarah", "seasonal", "1"): 4242}
        ctx.config.rows = [self._dormant()]
        ctx.config.build_only = frozenset({"another-row"})

        pipeline_mod.run(ctx, _people())

        assert ctx.plex.promote.called is False

    def test_its_own_scheduled_run_hides_it(self, ctx):
        """Every row has its own cron, so every scheduled run is scoped: the row's own is one of the runs
        that must still hide it."""
        halloween_copy = SimpleNamespace(title="🎃 Halloween picks" + row_marker(100), ratingKey=4242, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [halloween_copy] if label.lower() == "shortlist_sarah" else []
        )
        ctx.delivered_keys = {("sarah", "seasonal", "1"): 4242}
        ctx.config.rows = [self._dormant()]
        ctx.config.build_only = frozenset({"seasonal"})

        pipeline_mod.run(ctx, _people())

        ctx.plex.promote.assert_any_call(halloween_copy, shared=False, home=False, recommended=False)

    def test_someone_whose_other_rows_all_skip_a_cold_start_still_has_it_hidden(self, ctx):
        halloween_copy = SimpleNamespace(title="🎃 Halloween picks" + row_marker(100), ratingKey=4242, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [halloween_copy] if label.lower() == "shortlist_sarah" else []
        )
        ctx.delivered_keys = {("sarah", "seasonal", "1"): 4242}
        ctx.history_source.fetch.return_value = [make_watched("One Film", days_ago=1, rating_key=999)]
        ctx.config.rows = [self._dormant(), RowSpec(slug="plain", name_template="Plain", size=5, cold_start="skip")]

        pipeline_mod.run(ctx, _people())

        ctx.plex.promote.assert_any_call(halloween_copy, shared=False, home=False, recommended=False)

    def test_a_failed_list_keeps_any_credential_out_of_the_saved_error(self, ctx):
        ctx.tmdb.discover_all.side_effect = RuntimeError(
            "GET https://api.themoviedb.org/3/discover/movie?api_key=SECRETKEY failed"
        )
        ctx.config.rows = [seasonal_spec()]

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert "Christmas list could not be read" in (sarah.error or "")
        assert "SECRETKEY" not in (sarah.error or "") and "SECRETKEY" not in ctx.season_failures["christmas"]

    @pytest.mark.parametrize("thin_history", [False, True], ids=["enough_history", "cold_start"])
    def test_it_is_still_hidden_when_every_row_due_tonight_has_nothing_to_build_from(self, ctx, thin_history):
        """Halloween is over and the Christmas list cannot be read. Failing the person used to take them out of
        promotion altogether, so the Halloween row stayed on their Home: hiding is what this run still owes."""
        halloween_copy = SimpleNamespace(title="🎃 Halloween picks" + row_marker(100), ratingKey=4242, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [halloween_copy] if label.lower() == "shortlist_sarah" else []
        )
        ctx.delivered_keys = {("sarah", "halloween-row", "1"): 4242}
        ctx.tmdb.discover_all.side_effect = RuntimeError("TMDB API error HTTP 503 for /discover/movie")
        if thin_history:
            ctx.history_source.fetch.return_value = [make_watched("One Film", days_ago=1, rating_key=999)]
        ctx.config.rows = [
            seasonal_spec(slug="halloween-row", season=None, placement="off", placement_friends="off"),
            seasonal_spec(),
        ]

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.status == "error"
        assert "Christmas list could not be read" in (sarah.error or "")
        ctx.plex.promote.assert_any_call(halloween_copy, shared=False, home=False, recommended=False)

    def test_it_counts_as_hidden_today_even_if_its_placement_says_otherwise(self):
        """While a row is hidden, a collection promotion cannot identify might BE that row, so it is left alone
        rather than shown. The server makes an out-of-season row `off`; the engine does not rely on that alone."""
        config = EngineConfig(rows=[seasonal_spec(season=None, placement="both")], rows_defined=True)
        assert pipeline_mod.any_row_hidden_today(config) is True

    def test_promotion_hides_it_even_if_its_placement_says_otherwise(self, ctx):
        """The server makes an out-of-season row `off`; the engine does not rely on that alone."""
        collection = SimpleNamespace(title="🎃 Halloween picks" + row_marker(100), ratingKey=4242, labels=[])
        pipeline_mod._promote_one(ctx, collection, seasonal_spec(season=None, placement="both"), UserType.SHARED)
        ctx.plex.promote.assert_called_once_with(collection, shared=False, home=False, recommended=False)

    def test_a_run_with_nobody_in_it_reads_no_list(self, ctx):
        ctx.config.rows = [seasonal_spec()]
        pipeline_mod.run(ctx, [])
        assert ctx.tmdb.discover_all.called is False

    def test_a_run_given_no_catalogue_refuses_to_build_a_seasonal_row(self, ctx):
        """With an empty default every season read as unknown and every seasonal row quietly kept last night's
        picks (#137). A caller that forgot the catalogue is a bug, so it fails loudly."""
        ctx.config.seasons = None
        ctx.config.rows = [seasonal_spec()]
        with pytest.raises(RuntimeError, match=r"EngineConfig\.seasons was not set"):
            pipeline_mod._load_season_titles(ctx, _people(), {})

    def test_a_run_given_no_catalogue_reads_nothing_when_no_row_is_in_season(self, ctx):
        ctx.config.seasons = None
        ctx.config.rows = [seasonal_spec(season=None)]
        pipeline_mod._load_season_titles(ctx, _people(), {})
        assert ctx.season_titles == {} and ctx.season_failures == {}

    def test_a_custom_seasons_collection_missing_tonight_is_logged_and_the_season_still_builds(self, ctx):
        """Kometa deletes its seasonal collections out of season (#137 D5). The run reads the collection from
        the server's own Plex, says it was missing, and builds the season from its picks."""
        from loguru import logger

        from shortlist.engine.seasons import CollectionRef, DateRule, Season

        pat = Season(
            slug="pat",
            name="St Patrick's Day",
            emoji="☘️",
            rule=DateRule("fixed", month=3, day=17),
            description="",
            collections=(CollectionRef("1", "St Patrick's Movies"),),
            picks=((30, MediaType.MOVIE),),
        )
        ctx.config.seasons = {**BUILTIN_SEASONS, "pat": pat}
        ctx.config.rows = [
            seasonal_spec(seasons=["pat"], season=RowSeason("pat", pat.name, pat.emoji, date(2027, 3, 17)))
        ]
        ctx.plex.collection_members.return_value = None
        ctx.tmdb.list_item.side_effect = lambda tmdb_id, media_type: {"id": tmdb_id, "title": "Brooklyn"}
        lines: list[str] = []
        sink = logger.add(lines.append, level="INFO", format="{message}")
        try:
            pipeline_mod._load_season_titles(ctx, _people(), {MediaType.MOVIE: {30: 1030}, MediaType.SHOW: {}})
        finally:
            logger.remove(sink)

        ctx.plex.collection_members.assert_called_once_with("1", "St Patrick's Movies")
        assert ctx.season_failures == {}
        assert ctx.season_titles["pat"].contains(30, MediaType.MOVIE)
        assert any("St Patrick's Day list: built without “St Patrick's Movies”" in line for line in lines)


class TestWhatDecidesARebuild:
    def _policy(self, ctx, spec: RowSpec):
        from shortlist.engine.rows import RowPolicy, _rating_key_resolver

        return RowPolicy(
            ctx=ctx,
            user=make_profile("sarah", account_id=100),
            cfg=ctx.config,
            specs=[spec],
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )

    def test_next_years_christmas_is_a_new_recipe(self, ctx):
        """Otherwise next December would carry forward last December's row."""
        this_year = seasonal_spec()
        next_year = seasonal_spec(season=RowSeason("christmas", "Christmas", "🎄", date(2027, 12, 25)))
        assert row_recipe(self._policy(ctx, this_year), this_year) != row_recipe(
            self._policy(ctx, next_year), next_year
        )

    def test_a_row_that_is_not_seasonal_keeps_its_recipe_byte_for_byte(self, ctx):
        """An unconditional part would mismatch every stored recipe and rebuild every row on the night this
        shipped — the churn the cadence exists to prevent."""
        plain = RowSpec(slug="plain", name_template="Plain", size=5)
        assert "season" not in row_recipe(self._policy(ctx, plain), plain)

    def test_recipe_carries_the_hash_for_custom_seasons_only(self, ctx):
        """A custom season's sources are in its hash, so editing them rebuilds its rows (#137 D11). A built-in's
        recipe part stays exactly what it was before custom seasons, so no existing row rebuilds (D2)."""
        built_in = seasonal_spec()
        custom = seasonal_spec(
            seasons=["pat"],
            season=RowSeason("pat", "St Patrick's Day", "☘️", date(2027, 3, 17), content_hash="abc"),
        )
        assert row_recipe(self._policy(ctx, built_in), built_in).endswith("season=christmas@2026-12-25")
        assert "season=pat@2027-03-17#abc" in row_recipe(self._policy(ctx, custom), custom)

    def test_rows_following_different_seasons_never_share_a_pool(self, ctx):
        christmas = seasonal_spec()
        halloween = seasonal_spec(season=HALLOWEEN)
        policy = self._policy(ctx, christmas)
        assert policy.pool_key(christmas) != policy.pool_key(halloween)


class TestRequestsFromASeasonalRow:
    def test_the_inbox_names_the_row_as_it_reads_in_its_season(self, ctx, monkeypatch):
        """A request is labelled with the row that surfaced it. The label is filled by hand in
        `_record_demand`, apart from the renderer, so it has to get the season filled too."""
        from shortlist.engine.models import ArrTarget, RequestConfig, RequestReport

        ctx.tmdb.suggestions.return_value = [
            ({"id": 77, "title": "Not On This Server", "genre_ids": [28], "vote_average": 8.0}, 1.0),
            ({"id": 20, "title": "Die Hard 2", "genre_ids": [28], "vote_average": 7.0}, 1.0),
        ]
        ctx.config.requests = RequestConfig(
            enabled=True,
            radarr=ArrTarget(url="http://radarr.test", api_key="k", quality_profile_id=1, root_folder="/m"),
        )
        ctx.config.rows = [seasonal_spec()]
        captured = {}

        def spy(cfg, tmdb, demand, *, dry_run, already_handled=None, **kw):
            captured["demand"] = demand
            return RequestReport()

        monkeypatch.setattr(pipeline_mod.requests_mod, "request_missing", spy)

        pipeline_mod.run(ctx, _people())

        (row,) = captured["demand"]
        assert [why.row for why in row.demand[(77, MediaType.MOVIE)].why][:1] == ["🎄 Christmas picks"]


class TestCarryingASeasonalRowForward:
    def test_a_title_no_longer_in_the_season_does_not_carry_forward(self, ctx):
        """A night that keeps last run's picks must not keep one the season list no longer holds: only a new
        season forced a rebuild, so a title TMDB dropped mid-season stayed until the row's next refresh."""
        ctx.previous_picks = {
            ("sarah", "seasonal", "1"): [
                Pick(
                    tmdb_id=t, rating_key=1000 + t, title=f"T{t}", rank=i + 1, reason="kept", media_type=MediaType.MOVIE
                )
                for i, t in enumerate([10, 20, 30])
            ]
        }
        ctx.config.rows = [seasonal_spec(refresh_days=0)]

        report = pipeline_mod.run(ctx, _people())

        delivered = _picks(report, "sarah", "seasonal")
        assert 10 not in delivered
        assert {20, 30} <= set(delivered)


class TestColdStartOnASeasonalRow:
    def test_someone_with_too_little_history_gets_the_seasons_best_rated_titles(self, ctx):
        """Not the server's top-rated films, which are almost never Christmas films."""
        ctx.plex.top_rated.side_effect = lambda section, n: [
            (10, MagicMock(ratingKey=1010, title="Not Christmas")),
        ]
        ctx.history_source.fetch.return_value = [make_watched("One Film", days_ago=1, rating_key=999)]
        ctx.config.rows = [seasonal_spec()]

        report = pipeline_mod.run(ctx, _people())

        assert _picks(report, "sarah", "seasonal") == [20, 30, 31]

    def test_a_failed_list_builds_nothing_for_them(self, ctx):
        ctx.tmdb.discover_all.side_effect = RuntimeError("TMDB API error HTTP 503 for /discover/movie")
        ctx.plex.top_rated.side_effect = lambda section, n: [(10, MagicMock(ratingKey=1010, title="Not Christmas"))]
        ctx.history_source.fetch.return_value = [make_watched("One Film", days_ago=1, rating_key=999)]
        ctx.config.rows = [seasonal_spec()]

        report = pipeline_mod.run(ctx, _people())

        assert _picks(report, "sarah", "seasonal") == []

    def test_a_failed_list_on_their_only_row_reports_them_as_failed_like_someone_with_history(self, ctx):
        """One rule for both paths: a season list that cannot be read is a row whose every source is down. With
        nothing else to build, that is a failed person, not a quiet cold start that explains nothing."""
        ctx.tmdb.discover_all.side_effect = RuntimeError("TMDB API error HTTP 503 for /discover/movie")
        ctx.history_source.fetch.return_value = [make_watched("One Film", days_ago=1, rating_key=999)]
        ctx.config.rows = [seasonal_spec()]

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.status == "error"
        assert "Christmas list could not be read" in (sarah.error or "")
        assert ctx.plex.top_rated.called is False

    def test_a_failed_list_beside_another_row_still_builds_that_row(self, ctx):
        ctx.tmdb.discover_all.side_effect = RuntimeError("TMDB API error HTTP 503 for /discover/movie")
        ctx.plex.top_rated.side_effect = lambda section, n: [(10, MagicMock(ratingKey=1010, title="Popular"))]
        ctx.history_source.fetch.return_value = [make_watched("One Film", days_ago=1, rating_key=999)]
        ctx.config.rows = [seasonal_spec(), RowSpec(slug="plain", name_template="Plain picks", size=5)]

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.status == "cold_start"
        assert _picks(report, "sarah", "plain") != []
        assert _picks(report, "sarah", "seasonal") == []


class TestARewatchSeasonalRow:
    def test_the_finished_titles_it_leads_with_are_from_the_season(self, ctx):
        """ "Christmas films you've seen" — a finished film outside the season must not lead it."""
        ctx.history_source.fetch.return_value = [
            make_watched("Elf", days_ago=400, rating_key=1030, tmdb_id=30),
            make_watched("Not Christmas", days_ago=400, rating_key=1010, tmdb_id=10),
            *[make_watched("Die Hard", days_ago=i, rating_key=999) for i in range(1, 5)],
        ]
        ctx.config.rows = [seasonal_spec(rewatch=True, watched_pct=1.0)]

        report = pipeline_mod.run(ctx, _people())

        picks = _picks(report, "sarah", "seasonal")
        assert 10 not in picks
        assert picks[0] == 30


def _shared_people(ctx) -> list:
    """Three people who all watched Elf (30) and the film outside the season (10); two watched 31."""
    elf = make_watched("Elf", days_ago=2, rating_key=1030, tmdb_id=30)
    other = make_watched("Not Christmas", days_ago=2, rating_key=1010, tmdb_id=10)
    violent = make_watched("Violent Night", days_ago=2, rating_key=1031, tmdb_id=31)
    ctx.plextv.users = [plextv_user(100, "sarah"), plextv_user(200, "mike"), plextv_user(300, "amy")]
    people = [
        make_profile("sarah", account_id=100),
        make_profile("mike", account_id=200),
        make_profile("amy", account_id=300),
    ]
    for person, history in zip(people, ([elf, other, violent], [elf, other, violent], [elf, other]), strict=True):
        person.history = history
    return people


class TestASharedSeasonalRow:
    def _shared(self, **overrides) -> RowSpec:
        return seasonal_spec(slug="season-shared", shared=True, min_watchers=2, **overrides)

    def test_it_counts_only_the_seasons_titles(self, ctx):
        ctx.config.rows = [self._shared()]
        report = pipeline_mod.run(ctx, _shared_people(ctx))

        entries = [e for u in report.users for e in u.breakdown if e["row_slug"] == "season-shared"]
        assert [p["tmdb_id"] for p in entries[0]["picks"]] == [30, 31]

    def test_out_of_season_it_is_hidden_by_a_run_scoped_to_other_rows_too(self, ctx):
        """A shared row's backstop was only a full run where it was due: a scoped run or a "Run now" for one
        person left a shared Christmas row up into January."""
        collection = SimpleNamespace(title="🎃 Halloween picks" + row_marker(0), ratingKey=5151, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [collection] if label == "shortlist__shared_season-shared" else []
        )
        ctx.config.rows = [
            self._shared(season=None, placement="off", placement_friends="off"),
            RowSpec(slug="plain", name_template="Plain", size=5),
        ]
        ctx.config.build_only = frozenset({"plain"})
        ctx.config.users_scoped = True

        pipeline_mod.run(ctx, _shared_people(ctx)[:1])

        ctx.plex.promote.assert_any_call(collection, shared=False, home=False, recommended=False)

    def test_a_failed_list_skips_it_and_says_why(self, ctx):
        ctx.tmdb.discover_all.side_effect = RuntimeError("TMDB API error HTTP 503 for /discover/movie")
        ctx.config.rows = [self._shared()]
        report = pipeline_mod.run(ctx, _shared_people(ctx))

        shared = next(u for u in report.users if u.slug == "shared_season-shared")
        assert shared.status == "skipped"
        # A Plex collection a custom season names can fail it too, so the owner is not told TMDB failed.
        assert shared.reason == (
            "The Christmas films could not be read tonight, so this seasonal row was left as it was. "
            "It rebuilds on the next run that can read them."
        )

    def test_out_of_season_it_is_hidden_not_built(self, ctx):
        collection = SimpleNamespace(title="🎃 Halloween picks" + row_marker(0), ratingKey=5151, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [collection] if label == "shortlist__shared_season-shared" else []
        )
        ctx.config.rows = [self._shared(season=None, placement="off", placement_friends="off")]

        report = pipeline_mod.run(ctx, _shared_people(ctx))

        assert not any(u.slug == "shared_season-shared" and u.picks for u in report.users)
        assert ctx.tmdb.discover_all.called is False
        ctx.plex.promote.assert_any_call(collection, shared=False, home=False, recommended=False)


#: As `row_season_on` resolves it with the season's own 7-day lead: shown 10-17 March 2027.
PAT = RowSeason(
    slug="pat",
    name="St Patrick's Day",
    emoji="☘️",
    anchor=date(2027, 3, 17),
    content_hash="abc",
    starts=date(2027, 3, 10),
    ends=date(2027, 3, 17),
)


class TestLastSeasonsCollection:
    """A seasonal row with nothing for its new season in a library keeps last season's collection there, under
    last season's title and with last season's films. Promoting it would put "🎄 Christmas picks" on people's
    Home in March, so it is kept hidden like a dormant row until a run builds the new season into it (#137 C-1).
    Hiding is the only write: delivery leaves an empty library alone, and nothing here deletes."""

    @staticmethod
    def _christmas_copy(account: int = 100) -> SimpleNamespace:
        return SimpleNamespace(title="🎄 Christmas picks" + row_marker(account), ratingKey=4242, labels=[])

    def _in_march(self, ctx, collection) -> None:
        """Sarah's row follows St Patrick's Day (with no titles in the library) and Christmas; tonight is St
        Patrick's, and her library still holds the Christmas collection the row last built."""
        from shortlist.engine.seasons import DateRule, Season

        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [collection] if label.lower() == "shortlist_sarah" else []
        )
        ctx.delivered_keys = {("sarah", "seasonal", "1"): 4242}
        pat = Season(
            slug="pat",
            name=PAT.name,
            emoji=PAT.emoji,
            rule=DateRule("fixed", month=3, day=17),
            description="",
            keywords=(209352,),
            lead_days=7,
            after_days=0,
            content_hash="abc",
        )
        ctx.config.seasons = {**BUILTIN_SEASONS, "pat": pat}
        ctx.config.rows = [seasonal_spec(seasons=["pat", "christmas"], season=PAT)]

    @staticmethod
    def _midnight(ctx) -> None:
        """What `rows.visibility` does on the season's first day: promote one row by its ledger key."""
        pipeline_mod.promote_user_rows(
            ctx,
            _people()[0],
            {},
            placement_keys={4242: "seasonal"},
            skip_unmatched=True,
            only_row="seasonal",
            built_for=pipeline_mod.built_seasons(ctx),
        )

    def test_the_midnight_pass_keeps_it_hidden(self, ctx):
        stale = self._christmas_copy()
        self._in_march(ctx, stale)
        ctx.delivered_seasons = {("sarah", "seasonal", "1"): "christmas@2026-12-25"}

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(stale, shared=False, home=False, recommended=False)

    def test_last_years_collection_of_the_same_season_is_kept_hidden_too(self, ctx):
        stale = self._christmas_copy()
        self._in_march(ctx, stale)
        ctx.delivered_seasons = {("sarah", "seasonal", "1"): "pat@2026-03-17"}

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(stale, shared=False, home=False, recommended=False)

    @pytest.mark.parametrize("record", ["ledger", "recipe"])
    def test_a_date_moved_within_the_year_is_still_shown(self, ctx, record):
        """The owner moved St Patrick's from the 14th to the 17th while it was showing. The films were chosen for
        this year's St Patrick's, so the collection is the right one; hiding it until its next build would take
        a correct row off Home. The recipe keeps the full date, so the next run still rebuilds it."""
        fresh = SimpleNamespace(title="☘️ St Patrick's Day picks" + row_marker(100), ratingKey=4242, labels=[])
        self._in_march(ctx, fresh)
        if record == "ledger":
            ctx.delivered_seasons = {("sarah", "seasonal", "1"): "pat@2027-03-14"}
        else:
            ctx.previous_recipes = {("sarah", "seasonal", "1"): "movie|season=pat@2027-03-14#abc"}

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(fresh, shared=True, home=False, recommended=True)

    def test_one_built_for_tonights_season_is_promoted(self, ctx):
        fresh = SimpleNamespace(title="☘️ St Patrick's Day picks" + row_marker(100), ratingKey=4242, labels=[])
        self._in_march(ctx, fresh)
        ctx.delivered_seasons = {("sarah", "seasonal", "1"): "pat@2027-03-17"}

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(fresh, shared=True, home=False, recommended=True)

    def test_with_no_ledger_record_its_picks_recipe_decides(self, ctx):
        """Every collection delivered before the ledger recorded seasons has no record there. The picks it was
        built from still name their season, so last season's collection is not shown on the deploy night."""
        stale = self._christmas_copy()
        self._in_march(ctx, stale)
        ctx.previous_recipes = {
            ("sarah", "seasonal", "1"): "movie|||||False|False|5|1|similar|season=christmas@2026-12-25"
        }

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(stale, shared=False, home=False, recommended=False)

    def test_a_recipe_for_tonights_custom_season_ignores_its_source_hash(self, ctx):
        fresh = SimpleNamespace(title="☘️ St Patrick's Day picks" + row_marker(100), ratingKey=4242, labels=[])
        self._in_march(ctx, fresh)
        ctx.previous_recipes = {
            ("sarah", "seasonal", "1"): "movie|||||False|False|5|1|similar|season=pat@2027-03-17#old"
        }

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(fresh, shared=True, home=False, recommended=True)

    def test_a_collection_built_before_the_row_followed_seasons_is_kept_hidden(self, ctx):
        """Its recipe has no season at all, so its films were not chosen for this one."""
        stale = SimpleNamespace(title="Plain picks" + row_marker(100), ratingKey=4242, labels=[])
        self._in_march(ctx, stale)
        ctx.previous_recipes = {("sarah", "seasonal", "1"): "movie|||||False|False|5|1|similar"}

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(stale, shared=False, home=False, recommended=False)

    def test_with_no_record_at_all_it_is_promoted_as_before(self, ctx):
        """Nothing says what it holds, so nothing proves it stale: a row must not lose its place on the night
        this ships just because the ledger never recorded a season."""
        stale = self._christmas_copy()
        self._in_march(ctx, stale)

        self._midnight(ctx)

        ctx.plex.promote.assert_called_once_with(stale, shared=True, home=False, recommended=True)

    def test_a_run_that_finds_nothing_for_the_new_season_hides_it(self, ctx):
        """Someone whose only row found nothing tonight delivers nothing, and a person who delivers nothing was
        never promoted: last season's collection kept whatever flags it had (a Halloween row handing over to
        Christmas stayed on Home all through Christmas)."""
        stale = self._christmas_copy()
        self._in_march(ctx, stale)
        ctx.delivered_seasons = {("sarah", "seasonal", "1"): "christmas@2026-12-25"}

        report = pipeline_mod.run(ctx, _people())

        assert _picks(report, "sarah", "seasonal") == []
        ctx.plex.promote.assert_any_call(stale, shared=False, home=False, recommended=False)
        assert not [c for c in ctx.plex.promote.call_args_list if c.args[0] is stale and any(c.kwargs.values())]

    def test_a_run_that_builds_the_new_season_shows_what_it_built(self, ctx):
        """The ledger the run starts from still names last season; what the run delivers tonight is what counts."""
        built = MagicMock(ratingKey=5555, labels=[])
        built.title = "🎄 Christmas picks" + row_marker(100)
        ctx.plex.create_collection.return_value = built
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [built] if label.lower() == "shortlist_sarah" and ctx.plex.create_collection.called else []
        )
        ctx.delivered_seasons = {("sarah", "seasonal", "1"): "halloween@2026-10-31"}
        ctx.config.rows = [seasonal_spec()]

        report = pipeline_mod.run(ctx, _people()[:1])

        assert _picks(report, "sarah", "seasonal") != []
        ctx.plex.promote.assert_any_call(built, shared=True, home=False, recommended=True)

    def test_a_shared_rows_last_season_is_kept_hidden(self, ctx):
        stale = SimpleNamespace(title="🎄 Christmas picks" + row_marker(0), ratingKey=5151, labels=[])
        ctx.plex.find_owned_collections.side_effect = lambda section, label: (
            [stale] if label == "shortlist__shared_season-shared" else []
        )
        ctx.delivered_seasons = {("shared_season-shared", "season-shared", "1"): "christmas@2026-12-25"}
        spec = seasonal_spec(slug="season-shared", shared=True, seasons=["pat", "christmas"], season=PAT)

        pipeline_mod.promote_shared_row(ctx, spec, into=set(), built_for=pipeline_mod.built_seasons(ctx))

        ctx.plex.promote.assert_called_once_with(stale, shared=False, home=False, recommended=False)

    def test_what_a_run_delivers_or_removes_replaces_the_ledgers_record(self, ctx):
        from shortlist.engine.models import RunReport, UserRunReport

        ctx.delivered_seasons = {("sarah", "seasonal", "1"): "halloween@2026-10-31", ("sarah", "gone", "1"): "x@1"}
        ctx.previous_recipes = {("mike", "seasonal", "1"): "movie|season=halloween@2026-10-31#h"}
        sarah = UserRunReport(username="sarah", slug="sarah")
        sarah.breakdown = [
            {"row_slug": "seasonal", "library_key": "1", "rating_key": 1, "season": "christmas@2026-12-25"},
            {"row_slug": "plain", "library_key": "1", "rating_key": 2, "season": ""},
        ]
        sarah.removed_deliveries = [{"row_slug": "gone", "library_key": "1"}]
        report = RunReport(started_at=datetime(2026, 12, 1, tzinfo=UTC), users=[sarah])

        assert pipeline_mod.built_seasons(ctx, report) == {
            ("sarah", "seasonal", "1"): "christmas@2026-12-25",
            ("sarah", "plain", "1"): "",
            ("mike", "seasonal", "1"): "halloween@2026-10-31",
        }


def _custom(slug: str, month: int, day: int, *, lead: int, after: int = 0):
    from shortlist.engine.seasons import DateRule, Season

    return Season(
        slug=slug,
        name=slug.title(),
        emoji="🗓️",
        rule=DateRule("fixed", month=month, day=day),
        description="",
        keywords=(1,),
        lead_days=lead,
        after_days=after,
    )


class TestWhichCollectionIsTonightsSeason:
    """#137 C-1, round 3: a collection is built for tonight's season only when its record names the same season
    AND a day inside tonight's window (from the day before it opens to the day it closes). A slug-and-year
    comparison let an earlier date of the same year through: Diwali moved from March to November showed March's
    collection all of November. Every case resolves tonight's season with the real `row_season_on` and asks the
    real guard, `pipeline._unless_built_for_another_season`."""

    @staticmethod
    def _shown(
        day: date, built: str | None, seasons: dict, *, slugs: list[str], lead: int = 30, after: int = 0
    ) -> bool:
        from shortlist.engine.seasons import row_season_on

        catalogue = {**BUILTIN_SEASONS, **seasons}
        tonight = row_season_on(slugs, lead, after, day, catalogue=catalogue)
        assert tonight is not None, "the row is in season on that day"
        spec = seasonal_spec(seasons=slugs, season=tonight)
        guarded = pipeline_mod._unless_built_for_another_season(spec, built, SimpleNamespace(title="row"))
        return not guarded.dormant

    @pytest.mark.parametrize(
        ("day", "built", "season"),
        [
            (date(2026, 10, 30), "diwali@2026-03-01", _custom("diwali", 11, 8, lead=14, after=3)),
            (date(2026, 12, 20), "hols@2026-01-01", _custom("hols", 12, 31, lead=14)),
            # Deleted on 1 Oct and made again under the same slug for 8 Nov: DELETE leaves the ledger's record.
            (date(2026, 10, 30), "diwali@2026-10-01", _custom("diwali", 11, 8, lead=14, after=3)),
        ],
        ids=["march_moved_to_november", "january_moved_to_december", "deleted_and_made_again_for_another_day"],
    )
    def test_an_earlier_day_of_the_same_season_and_year_is_kept_hidden(self, day, built, season):
        # The season alone, so nothing but its own day can hide it: beside Christmas, 20 December would show
        # Christmas and hide the row for the wrong reason.
        assert self._shown(day, built, {season.slug: season}, slugs=[season.slug]) is False

    @pytest.mark.parametrize(
        ("day", "built", "season"),
        [
            (date(2027, 3, 12), "pat@2027-03-14", _custom("pat", 3, 17, lead=7)),
            (date(2026, 12, 30), "ny@2026-12-28", _custom("ny", 1, 5, lead=7, after=3)),
        ],
        ids=["14_moved_to_17_march", "28_december_moved_to_5_january"],
    )
    def test_a_day_moved_within_the_window_is_still_shown(self, day, built, season):
        """The films were chosen for this showing of the season; the recipe's full day rebuilds it next run."""
        assert self._shown(day, built, {season.slug: season}, slugs=[season.slug]) is True

    @pytest.mark.parametrize(
        ("day", "built", "season"),
        [
            (date(2027, 3, 12), "pat@2027-03-17", _custom("pat", 3, 16, lead=7)),
            (date(2027, 6, 13), "june@2027-06-10", _custom("june", 6, 19, lead=7, after=3)),
            (date(2026, 12, 30), "ny@2026-12-28", _custom("ny", 1, 6, lead=7, after=3)),
        ],
        ids=["17_moved_back_to_16_march", "10_moved_to_19_june", "28_december_moved_to_6_january"],
    )
    def test_a_day_moved_by_no_more_than_the_windows_width_is_still_shown(self, day, built, season):
        """Round 4: the record is held when it is within the window's width (lead + after) of tonight's day, either
        side. The one-sided "day before it opens to the day it closes" hid a row moved a day EARLIER when it had
        no days after (the default), and one moved later by more than lead + 1."""
        assert self._shown(day, built, {season.slug: season}, slugs=[season.slug]) is True

    @pytest.mark.parametrize("day", [date(2027, 1, 1), date(2027, 1, 2)], ids=["1_january", "2_january"])
    def test_new_years_eve_staying_into_january_shows_its_own_collection_and_hides_last_years(self, day):
        """The window crosses New Year: neither tonight's year nor the record's year alone can decide it."""
        nye = {"nye": _custom("nye", 12, 31, lead=7, after=2)}
        assert self._shown(day, "nye@2026-12-31", nye, slugs=["nye"]) is True
        assert self._shown(day, "nye@2025-12-31", nye, slugs=["nye"]) is False

    def test_valentines_shown_from_december_shows_its_own_collection_and_hides_last_years(self):
        """A 90-day lead opens February's season in November of the year before."""
        day = date(2026, 12, 20)
        assert self._shown(day, "valentines@2027-02-14", {}, slugs=["valentines"], lead=90) is True
        assert self._shown(day, "valentines@2026-02-14", {}, slugs=["valentines"], lead=90) is False

    @pytest.mark.parametrize(
        ("built", "shown"),
        [(None, True), ("", False), ("pat@", False), ("pat", False), ("christmas@2027-03-17", False)],
        ids=["no_record", "built_while_not_seasonal", "no_date", "no_anchor_at_all", "another_season"],
    )
    def test_a_record_that_names_no_day_of_this_season_keeps_todays_answer(self, built, shown):
        """No record is promoted as it always was; anything else that names no day of tonight's season is not."""
        pat = {"pat": _custom("pat", 3, 17, lead=7)}
        assert self._shown(date(2027, 3, 12), built, pat, slugs=["pat"]) is shown


class TestWhyASeasonalRowBuiltNothing:
    """A seasonal row that builds nothing used to report `ok` with no reason (a real run: a shared New Year's Eve
    row whose audience had watched none of its films together). Thin seasons make it common — Easter holds 5
    films on a 10,000-film server, Mother's Day 2 — so the run report says why, naming the season. Nothing about
    what is built or promoted changes."""

    def _row_alone(self, ctx, **overrides) -> None:
        ctx.config.rows = [seasonal_spec(**overrides)]

    def test_a_season_with_nothing_in_this_rows_libraries_says_so(self, ctx):
        ctx.tmdb.discover_all.side_effect = lambda media_type, params: (
            [{"id": 77, "title": "Not On This Server", "genre_ids": [28], "vote_average": 8.0}]
            if media_type is MediaType.MOVIE and params.get("with_keywords") == CHRISTMAS_KEYWORDS
            else []
        )
        self._row_alone(ctx)

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert _picks(report, "sarah", "seasonal") == []
        assert sarah.reason == "No 🎄 Christmas films are in this row's libraries, so it had nothing to show tonight."

    def test_a_season_they_have_seen_all_of_says_so(self, ctx):
        ctx.history_source.fetch.return_value = [
            make_watched("Die Hard", days_ago=1, rating_key=999),
            make_watched("Die Hard 2", days_ago=2, rating_key=1020, tmdb_id=20),
            make_watched("Elf", days_ago=3, rating_key=1030, tmdb_id=30),
            make_watched("Violent Night", days_ago=4, rating_key=1031, tmdb_id=31),
        ]
        self._row_alone(ctx)

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert _picks(report, "sarah", "seasonal") == []
        assert sarah.reason == "No 🎄 Christmas films are left for them — they've seen all 3."

    def test_a_shared_row_no_season_film_was_watched_by_enough_of_says_so(self, ctx):
        elf = make_watched("Elf", days_ago=2, rating_key=1030, tmdb_id=30)
        violent = make_watched("Violent Night", days_ago=2, rating_key=1031, tmdb_id=31)
        other = make_watched("Not Christmas", days_ago=2, rating_key=1010, tmdb_id=10)
        people = _shared_people(ctx)
        for person, history in zip(people, ([elf, other], [violent, other], [other]), strict=True):
            person.history = history
        ctx.config.rows = [seasonal_spec(slug="season-shared", shared=True, min_watchers=2)]

        report = pipeline_mod.run(ctx, people)

        shared = next(u for u in report.users if u.slug == "shared_season-shared")
        assert shared.picks == []
        assert shared.reason == (
            "No 🎄 Christmas film in this row's libraries has been watched by 2 or more of the 3 people in its "
            "audience yet, so it had nothing to show tonight."
        )

    def test_someone_whose_other_row_built_picks_gets_no_reason(self, ctx):
        """The person's reason is read as "why they got nothing": the Runs page shows it in place of their pick
        counts. Their seasonal row being empty beside a row that delivered is not that."""
        ctx.tmdb.discover_all.side_effect = lambda media_type, params: []
        ctx.config.rows = [seasonal_spec(), RowSpec(slug="plain", name_template="Plain picks", size=5)]

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert _picks(report, "sarah", "seasonal") == [] and _picks(report, "sarah", "plain") != []
        assert sarah.reason is None

    def test_two_empty_seasonal_rows_each_name_their_own_season(self, ctx):
        """Joined, two sentences that both said "this row's libraries" could not be told apart."""
        ctx.tmdb.discover_all.side_effect = lambda media_type, params: []
        ctx.config.rows = [seasonal_spec(), seasonal_spec(slug="spooky", season=HALLOWEEN)]

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert sarah.reason == (
            "No 🎄 Christmas films are in the Christmas row's libraries, so it had nothing to show tonight. "
            "No 🎃 Halloween films are in the Halloween row's libraries, so it had nothing to show tonight."
        )

    def test_a_row_that_built_something_gets_no_such_reason(self, ctx):
        self._row_alone(ctx)

        report = pipeline_mod.run(ctx, _people())

        sarah = next(u for u in report.users if u.username == "sarah")
        assert _picks(report, "sarah", "seasonal") != []
        assert sarah.reason is None


class TestSeasonInTheDescriptionAndPoster:
    def test_the_description_takes_the_season(self, ctx):
        ctx.config.rows = [seasonal_spec(description="{season_emoji} {season} films for {user}")]
        pipeline_mod.run(ctx, _people())

        summaries = [call.args[1].get("summary") for call in ctx.plex.edit_collection_fields.call_args_list]
        assert "🎄 Christmas films for sarah" in summaries

    def test_the_poster_text_takes_the_season(self):
        from shortlist.engine.delivery import season_poster
        from shortlist.engine.models import PosterSpec

        spec = seasonal_spec(poster=PosterSpec(mode="text", title="{season}", subtitle="{season_emoji} for {user}"))
        poster = season_poster(spec)
        assert (poster.title, poster.subtitle) == ("Christmas", "🎄 for {user}")

    def test_a_row_without_a_poster_has_none(self):
        from shortlist.engine.delivery import season_poster

        assert season_poster(seasonal_spec()) is None
