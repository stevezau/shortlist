"""MDBList client: ratings-by-TMDB-id, whole-set caching, normalisation, and quota handling."""

from __future__ import annotations

import httpx
import pytest
import respx

from shortlist.engine.clients.mdblist import MdbListClient, MdbListRateLimitError
from shortlist.engine.models import MediaType

pytestmark = pytest.mark.integration

# A movie/tmdb response: IMDb/Trakt/TMDB are 0..10; Rotten Tomatoes ("tomatoes") + Metacritic 0..100.
RATINGS = {
    "ids": {"imdb": "tt1", "tmdb": 273481},
    "ratings": [
        {"source": "imdb", "value": 8.2, "votes": 102000},
        {"source": "trakt", "value": 7.9, "votes": 4000},
        {"source": "tomatoes", "value": 92, "votes": 250},  # critic score, 0..100
        {"source": "metacritic", "value": 75, "votes": 40},
        {"source": "tmdb", "value": 8.0, "votes": 9000},
    ],
}


class _DictCache:
    """A tiny in-memory Cache so we can prove the whole rating set is cached from one call."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value


class TestMdbListRating:
    @respx.mock
    def test_returns_the_chosen_source_normalised_to_a_0_10_scale(self):
        respx.get("https://api.mdblist.com/tmdb/movie/273481").mock(return_value=httpx.Response(200, json=RATINGS))
        client = MdbListClient("k")
        assert client.rating(273481, MediaType.MOVIE, "imdb") == (8.2, 102000)
        # RT is 0..100 in MDBList; normalised to 9.2 on our 0..10 floor scale.
        rt = client.rating(273481, MediaType.MOVIE, "tomatoes")
        assert rt is not None and rt[0] == 9.2

    @respx.mock
    def test_a_low_rt_score_normalises_below_the_floor_not_above_it(self):
        # Regression: a genuine RT 8% (0..100) must become 0.8, NOT 8.0 — else a panned title clears a
        # min_rating of 7 and gets auto-requested. Scale by source, never by magnitude.
        respx.get("https://api.mdblist.com/tmdb/movie/9").mock(
            return_value=httpx.Response(200, json={"ratings": [{"source": "tomatoes", "value": 8, "votes": 200}]})
        )
        rt = MdbListClient("k").rating(9, MediaType.MOVIE, "tomatoes")
        assert rt == (0.8, 0)  # 8/100 -> 0.8; RT votes aren't an audience count, so 0

    @respx.mock
    def test_one_call_caches_every_source(self):
        route = respx.get("https://api.mdblist.com/tmdb/movie/273481").mock(
            return_value=httpx.Response(200, json=RATINGS)
        )
        cache = _DictCache()
        client = MdbListClient("k", cache=cache)
        assert client.rating(273481, MediaType.MOVIE, "imdb")[0] == 8.2
        # A different source for the SAME title is served from cache — no second HTTP call.
        assert client.rating(273481, MediaType.MOVIE, "metacritic")[0] == 7.5
        assert route.call_count == 1

    @respx.mock
    def test_shows_hit_the_show_path(self):
        route = respx.get("https://api.mdblist.com/tmdb/show/99").mock(return_value=httpx.Response(200, json=RATINGS))
        MdbListClient("k").rating(99, MediaType.SHOW, "imdb")
        assert route.called

    @respx.mock
    def test_429_raises_rate_limit_error(self):
        respx.get("https://api.mdblist.com/tmdb/movie/5").mock(return_value=httpx.Response(429))
        with pytest.raises(MdbListRateLimitError):
            MdbListClient("k").rating(5, MediaType.MOVIE, "imdb")

    @respx.mock
    def test_missing_source_or_soft_error_returns_none(self):
        respx.get("https://api.mdblist.com/tmdb/movie/6").mock(
            return_value=httpx.Response(200, json={"ratings": [{"source": "imdb", "value": 8.0, "votes": 10}]})
        )
        # letterboxd isn't among the ones we surface -> None; a 500 -> None (soft).
        assert MdbListClient("k").rating(6, MediaType.MOVIE, "letterboxd") is None

    @respx.mock
    def test_usage_and_ping_read_the_user_endpoint(self):
        respx.get("https://api.mdblist.com/user").mock(
            return_value=httpx.Response(200, json={"api_requests": 1000, "api_requests_count": 137})
        )
        client = MdbListClient("k")
        assert client.usage() == (137, 1000)
        assert "137 of 1000" in client.ping()
