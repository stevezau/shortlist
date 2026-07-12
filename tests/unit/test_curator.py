"""Curator matrix: null / anthropic / openai / google / ollama, plus the hallucination validator."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from rowarr.engine.curator import make_curator
from rowarr.engine.curator.base import MAX_REASON_LEN, build_prompts, picks_schema, validate_picks
from rowarr.engine.curator.null import NullCurator
from rowarr.engine.curator.ollama import OllamaCurator
from rowarr.engine.models import MediaType, Seed
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


def _fake_anthropic_module():
    mod = ModuleType("anthropic")

    class FakeError(Exception):
        def __init__(self, message="err", status_code=500):
            super().__init__(message)
            self.message = message
            self.status_code = status_code

    mod.RateLimitError = type("RateLimitError", (FakeError,), {})
    mod.APIStatusError = type("APIStatusError", (FakeError,), {})
    mod.APIConnectionError = type("APIConnectionError", (FakeError,), {})
    mod.Anthropic = MagicMock()
    return mod


class TestAnthropicCurator:
    def test_sends_structured_output_request_and_validates(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from rowarr.engine.curator.anthropic import AnthropicCurator

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

    def test_api_error_becomes_curator_error(self, monkeypatch):
        mod = _fake_anthropic_module()
        monkeypatch.setitem(sys.modules, "anthropic", mod)
        from rowarr.engine.curator.anthropic import AnthropicCurator
        from rowarr.engine.curator.base import CuratorError

        client = MagicMock()
        mod.Anthropic.return_value = client
        client.messages.create.side_effect = mod.APIStatusError("boom", status_code=529)
        with pytest.raises(CuratorError, match="529"):
            AnthropicCurator(api_key="k").curate(make_profile(history=[]), candidates(), k=2)


class TestOpenAICurator:
    def test_sends_json_schema_response_format(self, monkeypatch):
        mod = ModuleType("openai")
        mod.OpenAIError = type("OpenAIError", (Exception,), {})
        mod.OpenAI = MagicMock()
        monkeypatch.setitem(sys.modules, "openai", mod)
        from rowarr.engine.curator.openai import OpenAICurator

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


class TestGoogleCurator:
    def test_sends_response_schema(self, monkeypatch):
        google_pkg = ModuleType("google")
        genai = ModuleType("google.genai")
        genai.Client = MagicMock()
        google_pkg.genai = genai
        monkeypatch.setitem(sys.modules, "google", google_pkg)
        monkeypatch.setitem(sys.modules, "google.genai", genai)
        from rowarr.engine.curator.google import GoogleCurator

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


class TestOllamaCurator:
    @respx.mock
    def test_posts_schema_as_format_field(self):
        route = respx.post("http://ollama.test/api/chat").mock(
            return_value=httpx.Response(
                200,
                json={
                    "message": {"content": json.dumps({"picks": [{"tmdb_id": 1, "reason": "r"}]})},
                    "prompt_eval_count": 10,
                    "eval_count": 5,
                },
            )
        )
        curator = OllamaCurator(base_url="http://ollama.test")
        picks = curator.curate(make_profile(history=[]), candidates(), k=1)
        assert [p.tmdb_id for p in picks] == [1]
        assert curator.last_tokens == 15
        body = json.loads(route.calls.last.request.content)
        assert body["format"] == picks_schema()
        assert body["stream"] is False
