"""Helpers the row-override tests share across files: one movie row at a given refresh cadence, and prior picks."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock

from shortlist.engine.models import MediaType, Pick, RowSpec
from tests.conftest import make_watched
from tests.unit.pipeline_support import _ranked


class RowCtxHelpers:
    """Mixin for the row-override test classes."""

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

    def _prior_seeded_by(self, tmdb_ids, *, seed_tmdb_id: int, seed_title: str):
        return [replace(p, seed_tmdb_id=seed_tmdb_id, seed_title=seed_title) for p in self._prior_movies(tmdb_ids)]

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
