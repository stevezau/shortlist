"""Curator matrix: null / anthropic / openai / google / openai_compatible, plus the hallucination validator."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

from shortlist.engine.curator import make_curator
from shortlist.engine.curator.base import (
    MAX_REASON_LEN,
    TONE_PRESETS,
    CuratorError,
    build_prompts,
    picks_schema,
    validate_picks,
)
from shortlist.engine.curator.null import NullCurator
from shortlist.engine.models import MediaType, PromptConfig, Seed
from tests.conftest import make_candidate, make_profile


def candidates(n: int = 3):
    return [
        make_candidate(
            i,
            f"Movie {i}",
            rating_key=1000 + i,
            seeds=[Seed(tmdb_id=900, title="Fargo", media_type=MediaType.MOVIE, weight=2.0)],
        )
        for i in range(1, n + 1)
    ]


class TestValidatePicks:
    def test_drops_hallucinated_ids_and_dedupes(self):
        raw = [
            {"tmdb_id": 1, "reason": "good"},
            {"tmdb_id": 777, "reason": "invented"},
            {"tmdb_id": 1, "reason": "dupe"},
        ]
        picks = validate_picks(raw, candidates(), k=5, provider_name="test")
        assert [p.tmdb_id for p in picks] == [1]
        assert picks[0].rating_key == 1001

    def test_reason_truncated_to_cap(self):
        raw = [{"tmdb_id": 1, "reason": "x" * 300}]
        picks = validate_picks(raw, candidates(), k=5, provider_name="test")
        assert len(picks[0].reason) == MAX_REASON_LEN

    def test_stops_at_k_and_ranks_sequentially(self):
        raw = [{"tmdb_id": i, "reason": "r"} for i in (1, 2, 3)]
        picks = validate_picks(raw, candidates(), k=2, provider_name="test")
        assert [(p.rank, p.tmdb_id) for p in picks] == [(1, 1), (2, 2)]

    def test_empty_reason_falls_back_to_seed_template(self):
        picks = validate_picks([{"tmdb_id": 1, "reason": ""}], candidates(), k=1, provider_name="test")
        assert picks[0].reason == "Because you watched Fargo"


class TestNullCurator:
    def test_keeps_order_and_templates_reasons(self):
        picks = NullCurator().curate(make_profile(), candidates(), k=2)
        assert [p.tmdb_id for p in picks] == [1, 2]
        assert picks[0].reason == "Because you watched Fargo"
        assert picks[0].seed_title == "Fargo"

    def test_factory_none(self):
        assert make_curator("none").name == "none"


class TestBuildPrompts:
    def test_prompt_contains_candidates_history_and_no_account_ids(self):
        profile = make_profile()
        profile.history = []
        _system, user = build_prompts(profile, candidates(), k=2)
        assert "tmdb_id=1" in user
        assert "Fargo" in user
        assert str(profile.plex_account_id) not in user  # titles+years only — no PII

    def test_schema_is_strict(self):
        schema = picks_schema()
        assert schema["additionalProperties"] is False
        assert schema["properties"]["picks"]["items"]["additionalProperties"] is False


class TestPromptTuning:
    """The tunable recipe: tone / guidance / custom template / shared framing, always safe."""

    def _system(self, cfg: PromptConfig | None) -> str:
        profile = make_profile()
        profile.prompt = cfg
        system, _user = build_prompts(profile, candidates(), k=3)
        return system

    def test_default_recipe_is_personal_and_carries_the_contract(self):
        system = self._system(None)
        assert "Because you watched X" in system
        assert "Use only tmdb_id values from the candidate list" in system

    def test_tone_preset_is_injected(self):
        system = self._system(PromptConfig(tone="cinephile"))
        assert TONE_PRESETS["cinephile"].strip() in system

    def test_unknown_tone_is_ignored_not_crashed(self):
        system = self._system(PromptConfig(tone="nonsense"))
        assert "Because you watched X" in system  # still a valid personal prompt

    def test_guidance_is_injected(self):
        system = self._system(PromptConfig(guidance="Prefer hidden gems."))
        assert "Prefer hidden gems." in system

    def test_custom_template_replaces_skeleton_but_keeps_the_contract(self):
        system = self._system(PromptConfig(template="Pick $k great films for $username."))
        assert "Pick 3 great films for sarah" in system
        assert "Use only tmdb_id values from the candidate list" in system

    def test_unknown_template_variable_left_intact(self):
        # string.Template.safe_substitute leaves unknown $vars as literal text — never raises.
        system = self._system(PromptConfig(template="Hi $bogus there $username."))
        assert "Hi $bogus there sarah." in system

    def test_template_cannot_crash_or_introspect(self):
        # The $ grammar has no attribute/subscript access, so these render harmlessly rather than
        # raising (the old str.format path would crash on {username.foo} / {k[0]}).
        for tpl in ("$username.__class__", "${weird", "{k[0]}", "$k[0]", "{username.foo}"):
            system = self._system(PromptConfig(template=tpl))
            assert "Use only tmdb_id values from the candidate list" in system  # contract intact
            assert "object" not in system.lower()  # no class-repr leaked from introspection

    def test_shared_scope_uses_aggregate_framing(self):
        system = self._system(PromptConfig(shared=True))
        assert "popular on this server" in system.lower()
        assert "phrased like 'Because you watched X'" not in system  # personal-only clause absent


def _fake_anthropic_module():
    mod = ModuleType("anthropic")

    class FakeError(Exception):
        def __init__(self, message="err", status_code=500):
            super().__init__(message)
            self.message = message
            self.status_code = status_code

    mod.APIError = type("APIError", (FakeError,), {})
    mod.RateLimitError = type("RateLimitError", (mod.APIError,), {})
    mod.APIStatusError = type("APIStatusError", (mod.APIError,), {})
    mod.APIConnectionError = type("APIConnectionError", (mod.APIError,), {})
    mod.Anthropic = MagicMock()
    return mod


class TestAnthropicCurator:
    def test_sends_structured_output_request_and_validates(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from shortlist.engine.curator.anthropic import AnthropicCurator

        client = MagicMock()
        mod.Anthropic.return_value = client
        client.messages.create.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "picks": [
                                {"tmdb_id": 2, "reason": "Because you watched Fargo"},
                                {"tmdb_id": 999, "reason": "hallucinated"},
                            ]
                        }
                    ),
                )
            ],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            stop_reason="end_turn",
        )
        curator = AnthropicCurator(api_key="k")
        picks = curator.curate(make_profile(history=[]), candidates(), k=2)

        assert [p.tmdb_id for p in picks] == [2]
        assert curator.last_tokens == 150
        call = client.messages.create.call_args
        assert call.kwargs["model"] == "claude-haiku-4-5-20251001"
        assert call.kwargs["output_config"]["format"]["type"] == "json_schema"
        assert call.kwargs["output_config"]["format"]["schema"] == picks_schema()
        assert "temperature" not in call.kwargs  # sampling params 400 on newer tiers

    def test_list_models_returns_the_accounts_model_ids(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from shortlist.engine.curator.anthropic import AnthropicCurator

        client = MagicMock()
        mod.Anthropic.return_value = client
        client.models.list.return_value = SimpleNamespace(
            data=[SimpleNamespace(id="claude-opus-4-8"), SimpleNamespace(id="claude-haiku-4-5-20251001")]
        )
        curator = AnthropicCurator(api_key="k")

        assert curator.list_models() == ["claude-opus-4-8", "claude-haiku-4-5-20251001"]
        assert client.models.list.call_args.kwargs["limit"] == 100

    def test_api_error_becomes_curator_error(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from shortlist.engine.curator.anthropic import AnthropicCurator
        from shortlist.engine.curator.base import CuratorError

        client = MagicMock()
        mod.Anthropic.return_value = client
        client.messages.create.side_effect = mod.APIStatusError("boom", status_code=529)
        with pytest.raises(CuratorError, match="529"):
            AnthropicCurator(api_key="k").curate(make_profile(history=[]), candidates(), k=2)

    def test_recommend_web_sends_the_web_search_tool_and_parses_titles(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from shortlist.engine.curator.anthropic import AnthropicCurator

        client = MagicMock()
        mod.Anthropic.return_value = client
        # A web-search server-tool block precedes the model's final text (as real responses do).
        client.messages.create.return_value = SimpleNamespace(
            content=[
                SimpleNamespace(type="server_tool_use", text=None),
                SimpleNamespace(type="text", text='[{"title": "Dune", "year": 2021, "media": "movie"}]'),
            ],
            usage=SimpleNamespace(input_tokens=200, output_tokens=40),
        )
        seeds = [Seed(tmdb_id=1, title="Arrival", media_type=MediaType.MOVIE, weight=1.0)]
        out = AnthropicCurator(api_key="k").recommend_web(make_profile(history=[]), seeds, k=5)

        assert out == [{"title": "Dune", "year": 2021, "media": "movie"}]
        call = client.messages.create.call_args
        assert call.kwargs["tools"][0]["type"] == "web_search_20250305"  # the SUT-controlled contract

    def test_recommend_web_returns_empty_on_api_error(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from shortlist.engine.curator.anthropic import AnthropicCurator

        client = MagicMock()
        mod.Anthropic.return_value = client
        client.messages.create.side_effect = mod.APIStatusError("down", status_code=500)
        seeds = [Seed(tmdb_id=1, title="Arrival", media_type=MediaType.MOVIE, weight=1.0)]
        assert AnthropicCurator(api_key="k").recommend_web(make_profile(history=[]), seeds, k=5) == []

    def test_complete_sends_no_tools_and_returns_text(self, monkeypatch):
        # The external-search (Exa) path: a plain completion, NO web_search tool — the app already searched.
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from shortlist.engine.curator.anthropic import AnthropicCurator

        client = MagicMock()
        mod.Anthropic.return_value = client
        client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='[{"title":"Dune"}]')],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        out = AnthropicCurator(api_key="k").complete("sys", "user")
        assert out == '[{"title":"Dune"}]'
        assert "tools" not in client.messages.create.call_args.kwargs  # no web-search tool on the RAG path

    def test_complete_returns_empty_string_on_api_error(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from shortlist.engine.curator.anthropic import AnthropicCurator

        client = MagicMock()
        mod.Anthropic.return_value = client
        client.messages.create.side_effect = mod.APIStatusError("down", status_code=500)
        assert AnthropicCurator(api_key="k").complete("sys", "user") == ""


class TestOpenAICurator:
    def test_sends_json_schema_response_format(self, monkeypatch):
        mod = ModuleType("openai")
        mod.OpenAIError = type("OpenAIError", (Exception,), {})
        mod.OpenAI = MagicMock()
        monkeypatch.setitem(sys.modules, "openai", mod)
        from shortlist.engine.curator.openai import OpenAICurator

        client = MagicMock()
        mod.OpenAI.return_value = client
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps({"picks": [{"tmdb_id": 1, "reason": "r"}]})))
            ],
            usage=SimpleNamespace(total_tokens=42),
        )
        picks = OpenAICurator(api_key="k").curate(make_profile(history=[]), candidates(), k=1)
        assert [p.tmdb_id for p in picks] == [1]
        call = client.chat.completions.create.call_args
        assert call.kwargs["response_format"]["json_schema"]["strict"] is True

    def test_list_models_keeps_chat_families_sorted(self, monkeypatch):
        curator, client, _mod = self._client(monkeypatch)
        client.models.list.return_value = SimpleNamespace(
            data=[
                SimpleNamespace(id="gpt-5-mini"),
                SimpleNamespace(id="text-embedding-3-large"),
                SimpleNamespace(id="o3"),
            ]
        )
        assert curator.list_models() == ["gpt-5-mini", "o3"]  # embeddings dropped, chat families sorted

    def test_list_models_falls_back_to_all_when_nothing_looks_like_chat(self, monkeypatch):
        curator, client, _mod = self._client(monkeypatch)
        client.models.list.return_value = SimpleNamespace(
            data=[SimpleNamespace(id="whisper-1"), SimpleNamespace(id="dall-e-3")]
        )
        assert curator.list_models() == ["dall-e-3", "whisper-1"]  # no chat family matched -> the full sorted list

    def _client(self, monkeypatch):
        mod = ModuleType("openai")
        mod.OpenAIError = type("OpenAIError", (Exception,), {})
        mod.OpenAI = MagicMock()
        monkeypatch.setitem(sys.modules, "openai", mod)
        from shortlist.engine.curator.openai import OpenAICurator

        client = MagicMock()
        mod.OpenAI.return_value = client
        return OpenAICurator(api_key="k"), client, mod

    def test_provider_error_becomes_curator_error(self, monkeypatch):
        curator, client, mod = self._client(monkeypatch)
        client.chat.completions.create.side_effect = mod.OpenAIError("upstream 500")
        with pytest.raises(CuratorError, match="OpenAI"):
            curator.curate(make_profile(history=[]), candidates(), k=1)

    def test_unparseable_json_becomes_curator_error(self, monkeypatch):
        curator, client, _mod = self._client(monkeypatch)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not json{"))],
            usage=SimpleNamespace(total_tokens=1),
        )
        with pytest.raises(CuratorError, match="unparseable"):
            curator.curate(make_profile(history=[]), candidates(), k=1)

    def test_recommend_web_uses_the_responses_web_search_tool(self, monkeypatch):
        curator, client, _mod = self._client(monkeypatch)
        client.responses.create.return_value = SimpleNamespace(
            output_text='[{"title": "Sicario", "year": 2015, "media": "movie"}]',
            usage=SimpleNamespace(total_tokens=77),
        )
        seeds = [Seed(tmdb_id=1, title="Arrival", media_type=MediaType.MOVIE, weight=1.0)]
        out = curator.recommend_web(make_profile(history=[]), seeds, k=5)

        assert out == [{"title": "Sicario", "year": 2015, "media": "movie"}]
        call = client.responses.create.call_args
        assert call.kwargs["tools"][0]["type"] == "web_search"  # the SUT-controlled contract

    def test_recommend_web_returns_empty_on_provider_error(self, monkeypatch):
        curator, client, mod = self._client(monkeypatch)
        client.responses.create.side_effect = mod.OpenAIError("upstream 500")
        seeds = [Seed(tmdb_id=1, title="Arrival", media_type=MediaType.MOVIE, weight=1.0)]
        assert curator.recommend_web(make_profile(history=[]), seeds, k=5) == []

    def test_complete_returns_text_and_empty_on_error(self, monkeypatch):
        # The Exa path uses a plain chat.completions call (no web_search tool) and returns its content.
        curator, client, mod = self._client(monkeypatch)
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='[{"title":"Sicario"}]'))],
            usage=SimpleNamespace(total_tokens=12),
        )
        assert curator.complete("sys", "user") == '[{"title":"Sicario"}]'

        client.chat.completions.create.side_effect = mod.OpenAIError("down")
        assert curator.complete("sys", "user") == ""


class TestGoogleCurator:
    def test_sends_response_schema(self, monkeypatch):
        google_pkg = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.Client = MagicMock()
        google_pkg.genai = genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", genai)
        from shortlist.engine.curator.google import GoogleCurator

        client = MagicMock()
        genai.Client.return_value = client
        client.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps({"picks": [{"tmdb_id": 1, "reason": "r"}]}),
            usage_metadata=SimpleNamespace(total_token_count=10),
        )
        picks = GoogleCurator(api_key="k").curate(make_profile(history=[]), candidates(), k=1)
        assert [p.tmdb_id for p in picks] == [1]
        call = client.models.generate_content.call_args
        assert call.kwargs["config"]["response_json_schema"] == picks_schema()

    def test_curate_error_never_leaks_the_api_key(self, monkeypatch):
        # google-genai embeds the key in error text as `?key=AIza…`; redact() doesn't cover that shape,
        # so the CuratorError (which reaches the run report + events row + UI) must be type-only.
        google_pkg = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.Client = MagicMock()
        google_pkg.genai = genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", genai)
        from shortlist.engine.curator.google import GoogleCurator

        client = MagicMock()
        genai.Client.return_value = client
        secret = "AIzaSyLEAKED_secret_key_1234567890"
        client.models.generate_content.side_effect = RuntimeError(
            f"400 error from https://generativelanguage.googleapis.com/v1/models?key={secret}"
        )
        with pytest.raises(CuratorError) as exc:
            GoogleCurator(api_key="k").curate(make_profile(history=[]), candidates(), k=1)
        assert secret not in str(exc.value)
        assert "RuntimeError" in str(exc.value)  # type is still reported for debugging

    def test_web_and_complete_log_paths_never_leak_the_api_key(self, monkeypatch):
        # The llm_web + complete paths swallow errors and log a warning — same `?key=AIza…` leak class
        # as curate, but log-only. Capture the log and assert the secret never reaches it.
        from loguru import logger

        google_pkg = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.Client = MagicMock()
        types_mod = ModuleType("google.genai.types")  # recommend_web does `from google.genai import types`
        types_mod.Tool = MagicMock()
        types_mod.GoogleSearch = MagicMock()
        types_mod.GenerateContentConfig = MagicMock()
        genai.types = types_mod
        google_pkg.genai = genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
        from shortlist.engine.curator.google import GoogleCurator

        client = MagicMock()
        genai.Client.return_value = client
        secret = "AIzaSyLEAKED_secret_key_1234567890"
        client.models.generate_content.side_effect = RuntimeError(
            f"400 from https://generativelanguage.googleapis.com/v1/models?key={secret}"
        )

        captured: list[str] = []
        sink_id = logger.add(captured.append, level="WARNING")
        try:
            cur = GoogleCurator(api_key="k")
            assert cur.recommend_web(make_profile(history=[]), [], k=3) == []
            assert cur.complete("sys", "user") == ""
        finally:
            logger.remove(sink_id)
        assert secret not in "".join(captured)

    def test_list_models_strips_prefix_and_keeps_content_generators(self, monkeypatch):
        google_pkg = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.Client = MagicMock()
        google_pkg.genai = genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", genai)
        from shortlist.engine.curator.google import GoogleCurator

        client = MagicMock()
        genai.Client.return_value = client
        client.models.list.return_value = [
            SimpleNamespace(name="models/gemini-2.5-pro", supported_actions=["generateContent"]),
            SimpleNamespace(name="models/embedding-001", supported_actions=["embedContent"]),  # dropped
            SimpleNamespace(name="models/gemini-2.5-flash", supported_actions=["generateContent"]),
        ]
        # 'models/' prefix stripped so the id matches what the SDK is called with; sorted; embed-only gone.
        assert GoogleCurator(api_key="k").list_models() == ["gemini-2.5-flash", "gemini-2.5-pro"]

    def test_applies_the_timeout_to_the_client_in_milliseconds(self, monkeypatch):
        # Regression: the ctor accepted `timeout` but never passed it, so a stalled Gemini call was
        # unbounded. google-genai's HttpOptions.timeout is in milliseconds.
        google_pkg = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.Client = MagicMock()
        google_pkg.genai = genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", genai)
        from shortlist.engine.curator.google import GoogleCurator

        GoogleCurator(api_key="k", timeout=45.0)
        assert genai.Client.call_args.kwargs["http_options"] == {"timeout": 45000}

    def _client(self, monkeypatch):
        google_pkg = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.Client = MagicMock()
        # `from google.genai import types` (used by recommend_web for the grounding tool) resolves to
        # this fake submodule; its factories just capture kwargs so a test can inspect the tool sent.
        types = ModuleType("google.genai.types")
        types.GenerateContentConfig = lambda **kw: SimpleNamespace(**kw)
        types.Tool = lambda **kw: SimpleNamespace(**kw)
        types.GoogleSearch = lambda **kw: SimpleNamespace(kind="google_search", **kw)
        genai.types = types
        google_pkg.genai = genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", types)
        from shortlist.engine.curator.google import GoogleCurator

        client = MagicMock()
        genai.Client.return_value = client
        return GoogleCurator(api_key="k"), client

    def test_provider_error_becomes_curator_error(self, monkeypatch):
        curator, client = self._client(monkeypatch)
        client.models.generate_content.side_effect = RuntimeError("gemini exploded")
        with pytest.raises(CuratorError, match="Google"):
            curator.curate(make_profile(history=[]), candidates(), k=1)

    def test_unparseable_json_becomes_curator_error(self, monkeypatch):
        curator, client = self._client(monkeypatch)
        client.models.generate_content.return_value = SimpleNamespace(
            text="not json{", usage_metadata=SimpleNamespace(total_token_count=1)
        )
        with pytest.raises(CuratorError, match="unparseable"):
            curator.curate(make_profile(history=[]), candidates(), k=1)

    def test_recommend_web_sends_the_google_search_grounding_tool_and_parses_titles(self, monkeypatch):
        curator, client = self._client(monkeypatch)
        client.models.generate_content.return_value = SimpleNamespace(
            text='[{"title": "Shogun", "year": 2024, "media": "show"}]',
            usage_metadata=SimpleNamespace(total_token_count=30),
        )
        seeds = [Seed(tmdb_id=1, title="Arrival", media_type=MediaType.MOVIE, weight=1.0)]
        out = curator.recommend_web(make_profile(history=[]), seeds, k=5)

        assert out == [{"title": "Shogun", "year": 2024, "media": "show"}]
        # SUT-controlled contract: the Google Search grounding tool is on the request (that IS the
        # native web search — without it Gemini can't search, and the source silently returns nothing).
        config = client.models.generate_content.call_args.kwargs["config"]
        assert config.tools[0].google_search.kind == "google_search"

    def test_recommend_web_returns_empty_on_provider_error(self, monkeypatch):
        curator, client = self._client(monkeypatch)
        client.models.generate_content.side_effect = RuntimeError("gemini grounding down")
        seeds = [Seed(tmdb_id=1, title="Arrival", media_type=MediaType.MOVIE, weight=1.0)]
        assert curator.recommend_web(make_profile(history=[]), seeds, k=5) == []

    def test_complete_sends_no_grounding_tool_and_returns_text(self, monkeypatch):
        # The Exa path: a plain generate_content (no google_search tool, no schema) — the app searched.
        curator, client = self._client(monkeypatch)
        client.models.generate_content.return_value = SimpleNamespace(
            text='[{"title":"Shogun"}]', usage_metadata=SimpleNamespace(total_token_count=9)
        )
        assert curator.complete("sys", "user") == '[{"title":"Shogun"}]'
        config = client.models.generate_content.call_args.kwargs["config"]
        assert "tools" not in config and "response_json_schema" not in config

        client.models.generate_content.side_effect = RuntimeError("down")
        assert curator.complete("sys", "user") == ""


class TestThreadLocalTokens:
    def test_token_counts_do_not_race_across_threads(self):
        # The whole point of the descriptor: when users are curated on parallel threads, each
        # thread's token write must be visible only to itself — a plain attribute would let the
        # last writer clobber the value every other thread then reads.
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from shortlist.engine.curator.base import ThreadLocalTokens

        class Holder:
            last_tokens = ThreadLocalTokens()

        holder = Holder()
        barrier = threading.Barrier(4)

        def work(n: int) -> int:
            holder.last_tokens = n
            barrier.wait()  # every thread has written before any thread reads
            return holder.last_tokens

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = sorted(pool.map(work, [10, 20, 30, 40]))
        assert results == [10, 20, 30, 40]  # each thread read back its own write

    def test_defaults_to_zero_before_any_write(self):
        from shortlist.engine.curator.base import ThreadLocalTokens

        class Holder:
            last_tokens = ThreadLocalTokens()

        assert Holder().last_tokens == 0


class TestOpenAICompatibleCurator:
    """Issue #7: llama.cpp, LM Studio, vLLM, LocalAI, OpenRouter — anything speaking the OpenAI API."""

    def _curator(self, monkeypatch, models=("llama-3.3-70b",)):
        from shortlist.engine.curator.openai_compatible import OpenAICompatibleCurator

        curator = OpenAICompatibleCurator.__new__(OpenAICompatibleCurator)
        curator._model = "llama-3.3-70b"
        monkeypatch.setattr(type(curator), "list_models", lambda self: list(models))
        return curator

    def test_ping_asks_what_is_loaded_instead_of_generating(self, monkeypatch):
        """The inherited ping sends a real chat completion — 30+ seconds on a CPU-bound local model,
        and a failure when the server is up but has nothing loaded, which is the state Test exists
        to diagnose."""
        assert "1 model(s) available" in self._curator(monkeypatch).ping()

    def test_ping_says_so_when_the_server_has_no_model_loaded(self, monkeypatch):
        assert "no models loaded" in self._curator(monkeypatch, models=()).ping()

    def test_a_base_url_is_required(self):
        from shortlist.engine.curator.openai_compatible import OpenAICompatibleCurator

        with pytest.raises(ValueError, match="base URL"):
            OpenAICompatibleCurator(base_url="")

    def test_it_does_not_claim_native_web_search(self):
        """Web search is an OpenAI-hosted tool, not part of the API these servers implement — they
        reach the llm_web source through an external provider (Exa), like Ollama."""
        from shortlist.engine.curator.openai_compatible import OpenAICompatibleCurator

        assert OpenAICompatibleCurator.supports_native_web_search is False


class TestLocalServerUrl:
    """`normalize_base_url` — the single most likely reason "it can't reach my server"."""

    def test_a_bare_host_gains_the_openai_api_path(self):
        """People paste the address they know their server by; the Ollama docs' one has no path at
        all, and every runtime we target serves the API under /v1."""
        from shortlist.engine.curator.openai_compatible import normalize_base_url

        assert normalize_base_url("http://localhost:11434") == "http://localhost:11434/v1"
        assert normalize_base_url("http://localhost:11434/") == "http://localhost:11434/v1"

    def test_a_url_that_already_has_a_path_is_left_alone(self):
        from shortlist.engine.curator.openai_compatible import normalize_base_url

        assert normalize_base_url("http://llama:8080/v1") == "http://llama:8080/v1"
        assert normalize_base_url("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"

    def test_nonsense_is_returned_untouched_rather_than_mangled(self):
        """A malformed URL should fail at the request with the owner's own text in the error, not be
        silently rewritten into something they never typed."""
        from shortlist.engine.curator.openai_compatible import normalize_base_url

        assert normalize_base_url("not a url") == "not a url"


class TestLocalServerCompatibility:
    """The two things that would make a real llama.cpp / LM Studio / vLLM fail on first contact.

    Both are OpenAI extensions inherited from the parent class, and both are silently wrong against
    the servers this provider exists to support (#7).
    """

    def _curator(self, monkeypatch, *, model="", available=("local-llama",)):
        from shortlist.engine.curator.openai_compatible import OpenAICompatibleCurator

        curator = OpenAICompatibleCurator.__new__(OpenAICompatibleCurator)
        curator._model = model or "gpt-4o-mini"  # what the parent's default leaves us with
        curator._format_from = 0
        monkeypatch.setattr(type(curator), "list_models", lambda self: list(available))
        return curator

    def test_a_blank_model_becomes_whatever_the_server_actually_serves(self, monkeypatch):
        """llama.cpp ignores the model field, but vLLM and LM Studio validate it — sending OpenAI's
        `gpt-4o-mini` default to them answers "model not found"."""
        curator = self._curator(monkeypatch)

        assert curator._resolve_model() == "local-llama"

    def test_an_explicit_model_is_never_second_guessed(self, monkeypatch):
        curator = self._curator(monkeypatch, model="qwen2.5:14b")

        assert curator._resolve_model() == "qwen2.5:14b"

    def test_an_unreachable_model_list_does_not_block_the_call(self, monkeypatch):
        from shortlist.engine.curator.openai_compatible import OpenAICompatibleCurator

        curator = OpenAICompatibleCurator.__new__(OpenAICompatibleCurator)
        curator._model, curator._format_from = "gpt-4o-mini", 0
        monkeypatch.setattr(type(curator), "list_models", lambda self: (_ for _ in ()).throw(RuntimeError("offline")))

        assert curator._resolve_model() == "gpt-4o-mini"  # send what we have rather than fail here

    @staticmethod
    def _stub_openai(monkeypatch):
        """The SDK is an optional extra — present in the shipped image, absent from a dev/CI install
        — so it is stubbed here exactly as the OpenAI tests above do."""
        mod = ModuleType("openai")
        mod.OpenAIError = type("OpenAIError", (Exception,), {})
        # The real SDK hangs every one of these off OpenAIError, which is why catching the base
        # class here would have swallowed timeouts and 500s as "shape unsupported".
        for name in ("BadRequestError", "UnprocessableEntityError", "APITimeoutError", "RateLimitError"):
            setattr(mod, name, type(name, (mod.OpenAIError,), {}))
        mod.OpenAI = MagicMock()
        monkeypatch.setitem(sys.modules, "openai", mod)
        return mod

    def test_it_falls_back_when_the_server_rejects_openais_json_schema(self, monkeypatch):
        """`json_schema` + `strict` is an OpenAI extension. Older llama.cpp / LM Studio / vLLM reject
        it outright, so insisting on it would fail on the very servers this provider is for."""
        openai = self._stub_openai(monkeypatch)
        curator = self._curator(monkeypatch, model="local-llama")
        tried: list = []

        def create(**kwargs):
            tried.append((kwargs.get("response_format") or {}).get("type", "none"))
            if len(tried) < 3:  # reject both structured forms, accept the bare request
                raise openai.BadRequestError("unsupported")
            return "reply"

        curator._client = MagicMock(chat=MagicMock(completions=MagicMock(create=create)))

        assert curator._chat("sys", "usr") == "reply"
        assert tried == ["json_schema", "json_object", "none"], "it must degrade, not give up"

    def test_the_shape_that_worked_is_remembered(self, monkeypatch):
        """Otherwise every user in a run re-pays the rejected attempts."""
        openai = self._stub_openai(monkeypatch)
        curator = self._curator(monkeypatch, model="local-llama")
        tried: list = []

        def create(**kwargs):
            kind = (kwargs.get("response_format") or {}).get("type", "none")
            tried.append(kind)
            if kind == "json_schema":
                raise openai.BadRequestError("unsupported")
            return "reply"

        curator._client = MagicMock(chat=MagicMock(completions=MagicMock(create=create)))

        curator._chat("sys", "usr")
        curator._chat("sys", "usr")

        assert tried == ["json_schema", "json_object", "json_object"], "the first rung is tried once"

    def test_the_sdk_really_exports_the_names_the_ladder_narrows_to(self):
        """Everything else here runs against a hand-built stub, which can never disprove a typo.
        A misspelled name would raise AttributeError from `_chat` — and `curate()` catches only
        OpenAIError, so it would escape the heuristic-degradation path entirely instead of degrading.
        Skipped where the optional SDK isn't installed (it is, in the shipped image)."""
        openai = pytest.importorskip("openai")

        assert issubclass(openai.BadRequestError, openai.OpenAIError)
        assert issubclass(openai.UnprocessableEntityError, openai.OpenAIError)

    @pytest.mark.parametrize("failure", ["APITimeoutError", "RateLimitError"])
    def test_a_transient_failure_does_not_downgrade_the_rest_of_the_run(self, monkeypatch, failure):
        """A blip says nothing about what the server supports.

        The curator instance lives for the whole run, and `_format_from` only moves forward — so
        treating one timeout on user #1 as "json_schema unsupported" would silently drop users
        #2..#48 to a weaker structured-output guarantee for the rest of the night.
        """
        openai = self._stub_openai(monkeypatch)
        curator = self._curator(monkeypatch, model="local-llama")
        curator._client = MagicMock(
            chat=MagicMock(completions=MagicMock(create=MagicMock(side_effect=getattr(openai, failure)("blip"))))
        )

        with pytest.raises(openai.OpenAIError):
            curator._chat("sys", "usr")
        assert curator._format_from == 0, "a transient error must not consume a rung"

    def test_an_embedding_model_is_never_auto_selected(self, monkeypatch):
        """A stock Ollama box has several, and they sort ahead of every chat model people run —
        so "first model the server lists" reliably picks one that cannot serve /chat/completions,
        after the settings Test button has already passed."""
        curator = self._curator(
            monkeypatch, available=["all-minilm", "bge-m3", "llama3.3:70b", "nomic-embed-text", "qwen2.5:14b"]
        )

        assert curator._resolve_model() == "llama3.3:70b"

    def test_an_all_embedding_server_still_sends_something(self, monkeypatch):
        """Better a model error naming what we sent than a silent empty model field."""
        curator = self._curator(monkeypatch, available=["all-minilm", "bge-m3"])

        assert curator._resolve_model() == "all-minilm"

    def test_a_server_that_refuses_everything_still_raises(self, monkeypatch):
        openai = self._stub_openai(monkeypatch)
        curator = self._curator(monkeypatch, model="local-llama")
        curator._client = MagicMock(
            chat=MagicMock(completions=MagicMock(create=MagicMock(side_effect=openai.BadRequestError("no"))))
        )

        with pytest.raises(openai.OpenAIError):
            curator._chat("sys", "usr")
