"""A row named after one watch builds each of its libraries from that library's own watches (issue #162)."""

# ruff: noqa: F811 -- a test requests the imported fixture by name, which reads as a redefinition
from __future__ import annotations

from unittest.mock import MagicMock

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine import rows as rows_mod
from shortlist.engine.context import EngineContext
from shortlist.engine.delivery import strip_marker
from shortlist.engine.models import MediaType, RowSpec, UserRunReport
from tests.conftest import make_profile, make_watched, plextv_user
from tests.unit.pipeline_support import _ranked, ctx  # noqa: F401

NEW_SHOW, SILICON_VALLEY, ANIME_WATCH = 900, 800, 700


def _show(tmdb_id: int) -> dict:
    return {"id": tmdb_id, "name": f"t{tmdb_id}", "title": f"t{tmdb_id}", "genre_ids": [], "vote_average": 8.0}


def _two_show_libraries(ctx: EngineContext, *, tv: list[int], anime: list[int], lookalikes: dict[int, list[int]]):
    """A "TV Shows" and an "Anime" library, each holding the given titles plus the watches that live there."""
    tv_section = MagicMock(type="show", key="1", title="TV Shows")
    anime_section = MagicMock(type="show", key="2", title="Anime")
    ctx.plex.sections.return_value = [tv_section, anime_section]
    ctx.plex.sections_by_type.return_value = {MediaType.SHOW: tv_section}
    tv_index = {NEW_SHOW: 9900, SILICON_VALLEY: 9800, **{t: t for t in tv}}
    anime_index = {ANIME_WATCH: 9700, **{t: t for t in anime}}
    ctx.plex.build_library_index.side_effect = lambda sec: tv_index if sec is tv_section else anime_index
    ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked([_show(t) for t in lookalikes.get(tid, [])])
    ctx.config.rows = [
        RowSpec(slug="byw", name_template="Because you watched {top_seed}", size=5, media="show", refresh_days=1)
    ]
    ctx.config.min_history = 1


def _run(ctx: EngineContext, mock_plextv) -> UserRunReport:
    mock_plextv.users = [plextv_user(100, "sarah")]
    return pipeline_mod.run(ctx, [make_profile("sarah", account_id=100)]).users[0]


def _titles(user: UserRunReport) -> dict[str, str]:
    return {library: strip_marker(title) for library, title in user.placement_titles}


def _picks_in(user: UserRunReport, library: str) -> list[int]:
    return [p.tmdb_id for p in user.picks if p.library == library]


def test_each_library_is_named_after_a_watch_it_holds(ctx: EngineContext, mock_plextv):
    """The newest watch overall is a TV show whose look-alikes are mostly anime. Built from the ROW's
    watches, the Anime library led with it and was titled after a show it does not hold."""
    _two_show_libraries(
        ctx,
        tv=list(range(10, 20)),
        anime=list(range(50, 60)),
        lookalikes={
            NEW_SHOW: [50, 51, 52, 15, 16, 17],
            ANIME_WATCH: [53, 54, 55, 56, 57, 58],
            SILICON_VALLEY: [10, 11, 12, 13, 14],
        },
    )
    ctx.history_source.fetch.return_value = [
        make_watched("New Show", days_ago=1, media_type=MediaType.SHOW, rating_key=9900),
        make_watched("Anime Watch", days_ago=2, media_type=MediaType.SHOW, rating_key=9700),
        make_watched("Silicon Valley", days_ago=1500, media_type=MediaType.SHOW, rating_key=9800),
    ]

    titles = _titles(_run(ctx, mock_plextv))

    assert titles == {"1": "Because you watched New Show", "2": "Because you watched Anime Watch"}


def test_a_library_is_not_crowded_out_by_its_sibling(ctx: EngineContext, mock_plextv):
    """The pre-rank cut is per media type. Shared by two show libraries, the fresher watch's anime
    took every place and TV Shows got no row at all."""
    _two_show_libraries(
        ctx,
        tv=list(range(10, 15)),
        anime=list(range(50, 60)),
        lookalikes={ANIME_WATCH: list(range(50, 60)), SILICON_VALLEY: list(range(10, 15))},
    )
    ctx.config.candidates_pre_rank = 5
    ctx.history_source.fetch.return_value = [
        make_watched("Anime Watch", days_ago=1, media_type=MediaType.SHOW, rating_key=9700),
        make_watched("Silicon Valley", days_ago=60, media_type=MediaType.SHOW, rating_key=9800),
    ]

    user = _run(ctx, mock_plextv)

    assert sorted(_picks_in(user, "TV Shows")) == [10, 11, 12, 13, 14]
    assert _titles(user)["1"] == "Because you watched Silicon Valley"


def test_a_library_holding_none_of_their_watches_still_gets_a_row(ctx: EngineContext, mock_plextv):
    """Nothing they watched is in TV Shows, so it seeds from all their show watches, as it did before."""
    _two_show_libraries(
        ctx,
        tv=[10, 11, 12],
        anime=list(range(50, 55)),
        lookalikes={ANIME_WATCH: [50, 51, 52, 53, 54, 10, 11, 12]},
    )
    ctx.history_source.fetch.return_value = [
        make_watched("Anime Watch", days_ago=1, media_type=MediaType.SHOW, rating_key=9700),
    ]

    user = _run(ctx, mock_plextv)

    assert sorted(_picks_in(user, "TV Shows")) == [10, 11, 12]
    assert _titles(user)["1"] == "Because you watched Anime Watch"


def test_a_movies_and_tv_row_is_still_picked_as_one(ctx: EngineContext, mock_plextv):
    """A library alone in its type already had its own seeds and cut, so the row is not split: no extra
    gather, and its settings fingerprint (and so tonight's refresh) is what it was."""
    movies = MagicMock(type="movie", key="1", title="Movies")
    tv = MagicMock(type="show", key="2", title="TV Shows")
    ctx.plex.sections.return_value = [movies, tv]
    ctx.plex.sections_by_type.return_value = {MediaType.MOVIE: movies, MediaType.SHOW: tv}
    ctx.plex.build_library_index.side_effect = lambda sec: (
        {101: 9101, **{t: t for t in range(10, 15)}} if sec is movies else {SILICON_VALLEY: 9800}
    )
    ctx.tmdb.suggestions.side_effect = lambda tid, mt: _ranked([_show(t) for t in range(10, 15)] if tid == 101 else [])
    ctx.history_source.fetch.return_value = [
        make_watched("A Film", days_ago=1, media_type=MediaType.MOVIE, rating_key=9101),
        make_watched("Silicon Valley", days_ago=2, media_type=MediaType.SHOW, rating_key=9800),
    ]
    ctx.config.rows = [RowSpec(slug="byw", name_template="Because you watched {top_seed}", size=5, media="both")]
    ctx.config.min_history = 1

    user = _run(ctx, mock_plextv)

    assert [entry["rows"] for entry in user.pool_costs] == [["byw"]]
    assert sorted(_picks_in(user, "Movies")) == [10, 11, 12, 13, 14]


def test_a_library_whose_sources_are_down_leaves_the_other_built(ctx: EngineContext, mock_plextv, monkeypatch):
    """Each split library gathers on its own, so one failing is that library's problem: it is left as it is
    (not removed), and its sibling is still delivered."""
    _two_show_libraries(
        ctx,
        tv=list(range(10, 15)),
        anime=list(range(50, 55)),
        lookalikes={ANIME_WATCH: list(range(50, 55)), SILICON_VALLEY: list(range(10, 15))},
    )
    ctx.history_source.fetch.return_value = [
        make_watched("Anime Watch", days_ago=1, media_type=MediaType.SHOW, rating_key=9700),
        make_watched("Silicon Valley", days_ago=60, media_type=MediaType.SHOW, rating_key=9800),
    ]
    real_gather = rows_mod._gather_pool

    def anime_is_down(ctx_, seeds, library_index, **kwargs):
        if any(seed.tmdb_id == ANIME_WATCH for seed in seeds):
            raise RuntimeError("tmdb is down")
        return real_gather(ctx_, seeds, library_index, **kwargs)

    monkeypatch.setattr(rows_mod, "_gather_pool", anime_is_down)

    user = _run(ctx, mock_plextv)

    assert sorted(_picks_in(user, "TV Shows")) == [10, 11, 12, 13, 14]
    assert _picks_in(user, "Anime") == []
    assert not ctx.plex.delete_owned_collection.called


def test_a_new_watch_in_one_library_does_not_rebuild_its_sibling_as_a_settings_change(ctx: EngineContext, mock_plextv):
    """A split library's seed budget is its type's share of the row's, and that share moves as they watch.
    It is not a setting: TV Shows, whose watches did not change, must refresh as usual tomorrow, not throw
    its row away as "settings_changed"."""
    second_anime = 701
    _two_show_libraries(
        ctx,
        tv=list(range(10, 20)),
        anime=list(range(50, 60)),
        lookalikes={
            ANIME_WATCH: list(range(50, 56)),
            SILICON_VALLEY: list(range(10, 16)),
            second_anime: list(range(54, 60)),
        },
    )
    tv_section, _anime_section = ctx.plex.sections.return_value
    tv_index = {SILICON_VALLEY: 9800, **{t: t for t in range(10, 20)}}
    anime_index = {ANIME_WATCH: 9700, second_anime: 9701, **{t: t for t in range(50, 60)}}
    ctx.plex.build_library_index.side_effect = lambda sec: tv_index if sec is tv_section else anime_index
    ctx.history_source.fetch.return_value = [
        make_watched("Anime Watch", days_ago=1, media_type=MediaType.SHOW, rating_key=9700),
        make_watched("Silicon Valley", days_ago=60, media_type=MediaType.SHOW, rating_key=9800),
    ]
    first_night = _run(ctx, mock_plextv)
    previous: dict[tuple[str, str, str], list] = {}
    for pick in first_night.picks:
        previous.setdefault(("sarah", pick.collection_slug, str(pick.section_key)), []).append(pick)
    ctx.previous_picks = previous
    ctx.previous_recipes = {key: picks[0].recipe for key, picks in previous.items()}
    ctx.history_source.fetch.return_value = [
        make_watched("Anime Two", days_ago=0, media_type=MediaType.SHOW, rating_key=9701),
        *ctx.history_source.fetch.return_value,
    ]

    second_night = _run(ctx, mock_plextv)

    decisions = {s["library"]: s["decision"] for s in second_night.trace["selection"]}
    assert decisions["TV Shows"] != "settings_changed"
    assert decisions["Anime"] != "settings_changed"
