"""Tests for the web-search clients (the llm_web external-search backends: Exa and SearXNG)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from shortlist.engine.clients.search import (
    _EXA_TITLE_SCHEMA,
    DEFAULT_EXA_SEARCH_TYPE,
    EXA_SEARCH_URL,
    ExaClient,
    SearchResult,
    SearxngClient,
    TitleCandidate,
    extracts_titles,
)

_FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "searxng_search.json").read_text())
# A real Exa `deep-lite` response with `outputSchema` attached, recorded 2026-09-02. Verbatim except
# the `grounding` array, trimmed from 93 near-identical entries to 3 (40KB of the 59KB).
_EXA_STRUCTURED = json.loads((Path(__file__).parents[1] / "fixtures" / "exa_search_structured.json").read_text())

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


class TestExaStructuredExtraction:
    """Exa's `outputSchema` synthesis — the titles the found pages recommend, extracted server-side."""

    @respx.mock
    def test_extracts_titles_from_a_recorded_response(self):
        """Shape proven against a real response, not a hand-built one (rule 11).

        The synthesis lands at `output.content`, NOT at the top level and not where `/answer` puts
        it (`answer`) or where `/agent` does (`output.structured`) — three sibling endpoints, three
        different keys.
        """
        respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(200, json=_EXA_STRUCTURED))
        results, titles = ExaClient("k").search_detailed("shows like Severance")

        assert len(results) == 3  # the page snippets still come back alongside
        assert len(titles) == 31
        assert TitleCandidate(title="Lost", year=2004, media="show") in titles
        # Half the extraction carries no year, because the source article never printed one. The
        # recorded response has 15 of 31 — the resolver has to cope, so the parser must not drop them.
        assert sum(1 for t in titles if t.year is None) == 16
        assert TitleCandidate(title="From", year=None, media="show") in titles

    @respx.mock
    def test_sends_the_configured_search_type_and_a_schema(self):
        route = respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(200, json=_EXA_STRUCTURED))
        ExaClient("k", search_type="deep").search_detailed("q", num_results=10)

        body = json.loads(route.calls.last.request.content)
        assert body["type"] == "deep"
        assert body["numResults"] == 10
        assert body["outputSchema"]["properties"]["titles"]["type"] == "array"
        assert body["systemPrompt"]

    def test_an_unknown_search_type_falls_back_to_the_default(self):
        """A typo in the setting would otherwise be a 400 on every seed of every run, all night."""
        assert ExaClient("k", search_type="deep-litex")._search_type == DEFAULT_EXA_SEARCH_TYPE
        assert ExaClient("k", search_type="")._search_type == DEFAULT_EXA_SEARCH_TYPE

    def test_the_default_is_not_auto(self):
        """`auto` returned ZERO titles on a live run with 26k characters of page text in front of it,
        and 8-13 where `deep-lite` found 36-47. It is offered, but it must not be what people get."""
        assert DEFAULT_EXA_SEARCH_TYPE == "deep-lite"

    def test_the_schema_stays_at_three_fields(self):
        """A regression guard with a live cause: adding a fourth field (a one-line `why` per title)
        made every `deep-lite` search exceed Cloudflare's 100s limit and return an HTML 524 — 5 of 5
        attempts at ~125s each, against 200-in-10s without it. Exa's synthesis time scales with what
        it is asked to write and the ceiling is the CDN's, so a new field needs a live check first."""
        fields = _EXA_TITLE_SCHEMA["properties"]["titles"]["items"]["properties"]
        assert sorted(fields) == ["media", "title", "year"]

    @respx.mock
    def test_a_response_with_no_synthesis_still_returns_its_snippets(self):
        """Degrade, never fail: a mode that declines to synthesise (or a shape change at Exa) leaves
        the page text, and the caller falls back to the prose path that shipped before this."""
        respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(200, json=_RESULTS))
        results, titles = ExaClient("k").search_detailed("q")
        assert titles == []
        assert [r.title for r in results] == ["The 25 best sci-fi films of 2024", "What to watch next"]

    @respx.mock
    def test_plain_search_still_returns_only_snippets(self):
        """`search()` is the protocol both backends share; the extraction rides on the same request."""
        respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(200, json=_EXA_STRUCTURED))
        assert all(isinstance(r, SearchResult) for r in ExaClient("k").search("q"))

    def test_only_exa_advertises_extraction(self):
        """SearXNG is a metasearch proxy with no synthesis of its own — the source branches on this."""
        assert extracts_titles(ExaClient("k")) is True
        assert extracts_titles(SearxngClient(_SEARX)) is False


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
        # Free — measured against a real instance, identical yield with and without — and a
        # "what to watch next" row has no business surfacing adult results.
        assert params["safesearch"] == "1"

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

    def test_both_backends_ask_for_ten_results_for_different_reasons(self):
        """Ten each, and neither number is arbitrary.

        Exa used to ask for five because it bills per search and each result carried 800 characters
        of page text that had to fit a capped RAG prompt. Neither half holds now: Exa includes the
        first ten results in the base price (past ten bills $1/1k on top), a measured sweep found
        n=5, 10 and 20 all yielding the same range of usable titles, and the structured path sends a
        title list rather than prose so prompt size no longer rations the count. SearXNG returns a
        whole page regardless, so ten is simply how much of it we keep.
        """
        assert ExaClient("k").results_per_query == 10
        assert SearxngClient(_SEARX).results_per_query == 10


class TestSchemaSupportIsRemembered:
    """A model that rejects the response schema rejects it every time. Learning that once per process
    is the difference between one wasted request and one per user per night, seen only at DEBUG."""

    def test_openai_stops_sending_the_schema_after_a_rejection(self):
        from shortlist.engine.curator.openai import OpenAICurator

        curator = OpenAICurator.__new__(OpenAICurator)
        curator._model = "gpt-4o-mini"
        curator._schema_supported = True
        sent: list[bool] = []

        class _Responses:
            def create(self, **kw):
                sent.append("text" in kw)
                if "text" in kw:
                    import openai

                    # The base error class, because what matters is that the SDK raised something we
                    # catch — not which subclass a given model happens to answer with.
                    raise openai.OpenAIError("this model does not support text.format with web_search")
                return SimpleNamespace(output_text='[{"title": "Silo", "year": 2023, "media": "show"}]', usage=None)

        curator._client = SimpleNamespace(responses=_Responses())
        profile = SimpleNamespace(history=[])

        assert curator.recommend_web(profile, [], 5)[0]["title"] == "Silo"
        assert sent == [True, False]  # tried with the schema, fell back without it

        assert curator.recommend_web(profile, [], 5)[0]["title"] == "Silo"
        assert sent == [True, False, False]  # second run never tries the schema again


class TestExaTimeouts:
    def test_the_wait_matches_the_mode(self):
        """One timeout cannot serve every mode. Measured: the cheap modes answer in 2.5-4.6s,
        `deep-lite`/`deep` in 6.5-18s, `deep-reasoning` in 24-39s — and Exa does hang rather than
        answer (1 of 6 `deep-lite` searches never returned). Too low cuts off a legitimate slow
        search; too high spends a minute of every nightly run waiting on one that is not coming.
        """
        assert ExaClient("k", search_type="fast")._timeout < ExaClient("k", search_type="deep-lite")._timeout
        assert ExaClient("k", search_type="deep-lite")._timeout < ExaClient("k", search_type="deep-reasoning")._timeout
        # Comfortably above the slowest measured success for each mode, and no more.
        assert ExaClient("k", search_type="deep-lite")._timeout >= 40
        assert ExaClient("k", search_type="deep-reasoning")._timeout >= 60

    def test_an_explicit_timeout_still_wins(self):
        assert ExaClient("k", search_type="deep", timeout=5.0)._timeout == 5.0
