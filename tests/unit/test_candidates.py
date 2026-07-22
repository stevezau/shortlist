from types import SimpleNamespace

import pytest

from shortlist.engine.candidates import (
    GatherStats,
    _slice_for_llm,
    filter_candidates,
    gather_candidates,
    genre_coherence,
)
from shortlist.engine.clients.search import SearchResult
from shortlist.engine.curator import NullCurator
from shortlist.engine.curator.base import parse_web_titles
from shortlist.engine.models import MediaType, Pick, Seed
from tests.conftest import make_candidate


def make_result(title: str, text: str = "") -> SearchResult:
    return SearchResult(title=title, url="https://example.com", text=text)


def web_profile():
    """A minimal profile for the llm_web path — only `.history` is read (by taste_summary)."""
    return SimpleNamespace(history=[])


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


def _ranked(items: list[dict], affinity: float = 1.0) -> list[tuple[dict, float]]:
    """`TmdbClient.suggestions` returns (item, affinity) pairs — affinity being how near the top of
    TMDB's list the title sat. These tests predate that and don't care, so they use the neutral 1.0.
    """
    return [(item, affinity) for item in items]


class TestGatherCandidates:
    def test_pools_and_tags_with_all_suggesting_seeds(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [
                {
                    "id": 42,
                    "title": "Shared Pick",
                    "genre_ids": [18],
                    "vote_average": 8.0,
                    "release_date": "2020-01-01",
                },
                {"id": 42 + tid, "title": f"Only {tid}", "genre_ids": [], "vote_average": 6.0},
            ]
        )
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

        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [
                {"id": 42, "title": "Both", "genre_ids": [], "vote_average": 8.0},
            ]
        )
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
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 100, "title": "Similar", "genre_ids": [18], "vote_average": 7.0}]
        )
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
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "Similar", "genre_ids": [], "vote_average": 7.0}]
        )
        mock_tmdb.genre_ids_for.side_effect = lambda tid, mt: [18]
        mock_tmdb.discover.side_effect = RuntimeError("TMDB 503")
        # Discover blows up, but it's only a "widen" source — the tmdb_similar pool must survive.
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_similar", "tmdb_discover"])
        assert {c.tmdb_id for c in pool} == {1}

    def test_empty_sources_falls_back_to_default(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "Similar", "genre_ids": [], "vote_average": 7.0}]
        )
        # Toggling every source off still yields the baseline, never an empty pool.
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=[])
        assert {c.tmdb_id for c in pool} == {1}
        assert mock_tmdb.discover.called is False

    def test_default_sources_do_not_call_discover(self, mock_tmdb):
        gather_candidates(mock_tmdb, [seed(1)])  # unset -> default (tmdb_similar only)
        assert mock_tmdb.discover.called is False

    def test_llm_library_source_proposes_owned_titles(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
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
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
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
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
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
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        trakt = _FakeTrakt([{"tmdb_id": 700, "title": "Related", "year": 2019, "genres": ["drama"]}])
        s = seed(1)
        pool = gather_candidates(mock_tmdb, [s], sources=["trakt"], trakt=trakt)
        assert trakt.calls == [(1, MediaType.MOVIE)]  # queried with the seed's id + media type
        cand = next(c for c in pool if c.tmdb_id == 700)
        assert cand.media_type is MediaType.MOVIE
        assert s in cand.seeds  # provenance kept — this is a real "because you watched X"

    def test_trakt_failure_keeps_the_other_sources(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )

        class _Boom:
            def related(self, *a):
                raise RuntimeError("trakt down")

        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_similar", "trakt"], trakt=_Boom())
        assert {c.tmdb_id for c in pool} == {1}

    def test_llm_web_source_resolves_proposed_titles_via_tmdb_search(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        # A movie resolves, a SHOW resolves, and a hallucinated title doesn't (so it's dropped).
        resolved = {
            "Real Film": {"id": 800, "title": "Found", "genre_ids": [], "vote_average": 7.5},
            "Real Show": {"id": 900, "name": "Found Show", "genre_ids": [], "vote_average": 8.0},
        }
        mock_tmdb.search.side_effect = lambda title, mt, year=None: resolved.get(title)

        class _WebCurator:
            supports_native_web_search = True

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
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        # NullCurator has no web search -> the source no-ops (matching the UI gate); search never runs.
        pool = gather_candidates(
            mock_tmdb, [seed(1)], sources=["tmdb_similar", "llm_web"], curator=NullCurator(), profile=object()
        )
        assert {c.tmdb_id for c in pool} == {1}
        assert not mock_tmdb.search.called

    def test_llm_web_failure_keeps_the_other_sources(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )

        class _Boom:
            supports_native_web_search = True

            def recommend_web(self, *a):
                raise RuntimeError("web search down")

        pool = gather_candidates(
            mock_tmdb, [seed(1)], sources=["tmdb_similar", "llm_web"], curator=_Boom(), profile=object()
        )
        assert {c.tmdb_id for c in pool} == {1}


class _FakeSearch:
    """A stub external search backend (Exa) that returns canned results and records queries."""

    name = "exa"

    def __init__(self, results):
        self._results = results
        self.queries: list[str] = []

    def search(self, query, *, num_results=8):
        self.queries.append(query)
        return self._results


class _NonNativeCurator:
    """A curator with NO native web search (like Ollama): only `complete` powers llm_web."""

    supports_native_web_search = False

    def __init__(self, reply):
        self._reply = reply
        self.complete_calls = 0

    def complete(self, system, user):
        self.complete_calls += 1
        return self._reply


class _NativeCurator:
    """A curator WITH a native web-search tool (like Claude). `recommend_web` is preferred by auto/native."""

    supports_native_web_search = True

    def __init__(self):
        self.recommend_calls = 0
        self.complete_calls = 0

    def recommend_web(self, profile, seeds, k):
        self.recommend_calls += 1
        return [{"title": "Native Pick", "year": 2020, "media": "movie"}]

    def complete(self, system, user):
        self.complete_calls += 1
        return '[{"title": "Exa Pick", "year": 2021, "media": "movie"}]'


class TestLlmWebBackends:
    """The auto|native|exa backend matrix for the llm_web source (public-app: works on every provider)."""

    def _tmdb(self, mock_tmdb, resolved):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: resolved.get(title)
        return mock_tmdb

    def test_exa_path_lets_a_non_native_provider_do_web_search(self, mock_tmdb):
        """Ollama's path: no native tool, but Exa searches and the model picks from the results."""
        self._tmdb(mock_tmdb, {"Exa Pick": {"id": 55, "title": "Exa Pick", "genre_ids": [], "vote_average": 7.0}})
        search = _FakeSearch([make_result("Best of 2021", "Exa Pick is great")])
        curator = _NonNativeCurator('[{"title": "Exa Pick", "year": 2021, "media": "movie"}]')

        pool = gather_candidates(
            mock_tmdb, [seed(1, "Arrival")], sources=["llm_web"], curator=curator, profile=web_profile(), search=search
        )
        assert {c.tmdb_id for c in pool} == {55}
        assert curator.complete_calls == 1 and len(search.queries) == 1  # searched, then the model picked
        assert "Arrival" in search.queries[0]  # the query is built from what they watched, not a constant

    def test_auto_unions_native_and_exa_when_both_are_available(self, mock_tmdb):
        # Both configured → auto runs BOTH and unions them (they surface different titles).
        self._tmdb(
            mock_tmdb,
            {
                "Native Pick": {"id": 77, "title": "Native Pick", "genre_ids": [], "vote_average": 8.0},
                "Exa Pick": {"id": 88, "title": "Exa Pick", "genre_ids": [], "vote_average": 7.0},
            },
        )
        search = _FakeSearch([make_result("2021", "Exa Pick")])
        curator = _NativeCurator()

        pool = gather_candidates(
            mock_tmdb, [seed(1)], sources=["llm_web"], curator=curator, profile=web_profile(), search=search
        )
        assert {c.tmdb_id for c in pool} == {77, 88}  # native + exa, unioned
        assert curator.recommend_calls == 1 and curator.complete_calls == 1
        assert len(search.queries) == 1

    def test_auto_uses_only_the_native_tool_when_no_search_is_configured(self, mock_tmdb):
        self._tmdb(mock_tmdb, {"Native Pick": {"id": 77, "title": "Native Pick", "genre_ids": [], "vote_average": 8.0}})
        curator = _NativeCurator()

        pool = gather_candidates(
            mock_tmdb, [seed(1)], sources=["llm_web"], curator=curator, profile=web_profile(), search=None
        )
        assert {c.tmdb_id for c in pool} == {77}
        assert curator.recommend_calls == 1 and curator.complete_calls == 0

    def test_exa_mode_forces_external_search_even_for_a_native_provider(self, mock_tmdb):
        self._tmdb(mock_tmdb, {"Exa Pick": {"id": 88, "title": "Exa Pick", "genre_ids": [], "vote_average": 7.0}})
        search = _FakeSearch([make_result("2021", "Exa Pick")])
        curator = _NativeCurator()

        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_mode="exa",
        )
        assert {c.tmdb_id for c in pool} == {88}
        assert curator.recommend_calls == 0 and curator.complete_calls == 1  # forced onto the Exa path

    def test_native_mode_without_a_native_provider_is_a_noop_not_a_failure(self, mock_tmdb):
        """web_search_mode=native + Ollama: the source can't run, so it's skipped — the OTHER source
        still contributes and no phantom 'source failed' is raised (attempted must not include it)."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        search = _FakeSearch([make_result("x", "y")])
        curator = _NonNativeCurator("[]")

        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["tmdb_similar", "llm_web"],
            curator=curator,
            profile=object(),
            search=search,
            web_search_mode="native",
        )
        assert {c.tmdb_id for c in pool} == {1}
        assert curator.complete_calls == 0 and search.queries == []  # llm_web never ran under native mode

    def test_blocked_when_no_native_and_no_search(self, mock_tmdb):
        """auto + non-native provider + NO Exa key: llm_web simply can't run; tmdb_similar carries it."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 2, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        curator = _NonNativeCurator("[]")
        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["tmdb_similar", "llm_web"],
            curator=curator,
            profile=web_profile(),
            search=None,
        )
        assert {c.tmdb_id for c in pool} == {2}
        assert curator.complete_calls == 0

    def test_exa_mode_without_a_search_key_is_a_noop_not_a_failure(self, mock_tmdb):
        """web_search_mode=exa but no Exa client configured: llm_web can't run, so it's skipped and
        never registers as attempted — tmdb_similar still carries the pool, no phantom failure."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 3, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        curator = _NativeCurator()  # native-capable, but exa mode forces the (absent) Exa backend
        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["tmdb_similar", "llm_web"],
            curator=curator,
            profile=web_profile(),
            search=None,
            web_search_mode="exa",
        )
        assert {c.tmdb_id for c in pool} == {3}
        assert curator.recommend_calls == 0 and curator.complete_calls == 0

    def test_heuristic_curator_never_runs_llm_web_even_with_a_search_key(self, mock_tmdb):
        """The engine mirror of the frontend gate: NullCurator (heuristic mode) has no model to pick
        titles, so llm_web contributes nothing even with an Exa key — and doesn't false-fail the run."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 4, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        search = _FakeSearch([make_result("2024 picks", "Dune")])
        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["tmdb_similar", "llm_web"],
            curator=NullCurator(),
            profile=web_profile(),
            search=search,
            web_search_mode="exa",
        )
        assert {c.tmdb_id for c in pool} == {4}
        assert search.queries == []  # heuristic mode never even searches


class _DictCache:
    """A minimal in-memory Cache (get/set) for the per-title web-search cache."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


class TestPerTitleWebSearchCache:
    """One cached web search PER recent title (not one blended query), keyed by (media, tmdb_id) so a
    title many users watched is searched once server-wide."""

    def _tmdb(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: None  # resolution isn't what we're testing
        return mock_tmdb

    def test_searches_once_per_title_and_caches_by_id(self, mock_tmdb):
        self._tmdb(mock_tmdb)
        search = _FakeSearch([make_result("Result", "text")])
        curator = _NonNativeCurator("[]")
        cache = _DictCache()
        gather_candidates(
            mock_tmdb,
            [seed(1, "Dune"), seed(2, "Arrival")],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_cache=cache,
        )
        assert len(search.queries) == 2  # one search per title, not one blended query
        assert any("Dune" in q for q in search.queries) and any("Arrival" in q for q in search.queries)
        assert set(cache.store) == {"exasearch:movie:1", "exasearch:movie:2"}  # cached by (media, tmdb_id)

    def test_a_cached_title_is_not_researched(self, mock_tmdb):
        self._tmdb(mock_tmdb)
        search = _FakeSearch([make_result("Result", "text")])
        cache = _DictCache()
        cache.set("exasearch:movie:1", "[]", 1)  # Dune already searched by a prior user this window
        gather_candidates(
            mock_tmdb,
            [seed(1, "Dune")],
            sources=["llm_web"],
            curator=_NonNativeCurator("[]"),
            profile=web_profile(),
            search=search,
            web_search_cache=cache,
        )
        assert search.queries == []  # served from cache — no billable Exa search

    def test_recent_count_caps_how_many_titles_are_searched(self, mock_tmdb):
        self._tmdb(mock_tmdb)
        search = _FakeSearch([make_result("Result", "text")])
        gather_candidates(
            mock_tmdb,
            [seed(1, "A"), seed(2, "B"), seed(3, "C")],
            sources=["llm_web"],
            curator=_NonNativeCurator("[]"),
            profile=web_profile(),
            search=search,
            web_search_cache=_DictCache(),
            recent_count=2,
        )
        assert len(search.queries) == 2  # only the two most-recent titles searched


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


class TestBuildWebQueryForTitle:
    """The per-title external-search query must center on the one title (so it's precise AND cacheable
    across users) with a sane fallback for an empty title."""

    def test_centers_on_the_single_title(self):
        from shortlist.engine.curator.base import build_web_query_for_title

        query = build_web_query_for_title("Arrival")
        assert "Arrival" in query and "watch" in query.lower()

    def test_empty_title_is_a_generic_query(self):
        from shortlist.engine.curator.base import build_web_query_for_title

        assert build_web_query_for_title("  ") and "watch" in build_web_query_for_title("").lower()


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


class TestGatherStats:
    """gather_candidates folds the AI candidate sources' token/Exa spend into a passed-in GatherStats.

    Regression cover for a real gap: llm_web/llm_library set the curator's `last_tokens` but nothing
    read it, so every AI-source run undercounted its cost. These lock the accounting down per source.
    """

    def test_native_web_tokens_are_recorded(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: {
            "id": 77,
            "title": title,
            "genre_ids": [],
            "vote_average": 8.0,
        }

        class _C:
            supports_native_web_search = True
            last_tokens = 0

            def recommend_web(self, profile, seeds, k):
                self.last_tokens = 321
                return [{"title": "Native Pick", "year": 2020, "media": "movie"}]

        stats = GatherStats()
        gather_candidates(mock_tmdb, [seed(1)], sources=["llm_web"], curator=_C(), profile=web_profile(), stats=stats)
        assert stats.tokens_by_source == {"llm_web": 321}
        assert stats.exa_searches == 0  # the native tool doesn't use Exa

    def test_exa_path_counts_a_search_and_its_completion_tokens(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: {
            "id": 55,
            "title": title,
            "genre_ids": [],
            "vote_average": 7.0,
        }
        search = _FakeSearch([make_result("Best of 2021", "Exa Pick")])

        class _C:
            supports_native_web_search = False
            last_tokens = 0

            def complete(self, system, user):
                self.last_tokens = 99
                return '[{"title": "Exa Pick", "year": 2021, "media": "movie"}]'

        stats = GatherStats()
        gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["llm_web"],
            curator=_C(),
            profile=web_profile(),
            search=search,
            stats=stats,
        )
        assert stats.tokens_by_source == {"llm_web": 99}
        assert stats.exa_searches == 1  # the search request itself, billed per search

    def test_llm_library_tokens_are_recorded(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        catalog = {
            MediaType.MOVIE: [{"tmdb_id": 500, "rating_key": 1, "title": "Owned A", "year": 2020, "genres": ["Drama"]}]
        }

        class _C:
            last_tokens = 0

            def curate(self, profile, candidates, k):
                self.last_tokens = 210
                c = candidates[0]
                return [
                    Pick(tmdb_id=c.tmdb_id, rating_key=1, title=c.title, rank=1, reason="fits", media_type=c.media_type)
                ]

        stats = GatherStats()
        gather_candidates(
            mock_tmdb, [seed(1)], sources=["llm_library"], curator=_C(), catalog=catalog, profile=object(), stats=stats
        )
        assert stats.tokens_by_source == {"llm_library": 210}
        assert stats.exa_searches == 0

    def test_tmdb_only_sources_record_no_ai_cost(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 1, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        stats = GatherStats()
        gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_similar"], stats=stats)
        assert stats.tokens_by_source == {} and stats.exa_searches == 0

    def test_add_tokens_ignores_zero_and_sums(self):
        stats = GatherStats()
        stats.add_tokens("curate", 0)  # a NullCurator / skipped call adds nothing
        assert stats.tokens_by_source == {}
        stats.add_tokens("curate", 5)
        stats.add_tokens("curate", 3)
        assert stats.tokens_by_source == {"curate": 8}


class TestSeedsComeFromTheRowsOwnLibraries:
    """A row's libraries used to narrow only DELIVERY, never what was searched.

    So a Movies row on a server whose owner mostly watches sport spent every seed slot on sport,
    TMDB returned more sport, the library intersection threw it away, and the row came back thin
    and reported "ok" (issue #1 follow-up).
    """

    def _ctx(self, movie_keys: dict[int, int], sport_keys: dict[int, int]):
        from types import SimpleNamespace

        return SimpleNamespace(section_index={"1": movie_keys, "9": sport_keys})

    @staticmethod
    def _watch(title: str, rating_key: int):
        from tests.conftest import make_watched

        return make_watched(title, rating_key=rating_key)

    def test_only_watches_from_the_rows_libraries_are_kept(self):
        from shortlist.engine.models import RowSpec
        from shortlist.engine.rows import _history_for_row

        ctx = self._ctx(movie_keys={10: 100}, sport_keys={20: 200})
        history = [
            self._watch("Heat", 100),
            self._watch("Match of the Day", 200),
        ]

        kept = _history_for_row(ctx, history, RowSpec(slug="movies", name_template="", size=10, library_keys=["1"]))

        assert [w.title for w in kept] == ["Heat"], "a sport watch must not seed a Movies row"

    def test_an_unpinned_row_still_sees_everything(self):
        from shortlist.engine.models import RowSpec
        from shortlist.engine.rows import _history_for_row

        ctx = self._ctx(movie_keys={10: 100}, sport_keys={20: 200})
        history = [self._watch("Heat", 100), self._watch("Match of the Day", 200)]

        kept = _history_for_row(ctx, history, RowSpec(slug="all", name_template="", size=10))

        assert len(kept) == 2

    def test_a_row_whose_libraries_hold_nothing_they_watched_falls_back(self):
        """A weak row beats no row — and it's exactly what this person got before the filter."""
        from shortlist.engine.models import RowSpec
        from shortlist.engine.rows import _history_for_row

        ctx = self._ctx(movie_keys={}, sport_keys={20: 200})
        history = [self._watch("Match of the Day", 200)]

        kept = _history_for_row(ctx, history, RowSpec(slug="movies", name_template="", size=10, library_keys=["1"]))

        assert [w.title for w in kept] == ["Match of the Day"]


class TestTmdbAffinity:
    """TMDB's ordering is the similarity signal — pooling it away is what produced the bug where a
    medical drama's row filled with fantasy (beta.2 feedback)."""

    @staticmethod
    def _client(monkeypatch, recommendations: list[str], similar: list[str]):
        from shortlist.engine.clients.tmdb import TmdbClient

        client = TmdbClient.__new__(TmdbClient)
        pages = {
            "recommendations": [{"id": 100 + i, "name": t} for i, t in enumerate(recommendations)],
            "similar": [{"id": 200 + i, "name": t} for i, t in enumerate(similar)],
        }
        monkeypatch.setattr(type(client), "_get", lambda self, path, **kw: {"results": pages[path.rsplit("/", 1)[-1]]})
        return client

    def test_recommendations_outrank_similar_and_the_top_outranks_the_tail(self, monkeypatch):
        """Real shape of the reported case: /recommendations leads with medical dramas, /similar
        trails off into Torchwood."""
        client = self._client(
            monkeypatch,
            recommendations=["ER", "Chicago Med", "Grey's Anatomy", "Servant"],
            similar=["Presidio Med", "St. Elsewhere", "MDs", "Torchwood"],
        )

        ranked = client.suggestions(250307, MediaType.SHOW)

        by_title = {item.get("name"): affinity for item, affinity in ranked}
        assert by_title["ER"] > by_title["Servant"], "position within an endpoint must count"
        assert by_title["Presidio Med"] < by_title["ER"], "/similar is noisier than /recommendations"
        assert by_title["Torchwood"] == min(by_title.values())
        assert ranked[0][0].get("name") == "ER", "returned best-first"

    def test_a_title_in_both_lists_keeps_its_strongest_claim(self, monkeypatch):
        client = self._client(monkeypatch, recommendations=["Shared"], similar=["Shared"])
        monkeypatch.setattr(type(client), "_get", lambda self, path, **kw: {"results": [{"id": 7, "name": "Shared"}]})

        ranked = client.suggestions(1, MediaType.SHOW)

        assert len(ranked) == 1
        assert ranked[0][1] == 1.0, "the /recommendations claim beats the /similar one"

    def test_every_affinity_stays_in_range(self, monkeypatch):
        client = self._client(monkeypatch, recommendations=[f"R{i}" for i in range(20)], similar=[])

        assert all(0 < affinity <= 1.0 for _item, affinity in client.suggestions(1, MediaType.SHOW))


class TestGenreCoherence:
    """Position alone doesn't separate a medical drama from a fantasy series.

    TMDB tags The Pitt simply "Drama", and so is nearly everything it suggests — so genre OVERLAP
    discriminates nothing. What separates them is the genres a candidate has that the seed does not:
    Torchwood and The Sandman are also "Sci-Fi & Fantasy", and that is the entire difference.
    """

    DRAMA, SCIFI, MYSTERY, ACTION, REALITY = 18, 10765, 9648, 10759, 10764

    def test_a_candidate_inside_the_seeds_genres_is_untouched(self):
        # ER, Chicago Med, Grey's Anatomy — all plain "Drama", exactly like The Pitt.
        assert genre_coherence({self.DRAMA}, [self.DRAMA]) == 1.0

    def test_a_foreign_genre_costs_more_the_more_of_the_title_it_is(self):
        servant = genre_coherence({self.DRAMA}, [self.DRAMA, self.MYSTERY])
        torchwood = genre_coherence({self.DRAMA}, [self.SCIFI, self.ACTION, self.DRAMA])

        assert servant > torchwood, "one foreign genre in two beats two in three"
        assert 0.5 <= torchwood < 1.0

    def test_it_never_drops_below_half(self):
        """It shades the ranking; it must not be able to veto a title on its own."""
        assert genre_coherence({self.DRAMA}, [self.SCIFI, self.REALITY]) == 0.5

    def test_no_genres_on_either_side_means_no_opinion(self):
        assert genre_coherence(set(), [self.DRAMA]) == 1.0
        assert genre_coherence({self.DRAMA}, []) == 1.0

    def test_the_reported_row_is_separated_from_the_medical_dramas(self):
        """The whole point, in the reporter's own numbers: the two fantasy shows must end up
        materially below the medical dramas, not merely a hair behind."""
        er = genre_coherence({self.DRAMA}, [self.DRAMA])
        sandman = genre_coherence({self.DRAMA}, [self.SCIFI, self.DRAMA, self.ACTION])

        assert er - sandman >= 0.3


class TestAffinityAcrossSources:
    """The cell that was wrong: a title found by BOTH a ranked and an unranked source.

    `Candidate.affinity` defaults to 1.0 meaning "no ranking information" — which is
    indistinguishable from a source claiming a perfect match. So a tail suggestion that
    `tmdb_discover` (sorted by popularity, i.e. exactly the well-known-but-unrelated titles) also
    returned had its measured position overwritten and sailed back to the top of the row, undoing
    the fix entirely for anyone who turned that source on.
    """

    TAIL_ID = 900

    def _gather(self, mock_tmdb, sources: list[str]):
        # One TMDB suggestion, deliberately at the bottom of /similar; discover returns the same id.
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            ({"id": self.TAIL_ID, "title": "The Sandman", "genre_ids": [], "vote_average": 7.9}, 0.22)
        ]
        mock_tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": self.TAIL_ID, "title": "The Sandman", "genre_ids": [], "vote_average": 7.9}
        ]
        mock_tmdb.genre_ids_for.return_value = [18]
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=sources)
        return next(c for c in pool if c.tmdb_id == self.TAIL_ID)

    def test_a_measured_position_survives_an_unranked_source_finding_it_too(self, mock_tmdb):
        ranked_only = self._gather(mock_tmdb, ["tmdb_similar"])
        both = self._gather(mock_tmdb, ["tmdb_similar", "tmdb_discover"])

        assert ranked_only.affinity == pytest.approx(0.22)
        assert both.affinity == pytest.approx(0.22), "an unranked source must not restore the neutral 1.0"
        assert both.sources == {"tmdb_similar", "tmdb_discover"}, "it still competes in both shares"

    def test_an_unranked_source_alone_stays_neutral(self, mock_tmdb):
        """discover has no list position to offer, so it must not be penalised for lacking one."""
        discover_only = self._gather(mock_tmdb, ["tmdb_discover"])

        assert discover_only.affinity == 1.0

    def test_two_seeds_keep_the_strongest_claim(self, mock_tmdb):
        mock_tmdb.genre_ids_for.return_value = [18]
        mock_tmdb.suggestions.side_effect = lambda tid, mt: [
            ({"id": self.TAIL_ID, "title": "T", "genre_ids": [], "vote_average": 7.0}, 0.3 if tid == 1 else 0.9)
        ]

        pool = gather_candidates(mock_tmdb, [seed(1), seed(2)], sources=["tmdb_similar"])

        assert next(c for c in pool if c.tmdb_id == self.TAIL_ID).affinity == pytest.approx(0.9)
