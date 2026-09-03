import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shortlist.engine.candidates import (
    GatherStats,
    _web_search_capable,
    filter_candidates,
    gather_candidates,
    genre_coherence,
    web_recommendations,
)
from shortlist.engine.clients.search import SearchResult, TitleCandidate
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

    def test_a_title_trakt_found_first_picks_up_the_poster_a_later_source_has(self, mock_tmdb):
        """Sources run in a FIXED order — similar, discover, trakt, llm_web — whatever order they
        are listed in. So trakt (which has no artwork to give, and creates the pool entry via
        `merge`) is genuinely followed by llm_web, which resolves through TMDB search and does.

        Without folding the poster on that second sighting the entry keeps trakt's empty path, and
        the request pass later buys it back with a detail call — while a TMDB response carrying it
        was already in hand.
        """
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: {
            "id": 42,
            "title": "Both",
            "genre_ids": [],
            "vote_average": 8.0,
            "poster_path": "/art.jpg",
            "overview": "A synopsis.",
        }
        trakt = _FakeTrakt([{"tmdb_id": 42, "title": "Both", "year": 2020, "genres": []}])

        class _WebCurator:
            supports_native_web_search = True

            def recommend_web(self, profile, seeds, k):
                return [{"title": "Both", "year": 2020, "media": "movie"}]

        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["trakt", "llm_web"],
            trakt=trakt,
            curator=_WebCurator(),
            profile=object(),
        )

        both = next(c for c in pool if c.tmdb_id == 42)
        assert both.poster_path == "/art.jpg"
        assert both.overview == "A synopsis."  # the synopsis folds on the same rule, for the same reason
        assert both.sources == {"trakt", "llm_web"}  # one candidate, owned by both

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
    """A stub external search backend that returns canned results and records queries.

    Carries `name`/`results_per_query` because the real providers do (the fake must be no easier than
    the real thing) — the engine reads both to namespace the cache and size each search.
    """

    def __init__(self, results, name: str = "exa", results_per_query: int = 5):
        self._results = results
        self.name = name
        self.results_per_query = results_per_query
        self.queries: list[str] = []
        self.counts: list[int] = []

    def search(self, query, *, num_results=8):
        self.queries.append(query)
        self.counts.append(num_results)
        return self._results


class _FakeExtractingSearch(_FakeSearch):
    """A backend that also extracts titles server-side, as Exa does via `outputSchema`.

    The fake must be no easier than the real thing: the real client returns BOTH shapes from one
    request, so this does too — and the source is expected to prefer the titles while keeping the
    snippets for the trace and for the fallback.
    """

    def __init__(self, results, titles, name: str = "exa", results_per_query: int = 10):
        super().__init__(results, name=name, results_per_query=results_per_query)
        self._titles = titles

    def search_detailed(self, query, *, num_results=8):
        return self.search(query, num_results=num_results), list(self._titles)


class _NonNativeCurator:
    """A curator with NO native web search (like Ollama): only `complete` powers llm_web."""

    supports_native_web_search = False

    def __init__(self, reply):
        self._reply = reply
        self.complete_calls = 0
        self.last_user = ""  # the RAG prompt, so a test can assert what the curator was actually shown

    def complete(self, system, user):
        self.complete_calls += 1
        self.last_user = user
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
    """The native|exa|searxng backend matrix for the llm_web source (works on every provider)."""

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
            mock_tmdb,
            [seed(1, "Arrival")],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_mode="exa",
        )
        assert {c.tmdb_id for c in pool} == {55}
        assert curator.complete_calls == 1 and len(search.queries) == 1  # searched, then the model picked
        assert "Arrival" in search.queries[0]  # the query is built from what they watched, not a constant

    def test_exactly_one_backend_runs_even_when_both_are_available(self, mock_tmdb):
        """The `auto` mode used to union the provider's own search with an external one. It was
        removed in 1.3, so a native-capable curator WITH an external client configured must now run
        the named backend and nothing else — no second search, no second bill."""
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
            mock_tmdb,
            [seed(1)],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_mode="native",
        )
        assert {c.tmdb_id for c in pool} == {77}  # the native tool only
        assert curator.recommend_calls == 1 and curator.complete_calls == 0
        assert search.queries == []  # the external client is present but deliberately unused

    def test_the_native_tool_runs_when_no_search_is_configured(self, mock_tmdb):
        self._tmdb(mock_tmdb, {"Native Pick": {"id": 77, "title": "Native Pick", "genre_ids": [], "vote_average": 8.0}})
        curator = _NativeCurator()

        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=None,
            web_search_mode="native",
        )
        assert {c.tmdb_id for c in pool} == {77}
        assert curator.recommend_calls == 1 and curator.complete_calls == 0

    @pytest.mark.parametrize("mode", ["exa", "searxng"])
    def test_naming_an_external_backend_forces_it_even_for_a_native_provider(self, mock_tmdb, mode: str):
        """Both external providers are the same branch to the engine: picking either one by name means
        "search externally", overriding a curator that could have searched natively."""
        self._tmdb(mock_tmdb, {"Exa Pick": {"id": 88, "title": "Exa Pick", "genre_ids": [], "vote_average": 7.0}})
        search = _FakeSearch([make_result("2021", "Exa Pick")], name=mode)
        curator = _NativeCurator()

        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_mode=mode,
        )
        assert {c.tmdb_id for c in pool} == {88}
        assert curator.recommend_calls == 0 and curator.complete_calls == 1  # forced onto the external path

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

    @pytest.mark.parametrize("mode", ["exa", "searxng"])
    def test_an_external_mode_without_its_backend_is_a_noop_not_a_failure(self, mock_tmdb, mode: str):
        """web_search_mode names an external backend that isn't configured: llm_web can't run, so it's
        skipped and never registers as attempted — tmdb_similar still carries the pool, no phantom
        failure. Same for an Exa key that was never entered and a SearXNG URL that was never set."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 3, "title": "S", "genre_ids": [], "vote_average": 7.0}]
        )
        curator = _NativeCurator()  # native-capable, but an external mode forces the (absent) backend
        pool = gather_candidates(
            mock_tmdb,
            [seed(1)],
            sources=["tmdb_similar", "llm_web"],
            curator=curator,
            profile=web_profile(),
            search=None,
            web_search_mode=mode,
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
    """A minimal in-memory Cache (get/set) for the per-title web-search cache.

    Records the TTL as well as the value: how LONG a thin result is kept is a decision with a cost
    attached at both extremes, so tests assert it rather than just that something was stored.
    """

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value
        self.ttls[key] = ttl_s


class TestWebSearchWithoutAnLlm:
    """Exa extracts titles itself, so the model is not required to get value from a paid search.

    What actually shipped: `gather_candidates` refused the source entirely without a real curator,
    so the Exa branch of `_web_search_capable` was unreachable and Exa-without-AI produced nothing.
    It did NOT bill — a claim that it cost $7.94 a night was made and retracted;
    that figure is run 18's real, productive spend under Claude. These tests pin the capability now
    that it is reachable, and the front-door test below is the one that would have caught the
    original mistake.
    """

    def _titles(self, *names):
        return [TitleCandidate(title=n, year=2020, media="movie") for n in names]

    def test_exa_titles_are_used_when_no_ai_provider_is_configured(self):
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Andor", "Shogun"))
        stats = GatherStats()

        out = web_recommendations(
            NullCurator(), search, "exa", web_profile(), [seed(1, "Dune")], 5, stats, cache=_DictCache()
        )

        assert [o["title"] for o in out] == ["Andor", "Shogun"]
        assert stats.exa_searches == 1  # the search was paid for — and now it buys something

    def test_the_search_is_not_wasted_silently(self):
        """The regression in one assertion: paying for a search and returning nothing is the bug."""
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Andor"))
        stats = GatherStats()

        out = web_recommendations(
            NullCurator(), search, "exa", web_profile(), [seed(1, "Dune")], 5, stats, cache=_DictCache()
        )

        assert stats.exa_searches == 1 and out, (stats.exa_searches, out)

    def test_a_model_that_answers_with_nothing_usable_falls_back_to_the_extraction(self):
        """Rate-limited, timed out, or replying in prose — the searches are already paid for, so
        Exa's own titles beat losing them."""
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Andor", "Shogun"))
        curator = _NonNativeCurator("I'm sorry, I can't help with that.")
        stats = GatherStats()

        out = web_recommendations(
            curator, search, "exa", web_profile(), [seed(1, "Dune")], 5, stats, cache=_DictCache()
        )

        assert curator.complete_calls == 1  # it DID ask, and only fell back after
        assert [o["title"] for o in out] == ["Andor", "Shogun"]

    def test_a_working_model_still_wins_over_the_raw_extraction(self):
        """The fallback must not become the default path: a usable reply is still preferred, because
        the model is what matches the list to this person's taste."""
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Andor", "Shogun"))
        reply = '[{"title": "Silo", "year": 2023, "media": "show"}]'
        stats = GatherStats()

        out = web_recommendations(
            _NonNativeCurator(reply), search, "exa", web_profile(), [seed(1, "Dune")], 5, stats, cache=_DictCache()
        )

        assert [o["title"] for o in out] == ["Silo"]

    def test_the_model_is_never_called_when_there_is_none(self):
        """`NullCurator.complete` is free, but calling it logs a parse warning that reads as a real
        failure. Skipping it keeps a keyless install's log clean."""
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Andor"))
        curator = NullCurator()
        calls = []
        curator.complete = lambda system, user: calls.append(1) or ""

        web_recommendations(
            curator, search, "exa", web_profile(), [seed(1, "Dune")], 5, GatherStats(), cache=_DictCache()
        )

        assert calls == []

    def test_the_fallback_respects_k(self):
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("A", "B", "C", "D", "E"))

        out = web_recommendations(
            NullCurator(), search, "exa", web_profile(), [seed(1, "Dune")], 2, GatherStats(), cache=_DictCache()
        )

        assert len(out) == 2

    def test_searxng_still_needs_a_model_because_it_extracts_nothing(self):
        """The asymmetry that makes this per-backend: SearXNG returns snippets, so something must
        read them. Only Exa hands back titles. A self-hosted SearXNG costs nothing, so producing
        nothing here wastes no money — but it must not silently look like Exa's behaviour."""
        search = _FakeSearch([make_result("Result", "text")], name="searxng")

        out = web_recommendations(
            NullCurator(), search, "searxng", web_profile(), [seed(1, "Dune")], 5, GatherStats(), cache=_DictCache()
        )

        assert out == []

    def test_gather_candidates_really_runs_the_source_with_no_ai(self, mock_tmdb):
        """Through the FRONT DOOR, not `web_recommendations` directly.

        This is the test that was missing. `gather_candidates` gated the source on a separate
        `llm_ready` check that outranked `_web_search_capable`, so Exa-with-no-AI was refused before
        the capability function was ever consulted — and every test calling `web_recommendations`
        directly passed against what was, in production, dead code.
        """
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: (
            {"id": 9001, "name": "Andor", "first_air_date": "2022-09-21", "genre_ids": []} if title == "Andor" else None
        )
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Andor"))

        out = gather_candidates(
            mock_tmdb,
            [seed(1, "Dune")],
            sources=["llm_web"],
            curator=NullCurator(),
            profile=web_profile(),
            search=search,
            web_search_mode="exa",
            web_search_cache=_DictCache(),
        )

        pool = out[0] if isinstance(out, tuple) else out
        assert len(search.queries) == 1, "the source never ran"
        assert [c.title for c in pool] == ["Andor"]

    def test_gather_candidates_still_skips_searxng_with_no_ai(self, mock_tmdb):
        """The other half: lifting the gate must not let through what genuinely cannot work."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        search = _FakeSearch([make_result("a", "b")], name="searxng")

        gather_candidates(
            mock_tmdb,
            [seed(1, "Dune")],
            sources=["llm_web"],
            curator=NullCurator(),
            profile=web_profile(),
            search=search,
            web_search_mode="searxng",
            web_search_cache=_DictCache(),
        )

        assert search.queries == [], "SearXNG searched with nothing able to read the results"

    def test_searxng_with_no_model_is_not_even_capable(self):
        """`_web_search_capable` gates `attempted`: a source that cannot run must not register as
        having been tried, or "every source failed" misreads an incapable setup as a failure. A
        backend alone used to be enough, so SearXNG + no AI counted as attempted and returned
        nothing."""
        assert _web_search_capable(NullCurator(), _FakeSearch([], name="searxng"), "searxng") is False

    def test_exa_with_no_model_IS_capable_because_it_extracts(self):
        search = _FakeExtractingSearch([], self._titles("Andor"))
        assert _web_search_capable(NullCurator(), search, "exa") is True

    def test_searxng_with_a_model_is_capable(self):
        assert _web_search_capable(_NonNativeCurator("[]"), _FakeSearch([], name="searxng"), "searxng") is True

    def test_the_trace_records_why_the_model_was_skipped(self):
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Andor"))
        stats = GatherStats()

        web_recommendations(
            NullCurator(), search, "exa", web_profile(), [seed(1, "Dune")], 5, stats, cache=_DictCache()
        )

        assert "no AI provider" in stats.trace["web"]["unpicked"]


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
            web_search_mode="exa",
            web_search_cache=cache,
        )
        assert len(search.queries) == 2  # one search per title, not one blended query
        assert any("Dune" in q for q in search.queries) and any("Arrival" in q for q in search.queries)
        # Cached by (provider, media, tmdb_id) — see test_switching_backends_does_not_reuse... below.
        assert set(cache.store) == {"websearch2:exa:movie:1", "websearch2:exa:movie:2"}

    def test_switching_backends_does_not_reuse_the_other_backends_cached_results(self, mock_tmdb):
        """The cache key must carry the PROVIDER, or a switch is invisible for the 14-day TTL.

        Exa returns page text and SearXNG returns engine snippets; serving one from the other's cache
        would silently keep the old backend's results after the owner changed the setting.
        """
        self._tmdb(mock_tmdb)
        exa_cached = _DictCache()
        exa_cached.set(
            "websearch2:exa:movie:1", '{"results": [], "titles": []}', 1
        )  # Dune already searched via Exa this window
        searxng = _FakeSearch([make_result("Result", "text")], name="searxng")
        gather_candidates(
            mock_tmdb,
            [seed(1, "Dune")],
            sources=["llm_web"],
            curator=_NonNativeCurator("[]"),
            profile=web_profile(),
            search=searxng,
            web_search_cache=exa_cached,
            web_search_mode="searxng",
        )
        assert searxng.queries, "SearXNG must run its own search, not inherit Exa's cached page"
        assert "websearch2:searxng:movie:1" in exa_cached.store

    def test_each_backend_is_asked_for_its_own_result_depth(self, mock_tmdb):
        """SearXNG is free and returns thin snippets, so it pulls a wider page than Exa — the engine
        must ask each provider for ITS number rather than one shared constant."""
        self._tmdb(mock_tmdb)
        search = _FakeSearch([make_result("Result", "text")], name="searxng", results_per_query=10)
        gather_candidates(
            mock_tmdb,
            [seed(1, "Dune")],
            sources=["llm_web"],
            curator=_NonNativeCurator("[]"),
            profile=web_profile(),
            search=search,
            web_search_cache=_DictCache(),
            web_search_mode="searxng",
        )
        assert search.counts == [10]

    @pytest.mark.parametrize(("provider", "per_query"), [("exa", 5), ("searxng", 10)])
    def test_every_searched_seed_reaches_the_curator(self, mock_tmdb, provider: str, per_query: int):
        """The RAG prompt is capped at 40 results. Appending seed-by-seed and then slicing means the
        FIRST few seeds eat the whole cap, so the rest are searched, cached, counted — and dropped.

        At Exa's 5 results/search that silently lost 2 of 10 seeds; at SearXNG's 10 it lost 6, so
        choosing the local backend halved the curator's view of someone's taste while still paying for
        every search. Results are interleaved across seeds before the cap, so the budget is shared.
        """
        self._tmdb(mock_tmdb)
        seeds = [seed(i, f"Seed{i}") for i in range(1, 11)]
        search = _FakeSearch([], name=provider, results_per_query=per_query)
        search._results = None  # per-seed results are generated in the stub below

        def _per_seed(query, *, num_results=8):
            search.queries.append(query)
            search.counts.append(num_results)
            name = query.split("liked ")[1].split(" —")[0]
            # Distinct URLs per result: the union dedupes by url, so a shared one would collapse
            # every result to a single entry and the test would measure the dedupe, not the cap.
            return [
                SearchResult(title=f"{name} hit {i}", url=f"https://ex.com/{name}/{i}", text="t")
                for i in range(num_results)
            ]

        search.search = _per_seed
        curator = _NonNativeCurator("[]")
        gather_candidates(
            mock_tmdb,
            seeds,
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_cache=_DictCache(),
            web_search_mode=provider,
            recent_count=10,
        )
        prompt = curator.last_user
        covered = [s.title for s in seeds if s.title in prompt]
        assert len(search.queries) == 10, "every recent title is still searched"
        assert covered == [s.title for s in seeds], f"only {len(covered)}/10 seeds reached the prompt"

    def test_a_cached_title_is_not_researched(self, mock_tmdb):
        self._tmdb(mock_tmdb)
        search = _FakeSearch([make_result("Result", "text")])
        cache = _DictCache()
        cache.set(
            "websearch2:exa:movie:1", '{"results": [], "titles": []}', 1
        )  # Dune already searched by a prior user this window
        stats = GatherStats()
        gather_candidates(
            mock_tmdb,
            [seed(1, "Dune")],
            sources=["llm_web"],
            curator=_NonNativeCurator("[]"),
            profile=web_profile(),
            search=search,
            web_search_mode="exa",
            web_search_cache=cache,
            stats=stats,
        )
        assert search.queries == []  # served from cache — no billable Exa search
        # A cache hit is not a billed search, but it IS counted — else a fully-cached run reads
        # exa_searches:0 and looks like the source did nothing (it was the cache doing the work).
        assert stats.exa_searches == 0
        assert stats.exa_cache_hits == 1

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
            web_search_mode="exa",
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


class TestFilterCandidates:
    def _index(self):
        return {MediaType.MOVIE: {10: 1010, 20: 1020, 30: 1030}, MediaType.SHOW: {}}

    def test_keeps_only_library_matches_and_sets_rating_key(self):
        cands = [make_candidate(10, "In"), make_candidate(99, "Out")]
        kept = filter_candidates(cands, self._index(), watched_tmdb_ids=set(), excluded_genres=set())
        assert [c.tmdb_id for c in kept] == [10]
        assert kept[0].rating_key == 1010

    def test_drops_watched_and_excluded_genre(self):
        cands = [
            make_candidate(10, "Watched"),
            make_candidate(20, "Horror pick", genres=["Horror"]),
        ]
        kept = filter_candidates(
            cands,
            self._index(),
            watched_tmdb_ids={(10, MediaType.MOVIE)},
            excluded_genres={"horror"},
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
        )

        assert [c.title for c in kept] == ["Some Show"]

    def test_records_drop_reasons_without_changing_the_kept_list(self):
        """The optional `dropped` out-list is pure observation: the kept list is identical whether or
        not a caller passes it, and each drop is labelled with the reason it fell out — the data the
        trace needs to show every title in and out."""
        cands = [
            make_candidate(10, "Kept"),
            make_candidate(99, "Off-server"),
            make_candidate(20, "Watched"),
            make_candidate(30, "Horror", genres=["Horror"]),
        ]
        kwargs = dict(
            watched_tmdb_ids={(20, MediaType.MOVIE)},
            excluded_genres={"horror"},
        )
        without = filter_candidates([replace(c) for c in cands], self._index(), **kwargs)
        dropped: list[tuple] = []
        with_obs = filter_candidates([replace(c) for c in cands], self._index(), dropped=dropped, **kwargs)

        assert [c.tmdb_id for c in without] == [c.tmdb_id for c in with_obs] == [10]  # kept list unchanged
        assert {c.tmdb_id: reason for c, reason in dropped} == {
            99: "not_in_your_libraries",
            20: "already_watched",
            30: "excluded_genre",
        }


class TestGatherStats:
    """gather_candidates folds the AI candidate sources' token/Exa spend into a passed-in GatherStats.

    Regression cover for a real gap: llm_web set the curator's `last_tokens` but nothing
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
            web_search_mode="exa",
            stats=stats,
        )
        assert stats.tokens_by_source == {"llm_web": 99}
        assert stats.exa_searches == 1  # the search request itself, billed per search

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


class TestTheTraceReportsRealSearchCounts:
    """`queries` in the trace is a capped display sample (`_TRACE_SEEDS_SAMPLE`). The UI was counting
    it and presenting that as the number of searches, so a run that queried 30 seeds could report
    "searched 12" — the trace misstating the engine, which is the one thing it must never do."""

    def test_the_count_survives_the_sample_cap(self, mock_tmdb):
        from shortlist.engine.candidates import _TRACE_SEEDS_SAMPLE, GatherStats, gather_candidates
        from shortlist.engine.models import MediaType, Seed

        seeds = [
            Seed(tmdb_id=i, title=f"Seed {i}", media_type=MediaType.MOVIE, weight=1.0)
            for i in range(_TRACE_SEEDS_SAMPLE + 8)
        ]
        mock_tmdb.suggestions.return_value = [({"id": 900, "title": "Out", "genre_ids": [], "vote_average": 7.0}, 1.0)]
        mock_tmdb.genre_names.return_value = {}
        stats = GatherStats()

        gather_candidates(mock_tmdb, seeds, sources=["tmdb_similar"], stats=stats)

        entry = next(s for s in stats.trace["sources"] if s["source"] == "tmdb_similar")
        assert len(entry["queries"]) == _TRACE_SEEDS_SAMPLE, "the sample cap should still apply"
        assert entry["searched"]["movie"] == len(seeds), (
            f"the trace under-reported searches: said {entry['searched']} for {len(seeds)} seeds"
        )


class TestTheWebSourceCanFillTheLargestRow:
    """`_LLM_WEB_K` was a flat 20 while a row may be 40 (`MAX_ROW_SIZE`).

    It only bit in a non-default setup — `llm_web` enabled ALONE, on a row above 20 — but there it was
    the same shape as every other finding in this sweep: an invisible number quietly capping a visible
    one, with the row simply coming up short and nothing saying why.
    """

    def test_it_asks_for_enough_to_fill_the_largest_legal_row(self):
        from shortlist.engine.candidates import _LLM_WEB_K
        from shortlist.engine.models import MAX_ROW_SIZE

        assert _LLM_WEB_K >= MAX_ROW_SIZE, "a row of MAX_ROW_SIZE must be fillable from this source alone"

    def test_the_ask_reaches_the_provider(self, monkeypatch):
        """The wiring, not the constant: `k` must actually arrive at the completion. It is asked for
        in ONE call, so this is output tokens for extra titles — never extra requests."""
        from shortlist.engine import candidates as candidates_mod
        from shortlist.engine.models import MAX_ROW_SIZE

        seen: dict[str, int] = {}

        class Curator:
            supports_native_web_search = True
            last_tokens = 0

            def recommend_web(self, profile, seeds, k):
                seen["k"] = k
                return []

        candidates_mod.web_recommendations(
            Curator(),
            None,
            "native",
            object(),
            [],
            candidates_mod._LLM_WEB_K,
            candidates_mod.GatherStats(),
        )

        assert seen["k"] >= MAX_ROW_SIZE


class TestOriginalLanguageIsCarried:
    """The request gate can only pick a bar if the candidate actually knows its language.

    It is free in every TMDB list response, so this costs nothing — but a title that arrives without
    one is judged as PREFERRED, so a source that quietly drops the field would raise nobody's bar and
    the whole feature would look like it simply does not work.
    """

    def test_a_tmdb_suggestion_carries_its_original_language(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 42, "title": "Kaiju", "genre_ids": [], "vote_average": 8.7, "original_language": "ja"}]
        )
        pool = gather_candidates(mock_tmdb, [seed(1)])
        assert next(c for c in pool if c.tmdb_id == 42).language == "ja"

    def test_a_discover_result_carries_it_too(self, mock_tmdb):
        """discover is the source that most needs it: it sorts by GLOBAL popularity with no language
        constraint, which is where most of the non-English pool comes from in the first place."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: []
        mock_tmdb.discover.side_effect = lambda mt, gids, **kw: [
            {"id": 77, "title": "Popular Elsewhere", "genre_ids": [], "vote_average": 8.4, "original_language": "ko"}
        ]
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["tmdb_discover"])
        assert next(c for c in pool if c.tmdb_id == 77).language == "ko"

    def test_a_missing_original_language_is_empty_not_none(self, mock_tmdb):
        """ "" is the unknown sentinel the gate tests against; None would raise on `.lower()` upstream
        and compare wrong downstream."""
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 42, "title": "No Language", "genre_ids": [], "vote_average": 8.0}]
        )
        pool = gather_candidates(mock_tmdb, [seed(1)])
        assert next(c for c in pool if c.tmdb_id == 42).language == ""

    def test_it_is_lowercased_at_the_boundary(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 42, "title": "Shouty", "genre_ids": [], "vote_average": 8.0, "original_language": "JA"}]
        )
        pool = gather_candidates(mock_tmdb, [seed(1)])
        assert next(c for c in pool if c.tmdb_id == 42).language == "ja"

    def test_a_tmdb_source_fills_in_what_a_languageless_source_left_empty(self, mock_tmdb):
        """Trakt runs FIRST and builds candidates from its own fields, so a title both sources found
        would otherwise keep the unknown "" and be judged preferred when TMDB knew it was not."""
        trakt = SimpleNamespace(related=lambda tid, mt: [{"tmdb_id": 42, "title": "Kaiju", "year": 2020, "genres": []}])
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked(
            [{"id": 42, "title": "Kaiju", "genre_ids": [], "vote_average": 8.7, "original_language": "ja"}]
        )
        pool = gather_candidates(mock_tmdb, [seed(1)], sources=["trakt", "tmdb_similar"], trakt=trakt)
        merged = next(c for c in pool if c.tmdb_id == 42)
        assert merged.sources == {"trakt", "tmdb_similar"}
        assert merged.language == "ja", "the copy that KNOWS the language must win"


class TestStructuredExtractionPath:
    """Exa extracts the recommended titles server-side, so the curator picks from a title list
    rather than reading article prose. SearXNG can't, and keeps the prose path unchanged."""

    def _tmdb(self, mock_tmdb):
        mock_tmdb.suggestions.side_effect = lambda tid, mt: _ranked([])
        mock_tmdb.genre_names.return_value = {}
        mock_tmdb.search.side_effect = lambda title, mt, year=None: None
        return mock_tmdb

    def _titles(self, *names):
        return [TitleCandidate(title=n, year=2023, media="movie") for n in names]

    def _gather(self, mock_tmdb, search, curator, cache=None, seeds=None):
        return gather_candidates(
            mock_tmdb,
            seeds or [seed(1, "Dune")],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_mode="exa",
            web_search_cache=cache or _DictCache(),
        )

    def test_the_curator_is_given_the_extracted_titles_not_the_article_prose(self, mock_tmdb):
        """The whole point of the structured path: ~20 tokens a candidate instead of an 800-character
        block, so the cap stops rationing which seeds the curator ever sees."""
        self._tmdb(mock_tmdb)
        search = _FakeExtractingSearch(
            [make_result("An article", "a very long article body about many films")],
            self._titles("Silo", "Counterpart"),
        )
        curator = _NonNativeCurator("[]")
        self._gather(mock_tmdb, search, curator)

        prompt = curator.last_user
        assert "Silo" in prompt and "Counterpart" in prompt
        assert "a very long article body" not in prompt

    def test_it_falls_back_to_prose_when_extraction_comes_back_empty(self, mock_tmdb):
        """A mode that declines to synthesise, or a shape change at the provider, must degrade to the
        path that shipped before this — never to an empty source."""
        self._tmdb(mock_tmdb)
        search = _FakeExtractingSearch([make_result("An article", "article body text")], [])
        curator = _NonNativeCurator("[]")
        self._gather(mock_tmdb, search, curator)

        assert "article body text" in curator.last_user

    def test_a_title_found_by_several_seeds_is_listed_once(self, mock_tmdb):
        """Ten seeds asking "what to watch after X" name a lot of the same titles, and a prompt that
        lists Silo nine times spends its budget saying one thing."""
        self._tmdb(mock_tmdb)
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Silo"))
        curator = _NonNativeCurator("[]")
        self._gather(mock_tmdb, search, curator, seeds=[seed(1, "Dune"), seed(2, "Arrival")])

        assert curator.last_user.count("Silo") == 1

    def test_a_thin_result_is_cached_briefly_and_a_rich_one_for_a_fortnight(self, mock_tmdb):
        """Both extremes cost something, so the TTL is the dial rather than a yes/no.

        Caching a thin draw for 14 days serves a dud to every user who watched that title, and the
        provider is measurably variable — three identical calls returned 36, 45 and 38 usable titles.
        But NOT caching it bills a fresh search for every user, every night, forever, for any seed
        that genuinely has little written about it. A day covers one nightly run across the roster.
        """
        self._tmdb(mock_tmdb)
        cache = _DictCache()
        thin = _FakeExtractingSearch([make_result("a", "b")], self._titles("Silo"))  # 1 < the floor
        self._gather(mock_tmdb, thin, _NonNativeCurator("[]"), cache=cache)
        assert cache.ttls["websearch2:exa:movie:1"] == 24 * 3600

        cache = _DictCache()
        rich = _FakeExtractingSearch([make_result("a", "b")], self._titles("Silo", "Devs", "Counterpart"))
        self._gather(mock_tmdb, rich, _NonNativeCurator("[]"), cache=cache)
        assert cache.ttls["websearch2:exa:movie:1"] == 14 * 24 * 3600

    def test_the_seed_is_never_offered_back_as_a_recommendation(self, mock_tmdb):
        """An article headed "shows like Severance" names Severance, and the extraction lists it.

        Caught by running the real thing against live Exa: the curator proposed Severance to someone
        whose seed it was. The row never shows it — already-watched titles are dropped further down —
        but it burns one of the k proposal slots and reads as a bogus suggestion in the run trace.
        """
        self._tmdb(mock_tmdb)
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Dune", "Silo", "Devs", "From"))
        curator = _NonNativeCurator("[]")
        self._gather(mock_tmdb, search, curator, seeds=[seed(1, "Dune")])

        prompt = curator.last_user
        assert "Silo" in prompt  # the others survive
        assert "\n- Dune" not in prompt  # the seed does not

    def test_a_cached_entry_carries_both_shapes(self, mock_tmdb):
        """One request returns snippets AND titles, so the cache stores both — a reader must not have
        to know which backend wrote the entry."""
        self._tmdb(mock_tmdb)
        cache = _DictCache()
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Silo", "Devs", "From"))
        self._gather(mock_tmdb, search, _NonNativeCurator("[]"), cache=cache)

        stored = json.loads(cache.store["websearch2:exa:movie:1"])
        assert [t["title"] for t in stored["titles"]] == ["Silo", "Devs", "From"]
        assert stored["results"][0]["title"] == "a"

    def test_searxng_still_takes_the_prose_path(self, mock_tmdb):
        """It is a metasearch proxy with no synthesis of its own; the source branches on the provider,
        not on a setting."""
        self._tmdb(mock_tmdb)
        search = _FakeSearch([make_result("An article", "article body text")], name="searxng")
        curator = _NonNativeCurator("[]")
        gather_candidates(
            mock_tmdb,
            [seed(1, "Dune")],
            sources=["llm_web"],
            curator=curator,
            profile=web_profile(),
            search=search,
            web_search_mode="searxng",
            web_search_cache=_DictCache(),
        )
        assert "article body text" in curator.last_user

    def test_one_failing_seed_does_not_lose_the_other_seeds(self, mock_tmdb):
        """Exa's deeper modes run ~10s against a 100s ceiling at its CDN, and a request that exceeds
        it returns an HTML 524 — seen repeatedly while measuring. Left to raise, that single response
        disables `llm_web` for the whole user and discards every seed that already searched fine."""
        self._tmdb(mock_tmdb)

        class _FlakySearch(_FakeExtractingSearch):
            def search_detailed(self, query, *, num_results=8):
                if "Dune" in query:
                    raise RuntimeError("524 Origin Time-out")
                return super().search_detailed(query, num_results=num_results)

        search = _FlakySearch([make_result("a", "b")], self._titles("Silo", "Devs", "From"))
        curator = _NonNativeCurator("[]")
        self._gather(mock_tmdb, search, curator, seeds=[seed(1, "Dune"), seed(2, "Arrival")])

        assert "Silo" in curator.last_user  # Arrival's results still reached the curator

    def test_every_seed_failing_is_reported_as_a_failed_source(self, mock_tmdb):
        """Tolerating one dead seed must not quietly tolerate a dead backend — an empty return would
        read as "the web had nothing to suggest", and the caller's every-source-failed check exists
        precisely to make that loud."""
        self._tmdb(mock_tmdb)

        class _DeadSearch(_FakeExtractingSearch):
            def search_detailed(self, query, *, num_results=8):
                raise RuntimeError("524 Origin Time-out")

        with pytest.raises(RuntimeError, match="every candidate source failed"):
            gather_candidates(
                mock_tmdb,
                [seed(1, "Dune")],
                sources=["llm_web"],
                curator=_NonNativeCurator("[]"),
                profile=web_profile(),
                search=_DeadSearch([make_result("a", "b")], self._titles("Silo")),
                web_search_mode="exa",
                web_search_cache=_DictCache(),
            )

    def test_a_proposal_naming_a_seed_is_dropped(self, mock_tmdb):
        """The model proposes already-watched titles even when told not to, and even when they were
        stripped from the list it was shown — it fills them in from its own knowledge.

        Caught on a live 30-seed run: 6 of 40 proposals were seeds (Ted Lasso, Reacher, Slow Horses,
        Mr. Robot, The Capture, Star Trek: Strange New Worlds). Nothing broken reached a row, because
        watched titles are dropped downstream, but a seventh of the k the model was asked for was
        spent on titles that could never be used.
        """
        self._tmdb(mock_tmdb)
        reply = '[{"title": "Dune", "year": 2021, "media": "movie"}, {"title": "Silo", "year": 2023, "media": "show"}]'
        search = _FakeExtractingSearch([make_result("a", "b")], self._titles("Silo", "Devs", "From"))
        stats = GatherStats()
        out = web_recommendations(
            _NonNativeCurator(reply),
            search,
            "exa",
            web_profile(),
            [seed(1, "Dune")],
            5,
            stats,
            cache=_DictCache(),
        )
        assert [t["title"] for t in out] == ["Silo"]  # the seed is gone, the real suggestion stays
        assert stats.trace["web"]["already_watched"] == 1
        assert stats.trace["web"]["proposed"] == ["Silo (2023) [show]"]  # the trace shows what was used

    def test_a_native_curator_gets_the_same_filter(self, mock_tmdb):
        """Native providers never see a candidate list at all, so this is their only guard."""
        self._tmdb(mock_tmdb)

        class _Native:
            supports_native_web_search = True
            last_tokens = 0

            def recommend_web(self, profile, seeds, k):
                return [
                    {"title": "Dune", "year": 2021, "media": "movie"},
                    {"title": "Mr. Robot", "year": 2015, "media": "show"},
                    {"title": "Silo", "media": "show"},
                ]

        stats = GatherStats()
        # Watched but NOT a seed of this pool. Pools carry their own seed subset, so filtering on
        # seeds alone left exactly this case leaking on the live re-run.
        profile = SimpleNamespace(history=[SimpleNamespace(title="Mr. Robot")])
        out = web_recommendations(_Native(), None, "native", profile, [seed(1, "Dune")], 5, stats)
        assert [t["title"] for t in out] == ["Silo"]
