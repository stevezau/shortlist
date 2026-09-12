"""A "watch it again" row is built from what the person has FINISHED, not from what resembles it (#114).

Until this existed the row drew from the same similar-titles pool as every other row. That pool only
holds a finished title when a different watch's search happens to name it, and a rewatch row's pool
also dropped the ~30 seeds — the most-watched titles of all. So a person with hundreds of finished
films got a row of two, topped up with things they had never seen, under a "you've already seen" name.

Every test here drives the real engine against the template's own values, with a TMDB that suggests
NONE of the person's history unless a test says otherwise: that is the shape of a real server, and
the shape the old template test avoided by construction.
"""

from __future__ import annotations

from dataclasses import replace

import shortlist.engine.pipeline as pipeline_mod
from shortlist.engine.models import MediaType, Pick
from tests.conftest import NOW, fake_media_item, make_profile, make_watched, plextv_user
from tests.integration.test_row_templates_are_real import _movies, _picks_by_row, _shows, _spec
from tests.integration.test_row_templates_are_real import engine_ctx as engine_ctx  # re-exported: a fixture

FRESH = (10, 20, 30, 40, 50, 60)


def _library(engine_ctx, *ids: int) -> None:
    engine_ctx.plex.build_library_index.return_value = {i: 5000 + i for i in (*ids, *FRESH, 900, 800)}


def _run(engine_ctx, mock_plextv, spec, **profile_kw) -> list[Pick]:
    engine_ctx.run_at = NOW
    engine_ctx.config.rows = [spec]
    mock_plextv.users = [plextv_user(100, "sarah")]
    report = pipeline_mod.run(engine_ctx, [make_profile("sarah", account_id=100, **profile_kw)])
    return _picks_by_row(report).get(spec.slug, [])


def _row(**overrides):
    """The template's own values, with a test's overrides on top (the template already sets `size`)."""
    return replace(_spec("seen-it-already"), **overrides)


def _ids(picks: list[Pick]) -> list[int]:
    return [p.tmdb_id for p in picks]


class TestBuiltFromHistory:
    def test_finished_films_the_search_never_suggests_fill_the_row(self, engine_ctx, mock_plextv):
        """The report on #114: plenty finished, yet the row held two of them and three unseen films."""
        watched = list(range(901, 913))
        _library(engine_ctx, *watched)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate(watched)
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH)

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=8)))

        assert len(delivered) == 8
        assert set(delivered) <= set(watched), f"an unseen title took a slot a finished one could fill: {delivered}"

    def test_the_titles_the_row_was_seeded_from_are_eligible_too(self, engine_ctx, mock_plextv):
        """The seeds are the person's most prominent watches. Excluding them — what the shared pool
        does for every other row — removed exactly the titles a rewatch shelf is for."""
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.config.max_seeds = 3
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate((901, 902, 903))
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH)

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=3)))

        assert sorted(delivered) == [901, 902, 903]

    def test_a_thin_history_is_topped_up_with_unseen_titles_only(self, engine_ctx, mock_plextv):
        """Two finished films and a row of five: the rest is new suggestions, never a started show."""
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Film 901", days_ago=40, tmdb_id=901),
            make_watched("Film 902", days_ago=41, tmdb_id=902),
            make_watched("Film 903", days_ago=42, tmdb_id=903),
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH, 901)

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=5)))

        assert delivered[:3] and set(delivered[:3]) == {901, 902, 903}, delivered
        assert len(delivered) == 5
        assert set(delivered[3:]) <= set(FRESH)

    def test_a_finished_show_leads_and_a_merely_started_one_never_appears(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 801, 802, 803, 804)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(
                "Done", days_ago=40, media_type=MediaType.SHOW, tmdb_id=801, viewed_leaf_count=10, leaf_count=10
            ),
            make_watched(
                "Done 2", days_ago=41, media_type=MediaType.SHOW, tmdb_id=802, viewed_leaf_count=8, leaf_count=8
            ),
            make_watched(
                "Also", days_ago=42, media_type=MediaType.SHOW, tmdb_id=803, viewed_leaf_count=6, leaf_count=6
            ),
            make_watched(
                "Begun", days_ago=43, media_type=MediaType.SHOW, tmdb_id=804, viewed_leaf_count=1, leaf_count=40
            ),
        ]
        engine_ctx.tmdb.suggestions.return_value = _shows(*FRESH)

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=5, media="show")))

        assert set(delivered[:3]) == {801, 802, 803}, delivered
        assert 804 not in delivered, "a show two episodes in is neither a rewatch nor a fresh suggestion"

    def test_every_pick_says_why_it_is_there(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=400 + n, tmdb_id=i) for n, i in enumerate((901, 902, 903))
        ]

        picks = _run(engine_ctx, mock_plextv, _row(size=3))

        assert {p.sources[0] for p in picks} == {"history"}
        assert all(p.reason.startswith("Last watched") for p in picks), [p.reason for p in picks]


class TestRecentlyFinished:
    def _history(self):
        return [
            make_watched("Last night", days_ago=1, tmdb_id=901),
            make_watched("A fortnight ago", days_ago=14, tmdb_id=902),
            make_watched("Two months ago", days_ago=60, tmdb_id=903),
            make_watched("Older", days_ago=90, tmdb_id=904),
        ]

    def test_titles_finished_inside_the_cooldown_stay_out(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903, 904)
        engine_ctx.history_source.fetch.return_value = self._history()

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=4)))

        assert 901 not in delivered and 902 not in delivered, delivered
        assert {903, 904} <= set(delivered)

    def test_the_default_is_thirty_days(self, engine_ctx, mock_plextv):
        assert _row().rewatch_cooldown_days == 30

    def test_zero_lets_anything_finished_back_in(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903, 904)
        engine_ctx.history_source.fetch.return_value = self._history()

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=4, rewatch_cooldown_days=0)))

        assert sorted(delivered) == [901, 902, 903, 904]

    def test_a_carried_pick_they_have_just_rewatched_leaves_the_row(self, engine_ctx, mock_plextv):
        """It worked — they watched it again. Leaving it there until the next rebuild shows them the
        film they finished last night, which is the one thing the cooldown promises not to."""
        _library(engine_ctx, 901, 902, 903)
        spec = _row(size=2, refresh_days=0)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Rewatched last night", days_ago=1, tmdb_id=901),
            make_watched("Old", days_ago=90, tmdb_id=902),
            make_watched("Older", days_ago=120, tmdb_id=903),
        ]
        prior = [
            Pick(tmdb_id=901, rating_key=5901, title="a", rank=1, reason="", media_type=MediaType.MOVIE),
            Pick(tmdb_id=902, rating_key=5902, title="b", rank=2, reason="", media_type=MediaType.MOVIE),
        ]
        engine_ctx.previous_picks = {("sarah", spec.slug, "1"): prior}

        delivered = _ids(_run(engine_ctx, mock_plextv, spec))

        assert 901 not in delivered, delivered
        assert 902 in delivered


class TestCarryAndRefresh:
    def _prior(self, *ids: int) -> list[Pick]:
        return [
            Pick(tmdb_id=i, rating_key=5000 + i, title=str(i), rank=r, reason="", media_type=MediaType.MOVIE)
            for r, i in enumerate(ids, start=1)
        ]

    def test_a_gap_on_a_carried_night_is_filled_with_a_finished_title_first(self, engine_ctx, mock_plextv):
        """Two carried picks were just rewatched and leave. Padding alternates one title per seed, so an
        unseen title took a slot while finished ones sat unused — #114 again, for up to 11 nights."""
        _library(engine_ctx, 901, 902, 903, 904, 905, 906, 907)
        spec = _row(size=4, refresh_days=0)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("rewatched", days_ago=1, tmdb_id=901),
            make_watched("rewatched too", days_ago=2, tmdb_id=902),
            *[make_watched(f"old {i}", days_ago=100 + i, tmdb_id=i) for i in (903, 904, 905, 906, 907)],
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH)
        engine_ctx.previous_picks = {("sarah", spec.slug, "1"): self._prior(901, 902, 903, 904)}

        delivered = _ids(_run(engine_ctx, mock_plextv, spec))

        assert delivered[:2] == [903, 904], "the carried picks stay put"
        assert set(delivered) <= {903, 904, 905, 906, 907}, delivered

    def test_a_refresh_night_rotates_through_their_history(self, engine_ctx, mock_plextv):
        """The weakest third swaps for finished titles the row was NOT carrying, so a title rotated out
        tonight cannot come straight back."""
        ids = tuple(range(901, 910))
        _library(engine_ctx, *ids)
        spec = _row(size=3, refresh_days=1)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"old {i}", days_ago=500 - n, tmdb_id=i) for n, i in enumerate(ids)
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH)
        engine_ctx.previous_picks = {("sarah", spec.slug, "1"): self._prior(901, 902, 903)}
        engine_ctx.previous_recipes = {}

        delivered = _ids(_run(engine_ctx, mock_plextv, spec))

        assert len(delivered) == 3 and set(delivered) <= set(ids), delivered
        assert set(delivered) != {901, 902, 903}, "a refresh night changed nothing"
        assert {901, 902} <= set(delivered), "the strongest two-thirds stay"


class TestOrder:
    def test_their_favourites_then_their_current_taste_then_the_longest_unseen(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903, 904, 905)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Recent, unrated", days_ago=40, tmdb_id=901),
            make_watched("Old, unrated", days_ago=400, tmdb_id=902),
            make_watched("Rated 4 stars", days_ago=50, tmdb_id=903, user_rating=8.0),
            make_watched("Rated 5 stars", days_ago=60, tmdb_id=904, user_rating=10.0),
            make_watched("Matches tonight's taste", days_ago=45, tmdb_id=905),
        ]
        # 905 is what tonight's similar-titles search turned up — the taste signal.
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH, 905)

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=5)))

        assert delivered == [904, 903, 905, 902, 901]

    def test_a_middling_rating_is_not_a_favourite(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Three stars", days_ago=40, tmdb_id=901, user_rating=6.0),
            make_watched("Unrated but older", days_ago=400, tmdb_id=902),
            make_watched("Inside the cooldown", days_ago=10, tmdb_id=900),
        ]

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=2)))

        assert delivered == [902, 901]


class TestWhatStaysOut:
    def test_a_title_they_rated_low_is_never_put_back_in_front_of_them(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Hated it", days_ago=40, tmdb_id=901, user_rating=2.0),
            make_watched("Fine", days_ago=41, tmdb_id=902),
            make_watched("Fine too", days_ago=42, tmdb_id=903),
        ]

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=3)))

        assert 901 not in delivered

    def test_their_excluded_genres_hold_for_rewatches_too(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate((901, 902, 903))
        ]
        engine_ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [27] if tid == 901 else [18]
        engine_ctx.tmdb.genre_names.return_value = {18: "Drama", 27: "Horror"}

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=3), excluded_genres={"horror"}))

        assert 901 not in delivered
        assert {902, 903} <= set(delivered)

    def test_a_title_not_in_this_library_is_not_offered(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate((901, 902, 999))
        ]

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=3)))

        assert 999 not in delivered

    def test_a_failed_genre_lookup_keeps_the_title_out_and_stops_asking(self, engine_ctx, mock_plextv):
        """An exclusion is often a parent's. "We couldn't check" is not a reason to show it — and a TMDB
        that is down should cost one failed call, not one per finished title."""
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate((901, 902, 903))
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH)
        engine_ctx.config.max_seeds = 1  # the seed's own genre lookups (discover) are 901's, not ours
        engine_ctx.tmdb.genre_ids_for.side_effect = RuntimeError("TMDB down")

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=3), excluded_genres={"horror"}))

        assert not {901, 902, 903} & set(delivered), delivered
        asked = [c.args[0] for c in engine_ctx.tmdb.genre_ids_for.call_args_list if c.args[0] != 901]
        assert len(asked) <= 1, f"kept asking a TMDB that is down: {asked}"

    def test_a_genre_outage_on_a_rebuild_night_keeps_last_nights_row(self, engine_ctx, mock_plextv):
        """Rebuilt blind, the row fills with unseen titles — and a full row carries forward untouched, so
        they would stay until the next refresh night, 11 days on the template. Keep what it had."""
        _library(engine_ctx, 901, 902, 903, 904)
        spec = _row(size=2, refresh_days=1)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate((901, 902, 903, 904))
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH)
        engine_ctx.config.max_seeds = 1
        engine_ctx.tmdb.genre_ids_for.side_effect = RuntimeError("TMDB down")
        engine_ctx.previous_picks = {
            ("sarah", spec.slug, "1"): [
                Pick(tmdb_id=i, rating_key=5000 + i, title=str(i), rank=r, reason="", media_type=MediaType.MOVIE)
                for r, i in enumerate((903, 904), start=1)
            ]
        }

        delivered = _ids(_run(engine_ctx, mock_plextv, spec, excluded_genres={"horror"}))

        assert delivered == [903, 904], delivered

    def test_a_genre_outage_leaves_a_gap_short_rather_than_filling_it_with_an_unseen_title(
        self, engine_ctx, mock_plextv
    ):
        _library(engine_ctx, 901, 902, 903, 904)
        spec = _row(size=3, refresh_days=0)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Rewatched yesterday", days_ago=1, tmdb_id=901),
            *[make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate((902, 903, 904))],
        ]
        engine_ctx.tmdb.suggestions.return_value = _movies(*FRESH)
        engine_ctx.config.max_seeds = 1
        engine_ctx.tmdb.genre_ids_for.side_effect = RuntimeError("TMDB down")
        engine_ctx.previous_picks = {
            ("sarah", spec.slug, "1"): [
                Pick(tmdb_id=i, rating_key=5000 + i, title=str(i), rank=r, reason="", media_type=MediaType.MOVIE)
                for r, i in enumerate((901, 902, 903), start=1)
            ]
        }

        delivered = _ids(_run(engine_ctx, mock_plextv, spec, excluded_genres={"horror"}))

        assert delivered == [902, 903], delivered

    def test_genres_are_only_looked_up_for_titles_the_row_could_use(self, engine_ctx, mock_plextv):
        watched = list(range(901, 961))
        _library(engine_ctx, *watched)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Film {i}", days_ago=40 + n, tmdb_id=i) for n, i in enumerate(watched)
        ]
        engine_ctx.config.max_seeds = 1
        engine_ctx.tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=4), excluded_genres={"horror"}))

        assert len(delivered) == 4
        asked = {c.args[0] for c in engine_ctx.tmdb.genre_ids_for.call_args_list}
        assert len(asked) < 20, f"looked up {len(asked)} of 60 finished titles for a row of 4"


class TestIdentity:
    def test_a_watch_known_only_by_its_plex_rating_key_still_counts(self, engine_ctx, mock_plextv):
        """Plenty of history arrives without a TMDB id and is resolved through the library's ratingKey."""
        _library(engine_ctx, 801, 802, 803)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(
                f"Show {i}",
                days_ago=40,
                media_type=MediaType.SHOW,
                rating_key=5000 + i,
                viewed_leaf_count=6,
                leaf_count=6,
            )
            for i in (801, 802, 803)
        ]
        engine_ctx.tmdb.suggestions.return_value = _shows(*FRESH)

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=3, media="show")))

        assert sorted(delivered) == [801, 802, 803]

    def test_a_low_rating_on_a_watch_resolved_by_rating_key_still_keeps_it_out(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Hated", days_ago=40, rating_key=5901, user_rating=2.0),
            make_watched("Fine", days_ago=41, tmdb_id=902),
            make_watched("Fine too", days_ago=42, tmdb_id=903),
        ]

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=3)))

        assert 901 not in delivered

    def test_ratings_do_not_order_the_row_when_plex_ratings_are_switched_off(self, engine_ctx, mock_plextv):
        _library(engine_ctx, 901, 902, 903)
        engine_ctx.config.dislike_threshold = None
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Five stars, recent", days_ago=40, tmdb_id=901, user_rating=10.0),
            make_watched("Oldest", days_ago=400, tmdb_id=902),
            make_watched("Middle", days_ago=200, tmdb_id=903),
        ]

        picks = _run(engine_ctx, mock_plextv, _row(size=3))

        assert _ids(picks) == [902, 903, 901]
        assert not any("rated" in p.reason for p in picks)


class TestNaming:
    def test_a_row_named_after_a_watch_still_gets_a_name(self, engine_ctx, mock_plextv):
        """Every pick of a rewatch row IS a watch, so "Because you watched {top_seed}" is fillable —
        leaving its picks seedless rendered no name, and a row with no name is silently not built."""
        ids = tuple(range(901, 905))
        _library(engine_ctx, *ids)
        engine_ctx.history_source.fetch.return_value = [
            make_watched(f"Old {i}", days_ago=100 + n, tmdb_id=i) for n, i in enumerate(ids)
        ]

        picks = _run(engine_ctx, mock_plextv, _row(size=4, name_template="Because you watched {top_seed}"))

        assert picks, "the row was never built"
        assert all(p.seed_title for p in picks if p.sources == ["history"])


class TestColdStart:
    def test_a_thin_history_still_leads_with_what_they_finished(self, engine_ctx, mock_plextv):
        """Below `min_history` every row falls back to the server's top-rated titles — which, for a row
        promising things they've already seen, is a row of things they haven't."""
        _library(engine_ctx, 901, 902)
        engine_ctx.config.min_history = 5
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Film 901", days_ago=40, tmdb_id=901),
            make_watched("Film 902", days_ago=41, tmdb_id=902),
        ]
        engine_ctx.plex.top_rated.side_effect = lambda section, k: [
            (i, fake_media_item(5000 + i, f"Popular {i}", tmdb_id=i)) for i in (902, 10, 20, 30)
        ][:k]

        delivered = _ids(_run(engine_ctx, mock_plextv, _row(size=4, media="movie")))

        assert set(delivered[:2]) == {901, 902}, delivered
        assert delivered.count(902) == 1
        assert set(delivered[2:]) <= {10, 20, 30}


class TestPoolAndRecipe:
    def _policy(self, engine_ctx):
        from shortlist.engine.rows import RowPolicy

        policy = object.__new__(RowPolicy)
        policy.cfg = engine_ctx.config
        policy.watched_titles = set()
        policy.watched_shows = {}
        return policy

    def test_a_rewatch_row_shares_the_unseen_pool_its_top_up_comes_from(self, engine_ctx):
        """Its finished titles come from history now, so the gather only has to find UNSEEN ones — the
        same gather a 0% row pays for. Keying it apart bought a second TMDB/AI gather per person."""
        policy = self._policy(engine_ctx)
        rewatch = _row()
        plain = replace(rewatch, rewatch=False, watched_pct=0.0)

        assert policy.excludes_watched(rewatch)
        assert policy.pool_exclusions(rewatch) == policy.pool_exclusions(plain)

    def _real_policy(self, engine_ctx, specs):
        from unittest.mock import MagicMock

        from shortlist.engine.rows import RowPolicy, _rating_key_resolver

        return RowPolicy(
            ctx=engine_ctx,
            user=make_profile("sarah", account_id=100),
            cfg=engine_ctx.config,
            specs=specs,
            library_index={},
            report=MagicMock(),
            resolve=_rating_key_resolver({}),
        )

    def test_changing_the_cooldown_rebuilds_the_row(self, engine_ctx):
        """It decides which titles the row may hold, so waiting out the cadence would read as broken."""
        from shortlist.engine.rows import row_recipe

        thirty, seven = _row(), _row(rewatch_cooldown_days=7)
        policy = self._real_policy(engine_ctx, [thirty])

        assert row_recipe(policy, thirty) != row_recipe(policy, seven)

    def test_the_cooldown_leaves_every_other_rows_recipe_untouched(self, engine_ctx):
        """An unconditional part would mismatch every stored recipe and rebuild the whole server."""
        from shortlist.engine.rows import row_recipe

        plain = replace(_row(), rewatch=False)
        policy = self._real_policy(engine_ctx, [plain])

        assert "cooldown" not in row_recipe(policy, plain)
        assert row_recipe(policy, plain) == row_recipe(policy, replace(plain, rewatch_cooldown_days=7))

    def test_the_trace_counts_only_this_librarys_recent_finishes(self, engine_ctx, mock_plextv):
        """A Movies line that counted last night's TV episode says the wrong thing about the Movies row."""
        _library(engine_ctx, 901, 902, 903, 801)
        engine_ctx.run_at = NOW
        engine_ctx.history_source.fetch.return_value = [
            make_watched("Film last night", days_ago=1, tmdb_id=901),
            make_watched("Old", days_ago=90, tmdb_id=902),
            make_watched("Older", days_ago=95, tmdb_id=903),
            make_watched(
                "Show last night", days_ago=1, media_type=MediaType.SHOW, tmdb_id=801, viewed_leaf_count=4, leaf_count=4
            ),
        ]
        spec = _row(size=3, media="movie")
        engine_ctx.config.rows = [spec]
        mock_plextv.users = [plextv_user(100, "sarah")]
        report = pipeline_mod.run(engine_ctx, [make_profile("sarah", account_id=100)])

        entry = next(e for e in report.users[0].trace["selection"] if e["row"] == spec.slug)
        assert entry["cooling"] == 1
