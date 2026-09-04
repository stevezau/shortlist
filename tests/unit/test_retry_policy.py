"""Every provider retries the blips it can safely retry — and only those.

The gap this pins cost real candidates: Exa's search went through the MUTATION retry path, which
retries 429 alone, so a read timeout or a 502 lost the search outright. The first real 46-user run
lost 13 of 21 searches that way. A search is idempotent — repeating it changes nothing — so it now
retries the same wide set a GET does.

The other half matters just as much: a genuine mutation must NOT gain that behaviour, because a
retried write can double a Radarr add or a share-filter change.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from shortlist.engine.clients import http_retry
from shortlist.engine.clients.search import ExaClient, SearxngClient

URL = "https://example.test/search"


def _no_backoff(monkeypatch):
    """Retries really do sleep; the point here is the policy, not the wait."""
    monkeypatch.setattr(http_retry.time, "sleep", lambda _s: None)


class TestAnIdempotentPostRetriesLikeARead:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
    @respx.mock
    def test_it_retries_every_blip_status(self, status, monkeypatch):
        _no_backoff(monkeypatch)
        route = respx.post(URL).mock(side_effect=[httpx.Response(status), httpx.Response(200, json={"ok": True})])
        assert http_retry.idempotent_post(URL, json={}).status_code == 200
        assert route.call_count == 2

    @respx.mock
    def test_it_retries_a_read_timeout(self, monkeypatch):
        """The case that lost the searches: the request landed, the answer never came."""
        _no_backoff(monkeypatch)
        route = respx.post(URL).mock(
            side_effect=[httpx.ReadTimeout("too slow"), httpx.Response(200, json={"ok": True})]
        )
        assert http_retry.idempotent_post(URL, json={}).status_code == 200
        assert route.call_count == 2

    @respx.mock
    def test_it_gives_up_rather_than_retrying_for_ever(self, monkeypatch):
        _no_backoff(monkeypatch)
        route = respx.post(URL).mock(return_value=httpx.Response(503))
        assert http_retry.idempotent_post(URL, json={}).status_code == 503
        assert route.call_count == http_retry.DEFAULT_ATTEMPTS

    @respx.mock
    def test_a_4xx_that_is_not_rate_limiting_is_not_retried(self, monkeypatch):
        """A bad key is an answer, not a blip — retrying it just wastes the run's time."""
        _no_backoff(monkeypatch)
        route = respx.post(URL).mock(return_value=httpx.Response(401))
        assert http_retry.idempotent_post(URL, json={}).status_code == 401
        assert route.call_count == 1


class TestMutationsStayConservative:
    @respx.mock
    def test_a_mutation_does_NOT_retry_a_5xx(self, monkeypatch):
        """The write may already have applied — a blind retry doubles it."""
        _no_backoff(monkeypatch)
        route = respx.post(URL).mock(return_value=httpx.Response(500))
        assert http_retry.request("POST", URL, json={}).status_code == 500
        assert route.call_count == 1

    @respx.mock
    def test_a_mutation_does_NOT_retry_a_read_timeout(self, monkeypatch):
        _no_backoff(monkeypatch)
        respx.post(URL).mock(side_effect=httpx.ReadTimeout("too slow"))
        with pytest.raises(httpx.ReadTimeout):
            http_retry.request("POST", URL, json={})


class TestTheSearchBackendsUseTheRightPolicy:
    @pytest.mark.parametrize("status", [429, 502])
    @respx.mock
    def test_exa_survives_a_blip(self, status, monkeypatch):
        _no_backoff(monkeypatch)
        route = respx.post("https://api.exa.ai/search").mock(
            side_effect=[httpx.Response(status), httpx.Response(200, json={"results": [{"title": "x"}]})]
        )
        assert ExaClient("k").search("q")
        assert route.call_count == 2

    @respx.mock
    def test_exa_survives_a_read_timeout(self, monkeypatch):
        _no_backoff(monkeypatch)
        route = respx.post("https://api.exa.ai/search").mock(
            side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, json={"results": [{"title": "x"}]})]
        )
        assert ExaClient("k").search("q")
        assert route.call_count == 2

    @respx.mock
    def test_searxng_survives_a_blip(self, monkeypatch):
        """SearXNG was already on the read policy; this stops a refactor quietly moving it."""
        _no_backoff(monkeypatch)
        route = respx.get("http://searx.test/search").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"results": [{"title": "x"}]})]
        )
        assert SearxngClient("http://searx.test").search("q")
        assert route.call_count == 2
