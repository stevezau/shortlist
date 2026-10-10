"""TMDB, Trakt, Tautulli and the removed OMDb client."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from shortlist.engine.clients.tautulli import TautulliClient
from shortlist.engine.clients.tmdb import TmdbClient
from shortlist.engine.clients.trakt import TraktClient, TraktError
from shortlist.engine.models import MediaType
from tests.unit.clients_support import FIXTURES


class _MemoryCache:
    """Minimal in-memory Cache (get/set) for exercising the client caches without a DB or file."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


class TestTmdbClient:
    @respx.mock
    def test_suggestions_pools_recommendations_and_similar(self):
        respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 10}, {"id": 20}]})
        )
        respx.get("https://api.themoviedb.org/3/movie/1/similar").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 20}, {"id": 30}]})
        )
        pooled = TmdbClient("k").suggestions(1, MediaType.MOVIE)
        assert sorted(item["id"] for item, _affinity in pooled) == [10, 20, 30]
        affinities = {item["id"]: affinity for item, affinity in pooled}
        assert affinities[10] > affinities[30], "/recommendations vouches harder than /similar"
        # id 20 is second in /recommendations and FIRST in /similar. With lists this short the
        # /similar claim (0.6, top of its list) actually beats the /recommendations one (0.5,
        # bottom of its list) — and taking the max is the point: a title keeps the best case made
        # for it, whichever endpoint made it.
        assert affinities[20] == 0.6
        assert affinities[10] == 1.0 and affinities[30] == pytest.approx(0.3)

    @respx.mock
    def test_discover_queries_genres_and_returns_results(self):
        route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 7}, {"id": 8}]})
        )
        results = TmdbClient("k").discover(MediaType.MOVIE, [18, 28], min_votes=200)
        assert [r["id"] for r in results] == [7, 8]
        # The genre/sort/vote filters must reach TMDB (they're the whole point of discover).
        params = route.calls.last.request.url.params
        assert params.get("with_genres") == "18,28"
        assert params.get("sort_by") == "popularity.desc"
        assert params.get("vote_count.gte") == "200"

    @respx.mock
    def test_discover_all_reads_every_page_in_a_stable_order(self):
        """A season's list is read to the end. Paged by popularity it silently loses ~7% of titles, because
        popularity moves between page reads; by release date it returns them all
        (tests/fixtures/tmdb_discover_paged.json)."""
        recorded = json.loads((FIXTURES / "tmdb_discover_paged.json").read_text())
        pages = {1: recorded["movie_page_1"], 34: recorded["movie_last_page"]}
        blank = {"page": 0, "total_pages": 34, "total_results": 676, "results": []}

        def serve(request):
            page = int(request.url.params["page"])
            return httpx.Response(200, json=pages.get(page, {**blank, "page": page}))

        route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(side_effect=serve)
        titles = TmdbClient("k").discover_all(MediaType.MOVIE, {"with_keywords": "3335|180193"})

        assert sorted(int(call.request.url.params["page"]) for call in route.calls) == list(range(1, 35))
        params = route.calls.last.request.url.params
        assert params["sort_by"] == "primary_release_date.asc"
        assert params["include_adult"] == "false"
        assert params["with_keywords"] == "3335|180193"
        expected = [r["id"] for r in recorded["movie_page_1"]["results"] + recorded["movie_last_page"]["results"]]
        assert sorted(t["id"] for t in titles) == sorted(expected)

    @respx.mock
    def test_discover_all_sorts_shows_by_first_air_date(self):
        """TV has no release date to sort on; an unknown sort key is silently ignored rather than refused."""
        recorded = json.loads((FIXTURES / "tmdb_discover_paged.json").read_text())
        route = respx.get("https://api.themoviedb.org/3/discover/tv").mock(
            return_value=httpx.Response(200, json=recorded["tv_page_1"])
        )
        TmdbClient("k").discover_all(MediaType.SHOW, {"with_keywords": "3335"})
        assert route.calls.last.request.url.params["sort_by"] == "first_air_date.asc"

    @respx.mock
    def test_discover_all_stops_at_page_500(self):
        """TMDB answers HTTP 400 for a page past 500, so asking for one would fail the whole list."""
        route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
            return_value=httpx.Response(200, json={"page": 1, "total_pages": 800, "results": []})
        )
        TmdbClient("k").discover_all(MediaType.MOVIE, {"with_genres": "27"})
        requested = [int(call.request.url.params["page"]) for call in route.calls]
        assert max(requested) == 500 and len(requested) == 500

    @respx.mock
    def test_discover_all_is_cached_as_one_entry_and_read_back_without_a_call(self):
        """One entry, not one per page: pages cached at different moments would expire at different moments
        and be re-read against a list that has shifted since — the drift the stable sort exists to avoid."""
        route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
            return_value=httpx.Response(
                200,
                json={
                    "page": 1,
                    "total_pages": 2,
                    "results": [{"id": 7, "title": "A", "genre_ids": [27], "vote_average": 6.1, "overview": "x" * 500}],
                },
            )
        )
        cache = _MemoryCache()
        first = TmdbClient("k", cache=cache).discover_all(MediaType.MOVIE, {"with_genres": "27"})
        calls = len(route.calls)
        second = TmdbClient("k", cache=cache).discover_all(MediaType.MOVIE, {"with_genres": "27"})

        assert len(route.calls) == calls
        assert len(cache.store) == 1
        assert second == first
        # Only what a candidate is built from is kept: a Christmas list is ~3,700 titles, and overviews
        # alone would make it megabytes.
        assert first == [{"id": 7, "title": "A", "genre_ids": [27], "vote_average": 6.1}]

    @respx.mock
    def test_discover_all_reads_its_pages_under_the_callers_logging_context(self):
        """The pages are read on a pool, whose threads start with an empty context — so a warning from a
        page read lost the run it belonged to and never reached that run's activity log."""
        from loguru import logger

        def serve(request):
            page = int(request.url.params["page"])
            logger.warning("probe page {}", page)
            return httpx.Response(200, json={"page": page, "total_pages": 3, "results": [{"id": page}]})

        respx.get("https://api.themoviedb.org/3/discover/movie").mock(side_effect=serve)
        tagged: dict[str, object] = {}
        handler = logger.add(
            lambda message: tagged.__setitem__(message.record["message"], message.record["extra"].get("probe_run")),
            level="WARNING",
            filter=lambda record: record["message"].startswith("probe page"),
        )
        try:
            with logger.contextualize(probe_run=7):
                TmdbClient("k").discover_all(MediaType.MOVIE, {"with_genres": "27"})
        finally:
            logger.remove(handler)

        assert tagged == {"probe page 1": 7, "probe page 2": 7, "probe page 3": 7}

    @respx.mock
    def test_discover_all_raises_rather_than_returning_part_of_a_list(self):
        """A list missing a page would quietly drop titles from someone's season — and be cached for a week."""

        def serve(request):
            if request.url.params["page"] == "2":
                return httpx.Response(500)
            return httpx.Response(200, json={"page": 1, "total_pages": 2, "results": [{"id": 1}]})

        respx.get("https://api.themoviedb.org/3/discover/movie").mock(side_effect=serve)
        cache = _MemoryCache()
        with pytest.raises(RuntimeError, match="HTTP 500"):
            TmdbClient("k", cache=cache).discover_all(MediaType.MOVIE, {"with_genres": "27"})
        assert cache.store == {}

    @respx.mock
    def test_discover_all_refuses_a_page_that_came_back_empty_handed(self, caplog):
        """A 404 on one page reads as {} — which, taken as an empty page, would cache a short list for a week."""

        def serve(request):
            if request.url.params["page"] == "2":
                return httpx.Response(404)
            return httpx.Response(200, json={"page": 1, "total_pages": 2, "results": [{"id": 1}]})

        respx.get("https://api.themoviedb.org/3/discover/movie").mock(side_effect=serve)
        cache = _MemoryCache()
        with pytest.raises(RuntimeError, match="page 2"):
            TmdbClient("k", cache=cache).discover_all(MediaType.MOVIE, {"with_genres": "27"})
        assert cache.store == {}

    @respx.mock
    @pytest.mark.parametrize(
        "params",
        [{}, {"with_keywords": ""}, {"with_genres": " "}, {"with_keywords": "|"}, {"vote_count.gte": 200}],
    )
    def test_discover_all_refuses_a_query_with_no_filter(self, params):
        """With no filter TMDB answers with every title it holds: 500 pages read, and cached for a week."""
        route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
            return_value=httpx.Response(200, json={"page": 1, "total_pages": 1, "results": []})
        )
        with pytest.raises(ValueError, match="filter"):
            TmdbClient("k").discover_all(MediaType.MOVIE, params)
        assert not route.called

    @respx.mock
    def test_discover_with_no_genres_makes_no_call(self):
        # No genres -> no query at all (respx would raise on any unmocked request).
        assert TmdbClient("k").discover(MediaType.MOVIE, []) == []

    @respx.mock
    def test_search_sends_only_the_query_and_ranks_the_year_locally(self):
        """The year ranks, it no longer filters — and that is the point of the change.

        Sending `year=` (or `first_air_date_year=`) made TMDB exclude everything else, so a proposal
        whose year was one out returned NOTHING and the title was lost entirely. Sources disagree
        about years constantly: a series gets dated by its premiere, a film by its festival run.
        Ranking keeps the near-miss and still puts the right release first.
        """
        route = respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 42, "title": "Dune"}, {"id": 43}]})
        )
        found = TmdbClient("k").search("Dune", MediaType.MOVIE, year=2021)
        assert found["id"] == 42
        params = route.calls.last.request.url.params
        assert params.get("query") == "Dune"
        assert params.get("year") is None
        assert params.get("first_air_date_year") is None

    @respx.mock
    def test_search_prefers_an_exact_title_over_a_more_popular_one(self):
        """TMDB's own order is popularity, which is quietly wrong for shared and remade titles —
        exactly the case that puts an unrelated film in someone's row."""
        respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 1, "title": "Poor Things: The Making Of", "release_date": "2024-01-01"},
                        {"id": 2, "title": "Poor Things", "release_date": "2023-12-07"},
                    ]
                },
            )
        )
        assert TmdbClient("k").search("Poor Things", MediaType.MOVIE, year=2023)["id"] == 2

    @respx.mock
    def test_search_uses_the_year_to_separate_two_exact_titles(self):
        """A remake and its original share a title exactly, so only the year can tell them apart."""
        respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 1, "title": "Dune", "release_date": "1984-12-14"},
                        {"id": 2, "title": "Dune", "release_date": "2021-09-15"},
                    ]
                },
            )
        )
        assert TmdbClient("k").search("Dune", MediaType.MOVIE, year=2021)["id"] == 2
        assert TmdbClient("k").search("Dune", MediaType.MOVIE, year=1984)["id"] == 1

    @respx.mock
    def test_search_keeps_a_title_whose_year_is_one_out(self):
        """Half of what web extraction produces has no year at all, and plenty of the rest is off by
        one. Neither may cost us the title — under the old filter, both did."""
        respx.get("https://api.themoviedb.org/3/search/tv").mock(
            return_value=httpx.Response(
                200, json={"results": [{"id": 95396, "name": "Severance", "first_air_date": "2022-02-17"}]}
            )
        )
        assert TmdbClient("k").search("Severance", MediaType.SHOW, year=2023)["id"] == 95396
        assert TmdbClient("k").search("Severance", MediaType.SHOW)["id"] == 95396

    @respx.mock
    def test_search_ignores_punctuation_differences_in_the_title(self):
        """A title copied out of an article carries a curly apostrophe; TMDB stores a straight one."""
        respx.get("https://api.themoviedb.org/3/search/tv").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {"id": 1, "name": "Daredevil", "first_air_date": "2015-04-10"},
                        {"id": 2, "name": "Marvel's Daredevil", "first_air_date": "2015-04-10"},
                    ]
                },
            )
        )
        # The curly apostrophe is the point of the test, not a typo — hence the noqa.
        assert TmdbClient("k").search("Marvel’s Daredevil", MediaType.SHOW)["id"] == 2  # noqa: RUF001

    @respx.mock
    def test_search_falls_back_to_tmdb_order_when_nothing_matches_well(self):
        """No title or year signal to go on — keep the old behaviour rather than invent a preference."""
        respx.get("https://api.themoviedb.org/3/search/movie").mock(
            return_value=httpx.Response(200, json={"results": [{"id": 7}, {"id": 8}]})
        )
        assert TmdbClient("k").search("Something Else", MediaType.MOVIE)["id"] == 7

    @respx.mock
    def test_search_returns_none_when_nothing_matches(self):
        respx.get("https://api.themoviedb.org/3/search/tv").mock(return_value=httpx.Response(200, json={"results": []}))
        assert TmdbClient("k").search("Nonexistent Show", MediaType.SHOW) is None

    def test_search_blank_title_makes_no_call(self):
        # An empty proposed title never hits the network (respx.mock not needed — no request).
        assert TmdbClient("k").search("   ", MediaType.MOVIE) is None

    @respx.mock
    def test_tvdb_id_reads_external_ids_for_a_show(self):
        respx.get("https://api.themoviedb.org/3/tv/95396/external_ids").mock(
            return_value=httpx.Response(200, json={"tvdb_id": 371980, "imdb_id": "tt11280740"})
        )
        assert TmdbClient("k").tvdb_id(95396, MediaType.SHOW) == 371980

    @respx.mock
    def test_tvdb_id_is_none_when_tmdb_has_no_mapping(self):
        # TMDB returns the key present but null for titles with no TheTVDB entry.
        respx.get("https://api.themoviedb.org/3/tv/95396/external_ids").mock(
            return_value=httpx.Response(200, json={"tvdb_id": None})
        )
        assert TmdbClient("k").tvdb_id(95396, MediaType.SHOW) is None

    @respx.mock
    def test_poster_path_reads_the_movie_detail_endpoint(self):
        respx.get("https://api.themoviedb.org/3/movie/603").mock(
            return_value=httpx.Response(200, json={"id": 603, "poster_path": "/matrix.jpg"})
        )
        assert TmdbClient("k").poster_path(603, MediaType.MOVIE) == "/matrix.jpg"

    @respx.mock
    def test_poster_path_reads_the_tv_detail_endpoint(self):
        # The movie/tv split is the whole branch in this method — a show must not be asked for at
        # /movie/{id}, which is a DIFFERENT title (ids are unique only within their namespace).
        respx.get("https://api.themoviedb.org/3/tv/95396").mock(
            return_value=httpx.Response(200, json={"id": 95396, "poster_path": "/severance.jpg"})
        )
        assert TmdbClient("k").poster_path(95396, MediaType.SHOW) == "/severance.jpg"

    @respx.mock
    def test_poster_path_is_empty_when_tmdb_has_no_artwork(self):
        # TMDB returns the key present but null for a title with no poster. This MUST become "" and
        # never None: it is written to a NOT NULL column, so a None would fail the whole persist.
        respx.get("https://api.themoviedb.org/3/movie/603").mock(
            return_value=httpx.Response(200, json={"id": 603, "poster_path": None})
        )
        assert TmdbClient("k").poster_path(603, MediaType.MOVIE) == ""

    @respx.mock
    def test_poster_path_is_empty_when_the_title_is_unknown(self):
        respx.get("https://api.themoviedb.org/3/movie/999999").mock(return_value=httpx.Response(404, json={}))
        assert TmdbClient("k").poster_path(999999, MediaType.MOVIE) == ""

    @respx.mock
    def test_cache_prevents_second_fetch(self):
        route = respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        respx.get("https://api.themoviedb.org/3/movie/1/similar").mock(
            return_value=httpx.Response(200, json={"results": []})
        )

        client = TmdbClient("k", cache=_MemoryCache())
        client.suggestions(1, MediaType.MOVIE)
        client.suggestions(1, MediaType.MOVIE)
        assert route.call_count == 1

    @respx.mock
    def test_404_returns_empty_not_error(self):
        respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(return_value=httpx.Response(404))
        respx.get("https://api.themoviedb.org/3/movie/1/similar").mock(return_value=httpx.Response(404))
        assert TmdbClient("k").suggestions(1, MediaType.MOVIE) == []

    @respx.mock
    def test_a_404_miss_is_cached_like_trakts_related_deliberately_does(self):
        """Without this, a title TMDB 404s on gets re-fetched every run for every user who has it as
        a seed — trakt.py's `related()` already caches its own misses, with a comment saying why."""
        route = respx.get("https://api.themoviedb.org/3/tv/95396/external_ids").mock(return_value=httpx.Response(404))
        client = TmdbClient("k", cache=_MemoryCache())

        first = client.tvdb_id(95396, MediaType.SHOW)
        second = client.tvdb_id(95396, MediaType.SHOW)

        assert first is None and second is None
        assert route.call_count == 1, "the second call must be a cache hit, not a second 404"

    @respx.mock
    def test_api_key_never_appears_in_error_messages(self):
        respx.get("https://api.themoviedb.org/3/movie/1/recommendations").mock(return_value=httpx.Response(500))
        with pytest.raises(RuntimeError) as excinfo:
            TmdbClient("SUPERSECRETKEY").suggestions(1, MediaType.MOVIE)
        assert "SUPERSECRETKEY" not in str(excinfo.value)
        assert "500" in str(excinfo.value)

    @respx.mock
    def test_list_item_is_a_list_shaped_title_with_its_genres_as_ids(self):
        """A season's hand picks and collection films arrive as bare ids; the pool reads list items, so the
        detail payload is cut to a list item's keys, with ``genres`` turned into the list's ``genre_ids``."""
        route = respx.get("https://api.themoviedb.org/3/movie/603").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 603,
                    "title": "The Matrix",
                    "release_date": "1999-03-31",
                    "genres": [{"id": 28, "name": "Action"}, {"id": 878, "name": "Science Fiction"}],
                    "vote_average": 8.2,
                    "vote_count": 26000,
                    "poster_path": "/matrix.jpg",
                    "overview": "A hacker learns the truth.",
                    "original_language": "en",
                    "runtime": 136,
                    "credits": {"cast": [{"name": "Keanu Reeves"}]},
                },
            )
        )
        item = TmdbClient("k").list_item(603, MediaType.MOVIE)
        assert item == {
            "id": 603,
            "title": "The Matrix",
            "release_date": "1999-03-31",
            "genre_ids": [28, 878],
            "vote_average": 8.2,
            "vote_count": 26000,
            "poster_path": "/matrix.jpg",
            "overview": "A hacker learns the truth.",
            "original_language": "en",
        }
        # The same cached read `details` makes, so a title the run already looked up costs nothing.
        assert route.calls.last.request.url.params["append_to_response"] == "credits"

    @respx.mock
    def test_list_item_names_a_show_by_its_tv_keys(self):
        respx.get("https://api.themoviedb.org/3/tv/1399").mock(
            return_value=httpx.Response(
                200, json={"id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17", "genres": []}
            )
        )
        item = TmdbClient("k").list_item(1399, MediaType.SHOW)
        assert item == {"id": 1399, "name": "Game of Thrones", "first_air_date": "2011-04-17", "genre_ids": []}

    @respx.mock
    def test_list_item_is_none_for_a_title_tmdb_no_longer_has(self):
        respx.get("https://api.themoviedb.org/3/movie/999999").mock(return_value=httpx.Response(404))
        assert TmdbClient("k").list_item(999999, MediaType.MOVIE) is None

    @respx.mock
    def test_search_keywords_counts_each_tags_films_from_discover(self):
        """The editor shows how many films each tag holds; page 1 of discover carries ``total_results``. The
        keyword search answers what TMDB really answered (`tmdb_search_keyword.json`, rule 11); the counts are
        the ones TMDB gave for those tags the same day."""
        recorded = json.loads((FIXTURES / "tmdb_search_keyword.json").read_text())
        search = respx.get("https://api.themoviedb.org/3/search/keyword").mock(
            return_value=httpx.Response(200, json=recorded)
        )
        totals = {"4543": 133, "337336": 1}
        discover = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
            side_effect=lambda request: httpx.Response(
                200, json={"page": 1, "results": [], "total_results": totals[request.url.params["with_keywords"]]}
            )
        )
        cache = _MemoryCache()

        found = TmdbClient("k", cache=cache).search_keywords("thanksgiving", limit=2)
        TmdbClient("k", cache=cache).search_keywords("thanksgiving", limit=2)

        assert found == [
            {"id": 4543, "name": "thanksgiving", "movies": 133},
            {"id": 337336, "name": "thanksgiving prayer", "movies": 1},
        ]
        assert search.calls[0].request.url.params["query"] == "thanksgiving"
        assert sorted(call.request.url.params["with_keywords"] for call in discover.calls) == ["337336", "4543"]
        assert all(call.request.url.params["include_adult"] == "false" for call in discover.calls)

    def test_search_keywords_for_a_blank_query_makes_no_call(self):
        assert TmdbClient("k").search_keywords("  ") == []

    @respx.mock
    @pytest.mark.parametrize("workers", [1, 4])
    def test_discover_all_returns_the_same_list_however_many_pages_it_reads_at_once(self, workers):
        recorded = json.loads((FIXTURES / "tmdb_discover_paged.json").read_text())
        pages = {1: recorded["movie_page_1"], 34: recorded["movie_last_page"]}
        blank = {"page": 0, "total_pages": 34, "total_results": 676, "results": []}
        route = respx.get("https://api.themoviedb.org/3/discover/movie").mock(
            side_effect=lambda request: httpx.Response(
                200, json=pages.get(int(request.url.params["page"]), {**blank, "page": request.url.params["page"]})
            )
        )

        titles = TmdbClient("k").discover_all(MediaType.MOVIE, {"with_keywords": "3335|180193"}, workers=workers)

        expected = [r["id"] for r in recorded["movie_page_1"]["results"] + recorded["movie_last_page"]["results"]]
        assert [t["id"] for t in titles] == expected, "pages reassemble in page order"
        requested = [int(call.request.url.params["page"]) for call in route.calls]
        assert sorted(requested) == list(range(1, 35))
        if workers == 1:
            assert requested == list(range(1, 35)), "one at a time, in order"


class TestRemovedOmdbClient:
    """OMDb was replaced by MDBList (one call returns IMDb/Trakt/RT/Metacritic, cached). See
    tests/unit/test_mdblist.py."""


class TestTautulliClient:
    @respx.mock
    def test_ping_success(self):
        route = respx.get("http://taut.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "success", "data": {}}})
        )
        assert TautulliClient("http://taut.test", "key").ping() is True
        assert route.calls.last.request.url.params["cmd"] == "status"

    @respx.mock
    def test_api_failure_raises(self):
        respx.get("http://taut.test/api/v2").mock(
            return_value=httpx.Response(200, json={"response": {"result": "error", "message": "bad key"}})
        )
        with pytest.raises(RuntimeError, match="bad key"):
            TautulliClient("http://taut.test", "key").friendly_names()

    @respx.mock
    def test_api_key_never_appears_in_error_messages(self):
        respx.get("http://taut.test/api/v2").mock(return_value=httpx.Response(502))
        with pytest.raises(RuntimeError) as excinfo:
            TautulliClient("http://taut.test", "SUPERSECRETKEY").friendly_names()
        assert "SUPERSECRETKEY" not in str(excinfo.value)
        assert "502" in str(excinfo.value)


class TestTraktClient:
    @respx.mock
    def test_related_crosses_tmdb_then_normalizes(self):
        respx.get("https://api.trakt.tv/search/tmdb/550").mock(
            return_value=httpx.Response(200, json=[{"movie": {"ids": {"slug": "fight-club-1999", "tmdb": 550}}}])
        )
        respx.get("https://api.trakt.tv/movies/fight-club-1999/related").mock(
            return_value=httpx.Response(
                200, json=[{"title": "Se7en", "year": 1995, "ids": {"tmdb": 807}, "genres": ["thriller"]}]
            )
        )
        out = TraktClient("cid").related(550, MediaType.MOVIE)
        assert out == [{"tmdb_id": 807, "title": "Se7en", "year": 1995, "genres": ["thriller"]}]

    @respx.mock
    def test_related_uses_the_show_endpoints_for_shows(self):
        search = respx.get("https://api.trakt.tv/search/tmdb/1399").mock(
            return_value=httpx.Response(200, json=[{"show": {"ids": {"slug": "game-of-thrones"}}}])
        )
        related = respx.get("https://api.trakt.tv/shows/game-of-thrones/related").mock(
            return_value=httpx.Response(
                200, json=[{"title": "Rome", "year": 2005, "ids": {"tmdb": 1234}, "genres": ["drama"]}]
            )
        )
        out = TraktClient("cid").related(1399, MediaType.SHOW)
        assert out == [{"tmdb_id": 1234, "title": "Rome", "year": 2005, "genres": ["drama"]}]
        assert related.called  # the /shows/ endpoint (not /movies/) was used
        assert search.calls.last.request.url.params.get("type") == "show"

    @respx.mock
    def test_unknown_seed_returns_empty_not_error(self):
        respx.get("https://api.trakt.tv/search/tmdb/999").mock(return_value=httpx.Response(200, json=[]))
        assert TraktClient("cid").related(999, MediaType.MOVIE) == []

    @respx.mock
    def test_bad_key_raises_clean_error_without_leaking_it(self):
        respx.get("https://api.trakt.tv/movies/trending").mock(return_value=httpx.Response(403))
        with pytest.raises(TraktError) as excinfo:
            TraktClient("secret-cid").ping()
        assert "rejected the API key" in str(excinfo.value)
        assert "secret-cid" not in str(excinfo.value)
        # Named as the likely cause, because Trakt made API keys VIP-only and a lapsed subscription
        # takes a working key down with it — "rejected" alone sent a reporter checking a good key.
        assert "VIP" in str(excinfo.value)

    @respx.mock
    def test_trakt_saying_not_vip_is_reported_as_that_and_not_as_a_bad_key(self):
        """Issue #73. 426 is Trakt's own "you are not a VIP" signal (docs.trakt.tv/docs/vip-methods),
        and it is the ONLY authoritative answer available to us: the endpoint that reports VIP status
        needs an OAuth user token, and Shortlist is deliberately client-id-only. Reporting it as a bad
        key would send someone to regenerate a key that is perfectly valid."""
        respx.get("https://api.trakt.tv/movies/trending").mock(return_value=httpx.Response(426))
        with pytest.raises(TraktError) as excinfo:
            TraktClient("secret-cid").ping()
        assert "426" in str(excinfo.value)
        assert "VIP" in str(excinfo.value)
        assert "rejected the API key" not in str(excinfo.value)
        assert "secret-cid" not in str(excinfo.value)

    @respx.mock
    def test_related_is_cached_across_calls(self):
        # The related graph depends only on (tmdb_id, media_type), so a second call — a second user,
        # or the next nightly run — must serve from cache without re-hitting Trakt.
        search = respx.get("https://api.trakt.tv/search/tmdb/550").mock(
            return_value=httpx.Response(200, json=[{"movie": {"ids": {"slug": "fight-club-1999", "tmdb": 550}}}])
        )
        related = respx.get("https://api.trakt.tv/movies/fight-club-1999/related").mock(
            return_value=httpx.Response(200, json=[{"title": "Se7en", "ids": {"tmdb": 807}}])
        )
        client = TraktClient("cid", cache=_MemoryCache())
        first = client.related(550, MediaType.MOVIE)
        second = client.related(550, MediaType.MOVIE)
        assert first == second
        assert search.call_count == 1 and related.call_count == 1  # second call served from cache

    @respx.mock
    def test_empty_related_is_cached_too(self):
        # A seed Trakt doesn't know stays unknown for the TTL rather than being re-looked-up every run.
        search = respx.get("https://api.trakt.tv/search/tmdb/999").mock(return_value=httpx.Response(200, json=[]))
        client = TraktClient("cid", cache=_MemoryCache())
        assert client.related(999, MediaType.MOVIE) == []
        assert client.related(999, MediaType.MOVIE) == []
        assert search.call_count == 1  # the miss was cached, not re-attempted

    @respx.mock
    def test_a_trakt_error_is_never_cached(self):
        # A failure must not poison the cache — the next run should retry, not serve []. (403 isn't
        # retried, so this stays fast: no backoff sleeps.)
        respx.get("https://api.trakt.tv/search/tmdb/550").mock(return_value=httpx.Response(403))
        client = TraktClient("cid", cache=_MemoryCache())
        with pytest.raises(TraktError):
            client.related(550, MediaType.MOVIE)
        assert client._cache.get("trakt:related:movie:550:20") is None
