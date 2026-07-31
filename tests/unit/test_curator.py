"""Tests for shortlist.engine.curator — the provider dispatch table and each adapter's parsing.

Backlog 6.1: no test ever constructed a provider curator (`AnthropicCurator`, `OpenAICurator`,
`GoogleCurator`, `OpenAICompatibleCurator`) and no test called `make_curator` — every call site
elsewhere in the suite mocks it instead, leaving ~400 lines of adapter code unexecuted, including the
`openai_compatible` / `openai-compatible` / `local` / `ollama` back-compat alias that migration
0034 and `api/settings.py` both depend on. `make_curator` is never mocked below: the dispatch-table
tests call the real function, and the per-adapter tests build curators through it too.

Fixture note (testing.md rule 11): the provider SDKs are metered, keyed services with no test
credentials, so these fixtures can't be a literal network capture the way the PMS/plex.tv ones are.
Each one is instead validated through the SDK's OWN pydantic response model
(``anthropic.types.Message``, ``openai.types.chat.ChatCompletion``, ``openai.types.responses.Response``,
``google.genai.types.GenerateContentResponse``) before being handed to the curator, so a fixture that
doesn't match the real API's schema fails at load time rather than silently passing a hand-shaped mock.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
import openai
import pytest
from google.genai import types as gtypes
from loguru import logger
from openai.types.responses import Response as OpenAIResponse

from shortlist.engine.curator import NullCurator, make_curator
from shortlist.engine.curator.anthropic import AnthropicCurator
from shortlist.engine.curator.google import GoogleCurator
from shortlist.engine.curator.openai import OpenAICurator
from shortlist.engine.curator.openai_compatible import OpenAICompatibleCurator, normalize_base_url
from shortlist.engine.models import MediaType, Seed
from tests.conftest import make_profile

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _load(name: str) -> dict | list:
    return json.loads((FIXTURES / name).read_text())


def _captured(fn, level: str = "DEBUG") -> str:
    """loguru does not route through stdlib logging, so `caplog` sees nothing — capture with a real
    sink, same pattern as test_ranking.py's `_captured`."""
    lines: list[str] = []
    sink_id = logger.add(lines.append, level=level, format="{message}")
    try:
        fn()
    finally:
        logger.remove(sink_id)
    return "".join(lines)


class TestMakeCuratorDispatch:
    """`make_curator` is the whole subject here — table-tested across every accepted provider name."""

    @pytest.mark.parametrize(
        ("provider", "kwargs", "expected_type"),
        [
            ("anthropic", {"api_key": "sk-ant-test"}, AnthropicCurator),
            ("openai", {"api_key": "sk-test"}, OpenAICurator),
            ("openai_compatible", {"base_url": "http://localhost:11434/v1"}, OpenAICompatibleCurator),
            ("openai-compatible", {"base_url": "http://localhost:11434/v1"}, OpenAICompatibleCurator),
            ("local", {"base_url": "http://localhost:11434/v1"}, OpenAICompatibleCurator),
            # Pre-merge name for the same provider — 0034_merge_ollama_provider_for_real.py and
            # api/settings.py:143 both depend on this alias still resolving.
            ("ollama", {"base_url": "http://localhost:11434/v1"}, OpenAICompatibleCurator),
            ("google", {"api_key": "AIzaTest"}, GoogleCurator),
            ("none", {}, NullCurator),
            ("null", {}, NullCurator),
            ("", {}, NullCurator),
        ],
    )
    def test_resolves_every_accepted_provider_name(self, provider, kwargs, expected_type):
        curator = make_curator(provider, **kwargs)

        assert isinstance(curator, expected_type)

    def test_provider_name_is_case_insensitive(self):
        assert isinstance(make_curator("ANTHROPIC", api_key="sk-ant-test"), AnthropicCurator)
        assert isinstance(make_curator("Ollama", base_url="http://localhost:11434/v1"), OpenAICompatibleCurator)

    def test_unknown_provider_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown curator provider"):
            make_curator("bogus")


class TestNullCurator:
    def test_complete_returns_empty_string_with_no_llm_call(self):
        curator = make_curator("none")

        assert curator.complete("system", "user") == ""
        assert curator.last_tokens == 0
        assert curator.supports_native_web_search is False


class TestAnthropicCurator:
    def test_recommend_web_parses_titles_and_records_tokens(self):
        curator = make_curator("anthropic", api_key="sk-ant-test")
        curator._client = MagicMock()
        curator._client.messages.create.return_value = anthropic.types.Message.model_validate(
            _load("curator_anthropic_recommend_web.json")
        )
        seeds = [Seed(tmdb_id=1, title="Arrival", media_type=MediaType.MOVIE, weight=1.0)]

        titles = curator.recommend_web(make_profile(), seeds, k=5)

        assert titles == [
            {"title": "The Wild Robot", "year": 2024, "media": "movie"},
            {"title": "Severance", "year": 2022, "media": "show"},
        ]
        assert curator.last_tokens == 612 + 143

    def test_recommend_web_degrades_to_empty_list_on_api_error(self):
        curator = make_curator("anthropic", api_key="sk-ant-test")
        curator._client = MagicMock()
        curator.last_tokens = 4242  # sentinel: proves the error path never touches it
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        curator._client.messages.create.side_effect = anthropic.APIConnectionError(request=request)

        titles = curator.recommend_web(make_profile(), [], k=5)

        assert titles == []
        assert curator.last_tokens == 4242

    def test_complete_returns_joined_text_and_records_tokens(self):
        curator = make_curator("anthropic", api_key="sk-ant-test")
        curator._client = MagicMock()
        curator._client.messages.create.return_value = anthropic.types.Message.model_validate(
            _load("curator_anthropic_complete.json")
        )

        text = curator.complete("system", "user")

        assert text == "Based on the articles, I'd recommend The Wild Robot and Severance for their next watch."
        assert curator.last_tokens == 340 + 27

    def test_complete_degrades_to_empty_string_on_api_error(self):
        curator = make_curator("anthropic", api_key="sk-ant-test")
        curator._client = MagicMock()
        curator.last_tokens = 4242
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        curator._client.messages.create.side_effect = anthropic.APIConnectionError(request=request)

        assert curator.complete("system", "user") == ""
        assert curator.last_tokens == 4242

    def test_missing_extra_raises_import_error_naming_it(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("no module named anthropic")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", blocked)

        with pytest.raises(ImportError, match=r"shortlist\[anthropic\]"):
            AnthropicCurator(api_key="sk-ant-test")


class TestOpenAICurator:
    def test_recommend_web_parses_titles_and_records_tokens(self):
        curator = make_curator("openai", api_key="sk-test")
        curator._client = MagicMock()
        curator._client.responses.create.return_value = OpenAIResponse.model_validate(
            _load("curator_openai_responses_web_search.json")
        )

        titles = curator.recommend_web(make_profile(), [], k=5)

        assert titles == [
            {"title": "The Wild Robot", "year": 2024, "media": "movie"},
            {"title": "Severance", "year": 2022, "media": "show"},
        ]
        assert curator.last_tokens == 636

    def test_recommend_web_degrades_to_empty_list_on_api_error(self):
        curator = make_curator("openai", api_key="sk-test")
        curator._client = MagicMock()
        curator.last_tokens = 4242
        request = httpx.Request("POST", "https://api.openai.com/v1/responses")
        curator._client.responses.create.side_effect = openai.APIConnectionError(request=request)

        assert curator.recommend_web(make_profile(), [], k=5) == []
        assert curator.last_tokens == 4242

    def test_complete_returns_message_content_and_records_tokens(self):
        curator = make_curator("openai", api_key="sk-test")
        curator._client = MagicMock()
        curator._client.chat.completions.create.return_value = openai.types.chat.ChatCompletion.model_validate(
            _load("curator_openai_chat_completion.json")
        )

        text = curator.complete("system", "user")

        assert text == "Based on the articles, I'd recommend The Wild Robot and Severance."
        assert curator.last_tokens == 228
        call_kwargs = curator._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "gpt-4o-mini"

    def test_complete_degrades_to_empty_string_on_api_error(self):
        curator = make_curator("openai", api_key="sk-test")
        curator._client = MagicMock()
        curator.last_tokens = 4242
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        curator._client.chat.completions.create.side_effect = openai.APIConnectionError(request=request)

        assert curator.complete("system", "user") == ""
        assert curator.last_tokens == 4242


class TestOpenAICompatibleCurator:
    """The back-compat home for ollama/local/openai-compatible — see make_curator's alias comment."""

    def test_requires_base_url(self):
        with pytest.raises(ValueError, match="base URL"):
            OpenAICompatibleCurator(base_url="")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://localhost:11434", "http://localhost:11434/v1"),
            ("http://localhost:11434/", "http://localhost:11434/v1"),
            ("http://localhost:8080/v1", "http://localhost:8080/v1"),
            ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ],
    )
    def test_normalize_base_url(self, raw, expected):
        assert normalize_base_url(raw) == expected

    @staticmethod
    def _models_client(fixture: str = "curator_openai_compatible_models.json") -> MagicMock:
        client = MagicMock()
        client.models.list.return_value = SimpleNamespace(
            data=[openai.types.Model.model_validate(m) for m in _load(fixture)]
        )
        return client

    def test_send_model_skips_embedding_models_and_resolves_the_first_chat_model(self):
        curator = make_curator("ollama", base_url="http://localhost:11434/v1")
        curator._client = self._models_client()

        resolved = curator._send_model()

        assert resolved == "llama3.3"
        assert curator._model == "llama3.3"  # remembered — not re-resolved on the next call

    def test_list_models_is_unfiltered_unlike_the_openai_parent(self):
        # The inherited OpenAICurator.list_models() keeps only gpt-/o1-/o3-/o4-/chatgpt families,
        # which would hide every model on a local server. The compatible override must not filter.
        curator = make_curator("ollama", base_url="http://localhost:11434/v1")
        curator._client = self._models_client()

        assert curator.list_models() == ["bge-m3", "llama3.3", "nomic-embed-text"]

    def test_complete_sends_the_resolved_model_and_records_tokens(self):
        curator = make_curator("ollama", base_url="http://localhost:11434/v1")
        curator._client = self._models_client()
        curator._client.chat.completions.create.return_value = openai.types.chat.ChatCompletion.model_validate(
            _load("curator_openai_chat_completion.json")
        )

        text = curator.complete("system", "user")

        assert text == "Based on the articles, I'd recommend The Wild Robot and Severance."
        assert curator.last_tokens == 228
        call_kwargs = curator._client.chat.completions.create.call_args.kwargs
        assert call_kwargs["model"] == "llama3.3"  # resolved off the server's model list, not gpt-4o-mini

    def test_send_model_falls_back_to_the_only_model_when_nothing_chat_capable_is_available(self):
        curator = make_curator("ollama", base_url="http://localhost:11434/v1")
        curator._client = MagicMock()
        curator._client.models.list.return_value = SimpleNamespace(
            data=[
                openai.types.Model.model_validate(
                    {"id": "nomic-embed-text", "created": 1, "object": "model", "owned_by": "x"}
                )
            ]
        )

        resolved = curator._send_model()

        assert resolved == "nomic-embed-text"  # nothing chat-capable — sends the only model there is


class TestGoogleCurator:
    def test_recommend_web_parses_titles_and_records_tokens(self):
        curator = make_curator("google", api_key="AIzaTest")
        curator._client = MagicMock()
        curator._client.models.generate_content.return_value = gtypes.GenerateContentResponse.model_validate(
            _load("curator_google_generate_content_web.json")
        )

        titles = curator.recommend_web(make_profile(), [], k=5)

        assert titles == [
            {"title": "The Wild Robot", "year": 2024, "media": "movie"},
            {"title": "Severance", "year": 2022, "media": "show"},
        ]
        assert curator.last_tokens == 380

    def test_recommend_web_logs_only_the_exception_type_never_the_message(self):
        """google.py's comment: the google-genai error text can carry the API key (`?key=AIza...`),
        so only `type(e).__name__` may be logged — never `e` itself."""
        curator = make_curator("google", api_key="AIzaTest")
        curator._client = MagicMock()
        curator.last_tokens = 4242
        curator._client.models.generate_content.side_effect = RuntimeError("boom ?key=AIzaSyDSECRETVALUE")
        result = {}

        captured = _captured(
            lambda: result.__setitem__("titles", curator.recommend_web(make_profile(), [], k=5)),
            level="WARNING",
        )

        assert result["titles"] == []
        assert curator.last_tokens == 4242
        assert "AIzaSyDSECRETVALUE" not in captured
        assert "RuntimeError" in captured

    def test_complete_returns_text_and_records_tokens(self):
        curator = make_curator("google", api_key="AIzaTest")
        curator._client = MagicMock()
        curator._client.models.generate_content.return_value = gtypes.GenerateContentResponse.model_validate(
            _load("curator_google_generate_content_complete.json")
        )

        text = curator.complete("system", "user")

        assert text == "Based on the articles, I'd recommend The Wild Robot and Severance."
        assert curator.last_tokens == 164

    def test_complete_logs_only_the_exception_type_never_the_message(self):
        curator = make_curator("google", api_key="AIzaTest")
        curator._client = MagicMock()
        curator.last_tokens = 4242
        curator._client.models.generate_content.side_effect = RuntimeError("boom ?key=AIzaSyDSECRETVALUE")
        result = {}

        captured = _captured(
            lambda: result.__setitem__("text", curator.complete("system", "user")),
            level="WARNING",
        )

        assert result["text"] == ""
        assert curator.last_tokens == 4242
        assert "AIzaSyDSECRETVALUE" not in captured
        assert "RuntimeError" in captured
