from shortlist.engine.candidates import _slice_for_llm, filter_candidates, gather_candidates
from shortlist.engine.curator import NullCurator
from shortlist.engine.curator.base import parse_web_titles
from shortlist.engine.models import MediaType, Pick, Seed
from tests.conftest import make_candidate


class _PickFirstCurator:
    """A stand-in curator that 'proposes' the first title it's shown from the library slice."""

    def curate(self, profile, candidates, k):
        c = candidates[0]
        return [
            Pick(
                tmdb_id=c.tmdb_id,
                rating_key=c.rating_key or 0,
                title=c.title,
                rank=1,
                reason="fits",
                media_type=c.media_type,
            )
        ]


class _BoomCurator:
    def curate(self, profile, candidates, k):
        raise RuntimeError("llm down")


class _FakeTrakt:
    def __init__(self, items):
        self._items = items
        self.calls: list[tuple[int, MediaType]] = []

    def related(self, tmdb_id, media_type):
        self.calls.append((tmdb_id, media_type))
        return self._items


def seed(tmdb_id: int, title: str = "Seed") -> Seed:
    return Seed(tmdb_id=tmdb_id, title=title, media_type=MediaType.MOVIE, weight=1.0)


class TestGatherCandidates:
    def test_pools_and_tags_with_all_suggesting_seeds(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 42, "title": "Shared Pick", "genre_ids": [18], "vote_average": 8.0, "release_date": "2020-01-01"},
            {"id": 42 + tid, "title": f"Only {tid}", "genre_ids": [], "vote_average": 6.0},
        ]
        pool = gather_candidates(mock_tmdb, [seed(1), seed(2)])
        shared = next(c for c in pool if c.tmdb_id == 42)
        assert shared.seed_frequency == 2
        assert shared.genres == ["Drama"]
        assert shared.year == 2020
        assert len(pool) == 3

    def test_logs_a_per_source_breakdown(self, mock_tmdb):
        # The run log should show WHERE candidates came from — a title found by two sources counts
        # under each, so the parts can exceed the unique total.
        from loguru import logger

        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 42, "title": "Both", "genre_ids": [], "vote_average": 8.0},
        ]
        trakt = _FakeTrakt([{"tmdb_id": 42, "title": "Both", "year": 2020, "genres": []}])

        lines: list[str] = []
        sink = logger.add(lines.append, level="DEBUG", format="{message}")
        try:
            gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_similar", "trakt"], trakt=trakt)
        finally:
            logger.remove(sink)

        breakdown = next(line for line in lines if line.startswith("candidates ·"))
        assert "tmdb_similar 1" in breakdown
        assert "trakt 1" in breakdown
        assert "1 unique" in breakdown

    def test_genre_map_fetched_once_per_media_type(self, mock_tmdb):
        gather_candidates(mock_tmdb, [seed(1), seed(2), seed(3)])
        assert mock_tmdb.genre_names.call_count == 1

    def test_discover_source_widens_the_pool_with_taste_genres(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 100, "title": "Similar", "genre_ids": [18], "vote_average": 7.0}
        ]
        mock_tmdb.genre_ids_for.side_effect = lambda tid, mt: [18, 28]
        mock_tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 200, "title": "Discovered", "genre_ids": [18], "vote_average": 8.5}
        ]
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_similar", "tmdb_discover"])
        assert {c.tmdb_id for c in pool} == {100, 200}  # similar + discovered, unioned
        # discover was asked for the seeds' dominant genres
        assert 18 in mock_tmdb.discover.call_args.args[1]

    def test_sources_gate_which_apis_run(self, mock_tmdb):
        mock_tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        mock_tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 5, "title": "D", "genre_ids": [], "vote_average": 7.0}
        ]
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_discover"])
        assert mock_tmdb.suggestions.called is False  # similar disabled -> TMDB /similar never queried
        assert {c.tmdb_id for c in pool} == {5}

    def test_discover_failure_keeps_the_similar_pool(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 1, "title": "Similar", "genre_ids": [], "vote_average": 7.0}
        ]
        mock_tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        mock_tmdb.discover.side_effect = RuntimeError("TMDB 503")
        # Discover blows up, but it's only a "widen" source — the tmdb_similar pool must survive.
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_similar", "tmdb_discover"])
        assert {c.tmdb_id for c in pool} == {1}

    def test_empty_sources_falls_back_to_default(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 1, "title": "Similar", "genre_ids": [], "vote_average": 7.0}
        ]
        # Toggling every source off still yields the baseline, never an empty pool.
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=[])
        assert {c.tmdb_id for c in pool} == {1}
        assert mock_tmdb.discover.called is False

    def test_default_sources_do_not_call_discover(self, mock_tmdb):
        gather_candidates(mock_tmdb, [seed(1)])  # unset -> default (tmdb_similar only)
        assert mock_tmdb.discover.called is False

    def test_llm_library_source_proposes_owned_titles(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: []
        catalog = {
            MediaType.MOVIE: [
                {"tmdb_id": 500, "rating_key": 1, "title": "Owned A", "year": 2020, "genres": ["Drama"]},
                {"tmdb_id": 501, "rating_key": 2, "title": "Owned B", "year": 2021, "genres": ["Comedy"]},
            ]
        }
        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["llm_library"],
            curator=_PickFirstCurator(),
            catalog=catalog,
            profile=object(),
        )
        assert {c.tmdb_id for c in pool} == {500}  # only the curator's pick from the owned library

    def test_llm_library_failure_keeps_the_other_sources(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}
        ]
        catalog = {MediaType.MOVIE: [{"tmdb_id": 5, "rating_key": 1, "title": "X", "year": 2020, "genres": []}]}
        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["tmdb_similar", "llm_library"],
            curator=_BoomCurator(),
            catalog=catalog,
            profile=object(),
        )
        assert {c.tmdb_id for c in pool} == {1}  # similar survives; the LLM source's failure is swallowed

    def test_llm_library_is_a_noop_without_a_real_curator(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}
        ]
        catalog = {MediaType.MOVIE: [{"tmdb_id": 5, "rating_key": 1, "title": "X", "year": 2020, "genres": []}]}
        # NullCurator isn't AI -> the source no-ops (matches the UI gate); only tmdb_similar contributes.
        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["tmdb_similar", "llm_library"],
            curator=NullCurator(),
            catalog=catalog,
            profile=object(),
        )
        assert {c.tmdb_id for c in pool} == {1}

    def test_trakt_source_adds_related_titles(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: []
        trakt = _FakeTrakt([{"tmdb_id": 700, "title": "Related", "year": 2019, "genres": ["drama"]}])
        s = seed(1)
        pool = gather_candidates(mock_tmdb, [s], sources=["trakt"], trakt=trakt)
        assert trakt.calls == [(1, MediaType.MOVIE)]  # queried with the seed's id + media type
        cand = next(c for c in pool if c.tmdb_id == 700)
        assert cand.media_type is MediaType.MOVIE
        assert s in cand.seeds  # provenance kept — this is a real "because you watched X"

    def test_trakt_failure_keeps_the_other_sources(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}
        ]

        class _Boom:
            def related(self, *a):
                raise RuntimeError("trakt down")

        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_similar", "trakt"], trakt=_Boom())
        assert {c.tmdb_id for c in pool} == {1}

    def test_llm_web_source_resolves_proposed_titles_via_tmdb_search(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: []
        mock_tmdb.genre_names.return_value = {}
        # A movie resolves, a SHOW resolves, and a hallucinated title doesn't (so it's dropped).
        resolved = {
            "Real Film": {"id": 800, "title": "Found", "genre_ids": [], "vote_average": 7.5},
            "Real Show": {"id": 900, "name": "Found Show", "genre_ids": [], "vote_average": 8.0},
        }
        mock_tmdb.search.side_effect = lambda title, mt, year=None: resolved.get(title)

        class _WebCurator:
            def recommend_web(self, profile, seeds, k):
                return [
                    {"title": "Real Film", "year": 2022, "media": "movie"},
                    {"title": "Real Show", "year": 2019, "media": "show"},
                    {"title": "Made Up", "year": None, "media": "movie"},
                ]

        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["llm_web"], curator=_WebCurator(), profile=object())
        assert {c.tmdb_id for c in pool} == {800, 900}  # both resolved; the hallucinated one dropped
        # The show's media type and year are forwarded to search — not defaulted to movie / None.
        show_call = next(c for c in mock_tmdb.search.call_args_list if c.args[0] == "Real Show")
        assert show_call.args[1] is MediaType.SHOW and show_call.kwargs["year"] == 2019
        assert next(c for c in pool if c.tmdb_id == 900).media_type is MediaType.SHOW

    def test_llm_web_is_a_noop_without_a_real_curator(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}
        ]
        # NullCurator has no web search -> the source no-ops (matching the UI gate); search never runs.
        pool = gather_candidates(
            mock_tmdb, [seed(1)], sources=["tmdb_similar", "llm_web"], curator=NullCurator(), profile=object()
        )
        assert {c.tmdb_id for c in pool} == {1}
        assert not mock_tmdb.search.called

    def test_llm_web_failure_keeps_the_other_sources(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            {"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}
        ]

        class _Boom:
            def recommend_web(self, *a):
                raise RuntimeError("web search down")

        pool = gather_candidates(
            mock_tmdb, [seed(1)], sources=["tmdb_similar", "llm_web"], curator=_Boom(), profile=object()
        )
        assert {c.tmdb_id for c in pool} == {1}


class TestParseWebTitles:
    def test_parses_a_plain_json_array(self):
        text = '[{"title": "Dune", "year": 2021, "media": "movie"}, {"title": "Severance", "media": "show"}]'
        out = parse_web_titles(text, 10)
        assert out == [
            {"title": "Dune", "year": 2021, "media": "movie"},
            {"title": "Severance", "year": None, "media": "show"},
        ]

    def test_extracts_the_array_from_surrounding_prose(self):
        text = 'Here are picks:\n[{"title": "Sicario", "year": 2015, "media": "movie"}]\nHope that helps!'
        assert parse_web_titles(text, 10) == [{"title": "Sicario", "year": 2015, "media": "movie"}]

    def test_normalizes_media_aliases_and_drops_titleless_items(self):
        text = '[{"title": "X", "media": "tv"}, {"media": "movie"}, {"title": "Y", "media": "series"}]'
        out = parse_web_titles(text, 10)
        assert out == [{"title": "X", "year": None, "media": "show"}, {"title": "Y", "year": None, "media": "show"}]

    def test_unparseable_reply_yields_empty(self):
        assert parse_web_titles("the model refused to answer", 10) == []

    def test_skips_non_dict_items_and_caps_at_limit(self):
        text = '[1, "junk", {"title": "A"}, {"title": "B"}, {"title": "C"}]'
        out = parse_web_titles(text, 2)
        assert [it["title"] for it in out] == ["A", "B"]  # non-dicts skipped, then capped at 2

    def test_non_int_year_coerces_to_none(self):
        # A string/float year from a chatty model must not leak a bad type downstream.
        out = parse_web_titles('[{"title": "A", "year": "2021", "media": "movie"}]', 5)
        assert out == [{"title": "A", "year": None, "media": "movie"}]


class TestSliceForLlm:
    def test_caps_and_prefers_taste_matching_titles(self):
        # A big library must be trimmed to the cap, keeping taste-matching titles over the long tail.
        items = [{"tmdb_id": i, "genres": ["Comedy"]} for i in range(400)]
        items.append({"tmdb_id": 9999, "genres": ["Drama"]})  # the only Drama title, last in the list
        sliced = _slice_for_llm(items, {"Drama"}, 300)
        assert len(sliced) == 300  # capped
        assert any(it["tmdb_id"] == 9999 for it in sliced)  # taste match kept despite being last

    def test_small_library_is_returned_whole(self):
        items = [{"tmdb_id": 1, "genres": []}, {"tmdb_id": 2, "genres": []}]
        assert _slice_for_llm(items, set(), 300) == items


class TestFilterCandidates:
    def _index(self):
        return {MediaType.MOVIE: {10: 1010, 20: 1020, 30: 1030}, MediaType.SHOW: {}}

    def test_keeps_only_library_matches_and_sets_rating_key(self):
        cands = [make_candidate(10, "In"), make_candidate(99, "Out")]
        kept = filter_candidates(
            cands, self._index(), watched_tmdb_ids=set(), excluded_genres=set(), recent_pick_ids=set()
        )
        assert [c.tmdb_id for c in kept] == [10]
        assert kept[0].rating_key == 1010

    def test_drops_watched_excluded_genre_and_stale(self):
        cands = [
            make_candidate(10, "Watched"),
            make_candidate(20, "Horror pick", genres=["Horror"]),
            make_candidate(30, "Stale"),
        ]
        kept = filter_candidates(
            cands,
            self._index(),
            watched_tmdb_ids={(10, MediaType.MOVIE)},
            excluded_genres={"horror"},
            recent_pick_ids={(30, MediaType.MOVIE)},
        )
        assert kept == []

    def test_a_watched_movie_does_not_suppress_the_show_that_shares_its_id(self):
        """TMDB ids are unique only within a namespace: movie 550 and TV 550 are different
        titles. Keying the guards on the bare id silently drops valid recommendations."""
        show = make_candidate(550, "Some Show", media_type=MediaType.SHOW)
        index = {MediaType.MOVIE: {550: 1550}, MediaType.SHOW: {550: 2550}}

        kept = filter_candidates(
            [show],
            index,
            watched_tmdb_ids={(550, MediaType.MOVIE)},  # they watched the FILM
            excluded_genres=set(),
            recent_pick_ids={(550, MediaType.MOVIE)},  # and it was recently picked
        )

        assert [c.title for c in kept] == ["Some Show"]
