"""Tests for the web-search clients (the llm_web external-search backends: Exa and SearXNG)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from shortlist.engine.clients.search import EXA_SEARCH_URL, ExaClient, SearchResult, SearxngClient

_FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "searxng_search.json").read_text())

_RESULTS = {
    "results": [
        {"title": "The 25 best sci-fi films of 2024", "url": "https://ex.com/a", "text": "Dune: Part Two ..."},
        {"title": "What to watch next", "url": "https://ex.com/b", "text": "Shogun is a must ..."},
        {"title": "", "url": "https://ex.com/c", "text": "no title — skipped"},
    ]
}


class TestExaClient:
    @respx.mock
    def test_search_sends_key_header_and_query_then_parses_results(self):
        route = respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(200, json=_RESULTS))
        out = ExaClient("exa-key-123").search("what to watch next if you liked Arrival", num_results=5)

        # SUT-controlled contract: the key rides the header (never the URL), and our query + count go up.
        request = route.calls.last.request
        assert request.headers["x-api-key"] == "exa-key-123"
        body = json.loads(request.content)
        assert body["query"] == "what to watch next if you liked Arrival"
        assert body["numResults"] == 5
        assert body["contents"]["text"]["maxCharacters"] > 0  # we ask for extracted text to feed the LLM

        # Parsing: two titled results kept in order, the title-less one dropped.
        assert out == [
            SearchResult(title="The 25 best sci-fi films of 2024", url="https://ex.com/a", text="Dune: Part Two ..."),
            SearchResult(title="What to watch next", url="https://ex.com/b", text="Shogun is a must ..."),
        ]

    @respx.mock
    def test_ping_returns_ok_string(self):
        respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": [{"title": "x"}]}))
        assert "ok" in ExaClient("k").ping()

    @respx.mock
    def test_search_raises_on_http_error(self):
        respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(401, json={"error": "bad key"}))
        with pytest.raises(httpx.HTTPStatusError):
            ExaClient("bad").search("q")

    @respx.mock
    def test_429_is_retried_then_succeeds(self, monkeypatch):
        import shortlist.engine.clients.http_retry as http_retry

        monkeypatch.setattr(http_retry.time, "sleep", lambda *_: None)  # don't actually wait in tests
        route = respx.post(EXA_SEARCH_URL)
        route.side_effect = [httpx.Response(429), httpx.Response(200, json={"results": [{"title": "ok"}]})]
        out = ExaClient("k").search("q")
        assert [r.title for r in out] == ["ok"]
        assert len(route.calls) == 2  # rate-limited once, retried once


_SEARX = "http://searx.local:8080"


class TestSearxngClient:
    @respx.mock
    def test_search_asks_for_json_and_maps_content_to_text(self):
        """The real fixture's `content` snippet is what the RAG prompt reads — Exa calls it `text`."""
        route = respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        out = SearxngClient(_SEARX).search("shows like Severance", num_results=3)

        params = route.calls.last.request.url.params
        assert params["q"] == "shows like Severance"
        assert params["format"] == "json"  # without this SearXNG serves HTML, and parsing would explode
        assert params["categories"] == "general"

        expected = _FIXTURE["results"][:3]
        assert [r.title for r in out] == [r["title"] for r in expected]
        assert [r.url for r in out] == [r["url"] for r in expected]
        assert [r.text for r in out] == [r["content"] for r in expected]

    @respx.mock
    def test_search_truncates_to_num_results(self):
        """SearXNG has no result-count parameter — it returns a whole page, so WE slice it."""
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        assert len(_FIXTURE["results"]) > 2  # fixture must be able to prove truncation happened
        assert len(SearxngClient(_SEARX).search("q", num_results=2)) == 2

    @respx.mock
    def test_search_skips_results_with_no_title(self):
        body = {"results": [{"title": "", "url": "u", "content": "c"}, {"title": "Keep", "url": "u2", "content": "c2"}]}
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=body))
        assert SearxngClient(_SEARX).search("q") == [SearchResult(title="Keep", url="u2", text="c2")]

    @respx.mock
    def test_403_says_json_format_must_be_enabled(self):
        """A stock SearXNG serves `formats: [html]` and answers format=json with a bare HTML 403.

        Verified against a real container: the body carries no machine-readable hint, so the ONLY way
        an owner learns what to fix is us saying it.
        """
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(403, html="<h1>Forbidden</h1>"))
        with pytest.raises(RuntimeError, match=r"search\.formats"):
            SearxngClient(_SEARX).search("q")

    @respx.mock
    def test_403_message_never_leaks_the_url_or_credentials(self):
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(403, html="nope"))
        with pytest.raises(RuntimeError) as excinfo:
            SearxngClient(_SEARX, username="admin", password="hunter2").search("q")
        assert "hunter2" not in str(excinfo.value)
        assert "admin" not in str(excinfo.value)

    @respx.mock
    def test_a_non_403_http_error_never_carries_the_url(self):
        """`raise_for_status()` embeds the full request URL, and that string reaches the API response
        and an `events` row. Pointing at the wrong host (verified live against a PMS, which answers
        400) would publish whatever the URL contains — see the userinfo test below."""
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(400, text="bad request"))
        with pytest.raises(RuntimeError) as excinfo:
            SearxngClient(_SEARX).search("q")
        assert "/search?" not in str(excinfo.value)
        assert "400" in str(excinfo.value)  # the owner still learns what happened

    @respx.mock
    def test_credentials_pasted_into_the_url_are_moved_out_of_it(self):
        """`http://user:pass@host:8080` is the obvious thing to paste for a password-protected
        instance. Left in the URL it would ride into every error string and log line, so it is moved
        to the auth header at construction and the stored URL is clean (rule 9)."""
        route = respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        client = SearxngClient("http://admin:hunter2@searx.local:8080")
        client.search("q")

        assert "hunter2" not in client._search_url
        assert route.calls.last.request.headers["Authorization"].startswith("Basic ")
        assert str(route.calls.last.request.url).startswith(f"{_SEARX}/search?")

    @respx.mock
    def test_a_password_in_the_url_never_reaches_an_error_message(self):
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(RuntimeError) as excinfo:
            SearxngClient("http://admin:hunter2@searx.local:8080").search("q")
        assert "hunter2" not in str(excinfo.value)

    @respx.mock
    def test_explicit_credentials_win_over_any_in_the_url(self):
        client = SearxngClient("http://old:stale@searx.local:8080", username="admin", password="new")
        assert client._auth == ("admin", "new")

    @respx.mock
    def test_search_returns_empty_when_engines_are_down(self):
        """One dead seed must not kill the whole source — other seeds still search (hence [] not raise)."""
        body = {"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]}
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=body))
        assert SearxngClient(_SEARX).search("q") == []

    @respx.mock
    def test_search_sends_basic_auth_when_credentials_are_set(self):
        route = respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        SearxngClient(_SEARX, username="admin", password="hunter2").search("q")
        assert route.calls.last.request.headers["Authorization"].startswith("Basic ")

    @respx.mock
    def test_no_auth_header_without_credentials(self):
        route = respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        SearxngClient(_SEARX).search("q")
        assert "Authorization" not in route.calls.last.request.headers

    @respx.mock
    @pytest.mark.parametrize("base", [_SEARX, f"{_SEARX}/", f"{_SEARX}///"])
    def test_trailing_slashes_in_the_configured_url_are_tolerated(self, base):
        """Owners paste URLs with and without a trailing slash; `//search` 404s on some reverse proxies."""
        route = respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        SearxngClient(base).search("q")
        assert str(route.calls.last.request.url).startswith(f"{_SEARX}/search?")

    @respx.mock
    def test_a_subpath_deployment_keeps_its_prefix(self):
        """SearXNG behind a reverse proxy at `/searxng` is a normal deployment. Resolving "search"
        as a RELATIVE reference would replace the last segment and silently drop the prefix."""
        route = respx.get("http://proxy.local/searxng/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        SearxngClient("http://proxy.local/searxng").search("q")
        assert route.called

    @respx.mock
    def test_ping_reports_the_result_count(self):
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=_FIXTURE))
        assert "ok" in SearxngClient(_SEARX).ping()

    @respx.mock
    def test_ping_names_the_dead_engines_when_nothing_came_back(self):
        """`search` shrugs at an empty page; the Settings test button must NOT — it has to explain."""
        body = {"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["brave", "too many requests"]]}
        respx.get(f"{_SEARX}/search").mock(return_value=httpx.Response(200, json=body))
        with pytest.raises(RuntimeError, match="duckduckgo"):
            SearxngClient(_SEARX).ping()

    @respx.mock
    def test_limiter_429_is_retried(self, monkeypatch):
        """SearXNG's bot limiter is ON by default and 429s API callers — a retry rides out the burst."""
        import shortlist.engine.clients.http_retry as http_retry

        monkeypatch.setattr(http_retry.time, "sleep", lambda *_: None)
        route = respx.get(f"{_SEARX}/search")
        route.side_effect = [httpx.Response(429), httpx.Response(200, json=_FIXTURE)]
        assert SearxngClient(_SEARX).search("q")
        assert len(route.calls) == 2

    def test_pulls_a_wider_page_than_exa_because_it_is_free(self):
        """Exa bills per search so it stays lean; SearXNG is local, and keyword hits need more depth."""
        assert SearxngClient(_SEARX).results_per_query > ExaClient("k").results_per_query
