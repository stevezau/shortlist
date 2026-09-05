"""MDBList client: ratings-by-TMDB-id, whole-set caching, normalisation, and quota handling."""

from __future__ import annotations

import httpx
import pytest
import respx

from shortlist.engine.clients import http_retry
from shortlist.engine.clients.mdblist import (
    _BREAKER_TRIP,
    RATING_CACHE_TTL_S,
    MdbListClient,
    MdbListRateLimitError,
)
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
        self.ttls: dict[str, int] = {}  # last TTL each key was written with — `defer_recheck` re-stamps it

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl_s):
        self.store[key] = value
        self.ttls[key] = ttl_s


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
        # Only the first cost quota. The request gate budgets against THIS, not against how many
        # titles it inspected — billing cache hits is what starved it in production.
        assert client.live_lookups == 1

    @respx.mock
    def test_a_failed_call_still_counts_against_the_quota(self):
        respx.get("https://api.mdblist.com/tmdb/movie/9").mock(return_value=httpx.Response(500))
        client = MdbListClient("k", cache=_DictCache())
        assert client.rating(9, MediaType.MOVIE, "imdb") is None
        assert client.live_lookups == 1, "MDBList bills the request, not the useful answer"

    @respx.mock
    def test_it_stops_calling_a_dead_mdblist_instead_of_walking_the_whole_budget(self, monkeypatch):
        """The failure this pins cost over an hour of a real run.

        MDBList went down mid-run on 2026-09-04. Every lookup burned the full retry ladder (3
        attempts x a 15s timeout plus backoff, ~43s each) and returned a soft None, so the run walked
        its entire 100-lookup budget one dead call at a time — and would have done it again every
        night until MDBList came back.
        """
        monkeypatch.setattr("shortlist.engine.clients.http_retry.time.sleep", lambda _s: None)
        route = respx.get(url__regex=r"https://api\.mdblist\.com/tmdb/movie/\d+").mock(
            side_effect=httpx.ReadTimeout("mdblist is down")
        )
        client = MdbListClient("k", cache=_DictCache())

        for tmdb_id in range(1, 21):
            assert client.rating(tmdb_id, MediaType.MOVIE, "imdb") is None

        # 20 titles asked for, but the client stopped CALLING after it was sure — the point is that
        # the caller still gets a clean None for every one, so nothing downstream has to change.
        assert route.call_count <= 5 * http_retry.DEFAULT_ATTEMPTS, route.call_count

    @respx.mock
    def test_one_good_answer_clears_the_failures_behind_it(self):
        """A blip must not creep the client toward giving up over a whole run."""
        route = respx.get("https://api.mdblist.com/tmdb/movie/9")
        route.side_effect = [
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json=RATINGS),
            httpx.Response(500),
            httpx.Response(500),
            httpx.Response(200, json=RATINGS),
        ]
        client = MdbListClient("k", cache=_DictCache())
        for _ in range(6):
            client.rating(9, MediaType.MOVIE, "imdb")
        assert client._circuit_open is False

    @respx.mock
    def test_a_429_does_not_trip_the_breaker(self):
        """A quota verdict has its own handling and means something different from an outage."""
        respx.get("https://api.mdblist.com/tmdb/movie/9").mock(return_value=httpx.Response(429))
        client = MdbListClient("k", cache=_DictCache())
        for _ in range(6):
            with pytest.raises(MdbListRateLimitError):
                client.rating(9, MediaType.MOVIE, "imdb")
        assert client._circuit_open is False

    @respx.mock
    def test_a_404_does_not_trip_the_breaker(self):
        """A 404 means "not in our catalogue", not "we are down".

        Observed on the live server 2026-09-05: five unstocked titles in a row opened the breaker at
        03:37, so MDBList went dark for the REST of that run and every remaining title went unrated
        — which silently drops request ordering back to TMDB. The endpoint was healthy throughout
        (probed the same morning: a bad key answers 401, a retired path answers 404, and production
        was getting 404 with a valid key — i.e. a genuine per-title miss).
        """
        route = respx.get(url__regex=r"https://api\.mdblist\.com/tmdb/movie/\d+").mock(return_value=httpx.Response(404))
        client = MdbListClient("k", cache=_DictCache())

        for tmdb_id in range(1, 21):
            assert client.rating(tmdb_id, MediaType.MOVIE, "imdb") is None

        assert client._circuit_open is False
        # The real regression was invisible in the return values — every lookup answers None either
        # way. It only shows in whether the client KEPT ASKING, so assert the call count: with 404
        # counted as a failure this stopped at 5.
        assert route.call_count == 20, route.call_count

    @respx.mock
    def test_a_404_clears_the_failures_behind_it(self):
        """A miss proves the server is answering, so it resets the run of failures like a 200 does.

        Without this, four real failures plus a scattering of misses would still creep the client to
        the trip point over a long run.
        """
        respx.get("https://api.mdblist.com/tmdb/movie/9").mock(return_value=httpx.Response(404))
        client = MdbListClient("k", cache=_DictCache())
        client._consecutive_failures = _BREAKER_TRIP - 1

        assert client.rating(9, MediaType.MOVIE, "imdb") is None

        assert client._consecutive_failures == 0
        assert client._circuit_open is False

    @respx.mock
    def test_defer_recheck_restamps_the_ttl_without_refetching(self):
        route = respx.get("https://api.mdblist.com/tmdb/movie/273481").mock(
            return_value=httpx.Response(200, json=RATINGS)
        )
        cache = _DictCache()
        client = MdbListClient("k", cache=cache)
        client.rating(273481, MediaType.MOVIE, "imdb")
        assert cache.ttls["movie:273481"] == RATING_CACHE_TTL_S

        client.defer_recheck(273481, MediaType.MOVIE, 60 * 24 * 3600)

        assert cache.ttls["movie:273481"] == 60 * 24 * 3600
        assert route.call_count == 1, "holding a score must not buy it again"
        assert client.live_lookups == 1
        # The score itself is untouched, so a later run reads the real value — a deferral holds the
        # rating, never a verdict, and a lowered floor admits the title straight away.
        assert client.rating(273481, MediaType.MOVIE, "imdb") == (8.2, 102000)

    def test_defer_recheck_on_an_uncached_title_is_a_no_op(self):
        cache = _DictCache()
        MdbListClient("k", cache=cache).defer_recheck(1, MediaType.MOVIE, 999)
        assert cache.store == {}, "there is no verdict to hold on to, and none may be invented"

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
    def test_ping_reports_quota_from_a_single_user_call(self):
        route = respx.get("https://api.mdblist.com/user").mock(
            return_value=httpx.Response(200, json={"api_requests": 1000, "api_requests_count": 137})
        )

        # The Test button auto-fires on page load, so a second call would bill two requests against
        # the daily cap every time someone opens Settings.
        assert "137 of 1000" in MdbListClient("k").ping()
        assert route.call_count == 1
