"""Focused boundary tests for bounded provider calls.

These tests deliberately assert the request fields owned by Shortlist and that a denied call never
reaches an SDK or HTTP transport.  The provider-control implementation is supplied separately.
"""

from __future__ import annotations

import base64
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
import respx

from shortlist.engine.clients.search import EXA_SEARCH_URL, ExaClient, SearxngClient
from shortlist.engine.provider_calls import ProviderCall, ProviderCallControls
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.poster_service import PosterStudio, _GoogleArtist, _OpenAIArtist
from tests.conftest import make_profile
from tests.db_helpers import disposing_engine


class Denied(RuntimeError):
    """Raised by the test guard before a paid call is allowed to leave the process."""


def _controls(*, allow_provider_managed_search: bool = False, guard=None) -> ProviderCallControls:
    if guard is None:

        @contextmanager
        def guard(_call: ProviderCall):
            yield

    return ProviderCallControls(
        guard=guard,
        max_output_tokens=123,
        max_native_tool_uses=2,
        allow_provider_managed_search=allow_provider_managed_search,
    )


def _recording_guard(calls: list[ProviderCall]):
    @contextmanager
    def guard(call: ProviderCall) -> None:
        calls.append(call)
        yield

    return guard


@pytest.fixture
def sessions(tmp_path):
    run_migrations(tmp_path)
    with disposing_engine(make_engine(tmp_path)) as engine:
        factory = make_session_factory(engine)
        yield factory


def test_provider_call_controls_are_frozen_and_describe_the_paid_boundary():
    controls = _controls()

    with pytest.raises(AttributeError):
        controls.max_output_tokens = 999

    assert controls.max_native_tool_uses == 2
    assert controls.allow_provider_managed_search is False


def test_openai_native_call_is_capped_and_guarded():
    from shortlist.engine.curator.openai import OpenAICurator

    calls: list[ProviderCall] = []
    curator = OpenAICurator(api_key="sk-test", provider_controls=_controls(guard=_recording_guard(calls)))
    curator._client = MagicMock()
    curator._client.base_url = "https://api.openai.com/v1"
    curator._client.responses.create.return_value = SimpleNamespace(
        output_text='[{"title":"Silo","year":2023,"media":"show"}]',
        usage=SimpleNamespace(total_tokens=10, output_tokens=4),
    )

    assert curator.recommend_web(make_profile(), [], k=1)
    kwargs = curator._client.responses.create.call_args.kwargs
    assert kwargs["max_output_tokens"] == 123
    assert kwargs["max_tool_calls"] == 2
    assert calls[0].kind == "native_search"
    assert calls[0].provider == "openai"
    assert calls[0].model == curator._model
    assert calls[0].destination == "https://api.openai.com/v1"


def test_anthropic_native_uses_the_lower_of_provider_and_control_limits():
    from shortlist.engine.curator.anthropic import AnthropicCurator

    calls: list[ProviderCall] = []
    curator = AnthropicCurator(api_key="sk-ant-test", provider_controls=_controls(guard=_recording_guard(calls)))
    curator._client = MagicMock()
    curator._client.base_url = "https://api.anthropic.com"
    curator._client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='[{"title":"Silo","year":2023,"media":"show"}]')],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )

    assert curator.recommend_web(make_profile(), [], k=1)
    kwargs = curator._client.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == 123
    assert kwargs["tools"][0]["max_uses"] == 2
    assert calls[0].kind == "native_search"
    assert calls[0].destination == "https://api.anthropic.com"


def test_google_native_is_denied_before_network_when_provider_search_is_not_allowed():
    from shortlist.engine.curator.google import GoogleCurator

    calls: list[ProviderCall] = []
    curator = GoogleCurator(api_key="AIzaTest", provider_controls=_controls(guard=_recording_guard(calls)))
    curator._client = MagicMock()

    assert curator.recommend_web(make_profile(), [], k=1) == []
    curator._client.models.generate_content.assert_not_called()
    assert calls == []


def test_google_native_can_be_explicitly_allowed_and_output_is_capped():
    from google.genai import types

    from shortlist.engine.curator.google import GoogleCurator

    calls: list[ProviderCall] = []
    curator = GoogleCurator(
        api_key="AIzaTest",
        provider_controls=_controls(allow_provider_managed_search=True, guard=_recording_guard(calls)),
    )
    curator._client = MagicMock()
    curator._client.models.generate_content.return_value = MagicMock(text="[]", usage_metadata=None)

    curator.recommend_web(make_profile(), [], k=1)
    kwargs = curator._client.models.generate_content.call_args.kwargs
    config = kwargs["config"]
    assert isinstance(config, types.GenerateContentConfig)
    assert config.max_output_tokens == 123
    assert calls[0].kind == "native_search"
    assert calls[0].native_tool_uses is None


@respx.mock
def test_exa_control_disables_retry_redirects_and_describes_destination():
    calls: list[ProviderCall] = []
    route = respx.post(EXA_SEARCH_URL).mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.test/redirected"})
    )
    redirected = respx.post("https://example.test/redirected").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    with pytest.raises(httpx.HTTPStatusError):
        ExaClient("exa-key", provider_controls=_controls(guard=_recording_guard(calls))).search("q")

    assert len(route.calls) == 1
    assert len(redirected.calls) == 0
    assert calls[0].kind == "external_search"
    assert calls[0].provider == "exa"
    assert calls[0].destination == "https://api.exa.ai"


@respx.mock
def test_exa_control_disables_status_retries():
    calls: list[ProviderCall] = []
    route = respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        ExaClient("exa-key", provider_controls=_controls(guard=_recording_guard(calls))).search("q")
    assert len(route.calls) == 1


@respx.mock
def test_searxng_control_disables_retry_redirects():
    calls: list[ProviderCall] = []
    url = "http://searx.local:8080"
    route = respx.get(f"{url}/search").mock(return_value=httpx.Response(503))
    with pytest.raises(RuntimeError):
        SearxngClient(url, provider_controls=_controls(guard=_recording_guard(calls))).search("q")

    assert len(route.calls) == 1
    assert calls[0].kind == "external_search"
    assert calls[0].destination == url


def test_openai_artist_is_one_image_call_and_guarded(monkeypatch):
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value.images.generate.return_value = MagicMock(
        data=[MagicMock(b64_json=base64.b64encode(b"IMAGE").decode())]
    )
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    calls: list[ProviderCall] = []

    assert (
        _OpenAIArtist("sk-test", provider_controls=_controls(guard=_recording_guard(calls))).render(
            title="T", subtitle="", style="", engine="ai"
        )
        == b"IMAGE"
    )
    kwargs = fake_openai.OpenAI.return_value.images.generate.call_args.kwargs
    assert kwargs["n"] == 1
    assert calls[0].kind == "image"
    assert calls[0].destination == "https://api.openai.com/v1"


def test_google_artist_is_one_image_call_and_guarded(monkeypatch):
    fake_genai = MagicMock()
    image = MagicMock()
    image.image.image_bytes = b"IMAGE"
    fake_genai.Client.return_value.models.generate_images.return_value = MagicMock(generated_images=[image])
    google_pkg = MagicMock(genai=fake_genai)
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    calls: list[ProviderCall] = []

    assert (
        _GoogleArtist("g-key", provider_controls=_controls(guard=_recording_guard(calls))).render(
            title="T", subtitle="", style="", engine="ai"
        )
        == b"IMAGE"
    )
    config = fake_genai.types.GenerateImagesConfig.call_args.kwargs
    assert config["number_of_images"] == 1
    assert calls[0].kind == "image"
    assert calls[0].destination == "https://generativelanguage.googleapis.com"


def test_denied_guard_prevents_openai_sdk_call():
    from shortlist.engine.curator.openai import OpenAICurator

    @contextmanager
    def deny(_call: ProviderCall):
        raise Denied
        yield

    curator = OpenAICurator(api_key="sk-test", provider_controls=_controls(guard=deny))
    curator._client = MagicMock()

    with pytest.raises(Denied):
        curator.recommend_web(make_profile(), [], k=1)
    curator._client.responses.create.assert_not_called()


def test_openai_completion_uses_caller_cap_then_control_cap_then_control_default():
    from shortlist.engine.curator.openai import OpenAICurator

    curator = OpenAICurator(api_key="sk-test", provider_controls=_controls())
    curator._client = MagicMock()
    curator._client.base_url = "https://api.openai.com/v1"
    curator._client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))],
        usage=SimpleNamespace(total_tokens=1, completion_tokens=1),
    )

    curator.complete("system", "user", max_tokens=50)
    assert curator._client.chat.completions.create.call_args.kwargs["max_completion_tokens"] == 50
    curator.complete("system", "user", max_tokens=800)
    assert curator._client.chat.completions.create.call_args.kwargs["max_completion_tokens"] == 123
    curator.complete("system", "user")
    assert curator._client.chat.completions.create.call_args.kwargs["max_completion_tokens"] == 123


def test_anthropic_completion_uses_control_cap_when_caller_omits_or_exceeds_it():
    from shortlist.engine.curator.anthropic import AnthropicCurator

    curator = AnthropicCurator(api_key="sk-ant-test", provider_controls=_controls())
    curator._client = MagicMock()
    curator._client.base_url = "https://api.anthropic.com"
    curator._client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="[]")], usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )

    curator.complete("system", "user")
    assert curator._client.messages.create.call_args.kwargs["max_tokens"] == 123
    curator.complete("system", "user", max_tokens=800)
    assert curator._client.messages.create.call_args.kwargs["max_tokens"] == 123
    curator.complete("system", "user", max_tokens=50)
    assert curator._client.messages.create.call_args.kwargs["max_tokens"] == 50


def test_google_completion_always_has_control_output_cap():
    from shortlist.engine.curator.google import GoogleCurator

    curator = GoogleCurator(api_key="AIzaTest", provider_controls=_controls())
    curator._client = MagicMock()
    curator._client.models.generate_content.return_value = SimpleNamespace(text="", usage_metadata=None)

    curator.complete("system", "user")
    assert curator._client.models.generate_content.call_args.kwargs["config"]["max_output_tokens"] == 123
    curator.complete("system", "user", max_tokens=800)
    assert curator._client.models.generate_content.call_args.kwargs["config"]["max_output_tokens"] == 123
    curator.complete("system", "user", max_tokens=50)
    assert curator._client.models.generate_content.call_args.kwargs["config"]["max_output_tokens"] == 50


def test_controlled_openai_constructor_disables_retries_and_redirects(monkeypatch):
    import openai

    from shortlist.engine.curator.openai import OpenAICurator

    client = MagicMock()
    monkeypatch.setattr(openai, "OpenAI", MagicMock(return_value=client))
    http_client = MagicMock()
    monkeypatch.setattr(openai, "DefaultHttpxClient", MagicMock(return_value=http_client))
    OpenAICurator(api_key="sk-test", provider_controls=_controls())
    kwargs = openai.OpenAI.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["http_client"] is http_client
    assert openai.DefaultHttpxClient.call_args.kwargs["follow_redirects"] is False


def test_controlled_anthropic_constructor_disables_retries_and_redirects(monkeypatch):
    import anthropic

    from shortlist.engine.curator.anthropic import AnthropicCurator

    client = MagicMock()
    monkeypatch.setattr(anthropic, "Anthropic", MagicMock(return_value=client))
    http_client = MagicMock()
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", MagicMock(return_value=http_client))
    AnthropicCurator(api_key="sk-ant-test", provider_controls=_controls())
    kwargs = anthropic.Anthropic.call_args.kwargs
    assert kwargs["max_retries"] == 0
    assert kwargs["http_client"] is http_client
    assert anthropic.DefaultHttpxClient.call_args.kwargs["follow_redirects"] is False


def test_controlled_google_constructor_uses_one_attempt_and_no_redirects(monkeypatch):
    from google import genai

    from shortlist.engine.curator.google import GoogleCurator

    client = MagicMock()
    monkeypatch.setattr(genai, "Client", MagicMock(return_value=client))
    GoogleCurator(api_key="AIzaTest", provider_controls=_controls())
    http_options = genai.Client.call_args.kwargs["http_options"]
    assert http_options["retry_options"]["attempts"] == 1
    assert http_options["client_args"]["follow_redirects"] is False
    assert http_options["async_client_args"]["follow_redirects"] is False


def test_controlled_openai_native_schema_failure_does_not_make_a_second_paid_call():
    import openai

    from shortlist.engine.curator.openai import OpenAICurator

    calls: list[ProviderCall] = []
    curator = OpenAICurator(api_key="sk-test", provider_controls=_controls(guard=_recording_guard(calls)))
    curator._client = MagicMock()
    curator._client.base_url = "https://api.openai.com/v1"
    curator._client.responses.create.side_effect = openai.OpenAIError("schema rejected")

    assert curator.recommend_web(make_profile(), [], k=1) == []
    assert curator._client.responses.create.call_count == 1
    assert len(calls) == 1


def test_controlled_google_native_failure_does_not_make_a_second_paid_call():
    from shortlist.engine.curator.google import GoogleCurator

    calls: list[ProviderCall] = []
    curator = GoogleCurator(
        api_key="AIzaTest",
        provider_controls=_controls(allow_provider_managed_search=True, guard=_recording_guard(calls)),
    )
    curator._client = MagicMock()
    curator._client.models.generate_content.side_effect = RuntimeError("schema rejected")

    assert curator.recommend_web(make_profile(), [], k=1) == []
    assert curator._client.models.generate_content.call_count == 1
    assert len(calls) == 1


def test_denied_guard_prevents_anthropic_sdk_call():
    from shortlist.engine.curator.anthropic import AnthropicCurator

    @contextmanager
    def deny(_call: ProviderCall):
        raise Denied
        yield

    curator = AnthropicCurator(api_key="sk-ant-test", provider_controls=_controls(guard=deny))
    curator._client = MagicMock()
    with pytest.raises(Denied):
        curator.recommend_web(make_profile(), [], k=1)
    curator._client.messages.create.assert_not_called()


def test_denied_guard_prevents_allowed_google_sdk_call():
    from shortlist.engine.curator.google import GoogleCurator

    @contextmanager
    def deny(_call: ProviderCall):
        raise Denied
        yield

    curator = GoogleCurator(
        api_key="AIzaTest",
        provider_controls=_controls(allow_provider_managed_search=True, guard=deny),
    )
    curator._client = MagicMock()
    assert curator.recommend_web(make_profile(), [], k=1) == []
    curator._client.models.generate_content.assert_not_called()


@respx.mock
def test_denied_guard_prevents_external_search_http_call():
    @contextmanager
    def deny(_call: ProviderCall):
        raise Denied
        yield

    route = respx.post(EXA_SEARCH_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    with pytest.raises(Denied):
        ExaClient("exa-key", provider_controls=_controls(guard=deny)).search("q")
    assert route.calls == []


def test_denied_guard_prevents_openai_image_call(monkeypatch):
    fake_openai = MagicMock()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    @contextmanager
    def deny(_call: ProviderCall):
        raise Denied
        yield

    with pytest.raises(Denied):
        _OpenAIArtist("sk-test", provider_controls=_controls(guard=deny)).render(
            title="T", subtitle="", style="", engine="ai"
        )
    fake_openai.OpenAI.return_value.images.generate.assert_not_called()


def test_poster_cache_hit_does_not_invoke_the_guarded_artist_again(sessions):
    ai = MagicMock()
    ai.render.return_value = b"IMAGE"
    studio = PosterStudio(sessions, ai=ai)

    assert studio.render(title="T", subtitle="", style="", engine="ai") == b"IMAGE"
    assert studio.render(title="T", subtitle="", style="", engine="ai") == b"IMAGE"
    ai.render.assert_called_once_with(title="T", subtitle="", style="", engine="ai")


@respx.mock
def test_real_openai_sdk_controlled_completion_attempts_once_and_does_not_follow_redirects():
    from shortlist.engine.curator.openai import OpenAICurator

    base = "https://api.openai.test/v1"
    route = respx.post(f"{base}/chat/completions").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.test/openai-target"})
    )
    target = respx.post("https://example.test/openai-target").mock(return_value=httpx.Response(200, json={}))
    curator = OpenAICurator(api_key="sk-test", base_url=base, provider_controls=_controls())

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1
    assert target.calls == []


@respx.mock
def test_real_openai_sdk_controlled_completion_does_not_retry_500():
    from shortlist.engine.curator.openai import OpenAICurator

    base = "https://api.openai.test/v1"
    route = respx.post(f"{base}/chat/completions").mock(return_value=httpx.Response(500))
    curator = OpenAICurator(api_key="sk-test", base_url=base, provider_controls=_controls())

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1


@respx.mock
def test_real_anthropic_sdk_controlled_completion_attempts_once_and_does_not_follow_redirects():
    from shortlist.engine.curator.anthropic import AnthropicCurator

    base = "https://api.anthropic.test"
    route = respx.post(f"{base}/v1/messages").mock(
        return_value=httpx.Response(302, headers={"Location": "https://example.test/anthropic-target"})
    )
    target = respx.post("https://example.test/anthropic-target").mock(return_value=httpx.Response(200, json={}))
    curator = AnthropicCurator(api_key="sk-ant-test", base_url=base, provider_controls=_controls())

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1
    assert target.calls == []


@respx.mock
def test_real_anthropic_sdk_controlled_completion_does_not_retry_500():
    from shortlist.engine.curator.anthropic import AnthropicCurator

    base = "https://api.anthropic.test"
    route = respx.post(f"{base}/v1/messages").mock(return_value=httpx.Response(500))
    curator = AnthropicCurator(api_key="sk-ant-test", base_url=base, provider_controls=_controls())

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1


@respx.mock
def test_real_google_sdk_controlled_completion_does_not_retry_500():
    from shortlist.engine.curator.google import GoogleCurator

    base = "https://api.google.test"
    route = respx.post(url__regex=r"https://api\.google\.test/.+generateContent").mock(return_value=httpx.Response(500))
    curator = GoogleCurator(api_key="AIzaTest", base_url=base, provider_controls=_controls())

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1


@respx.mock
def test_controlled_openai_preserves_configured_destination_but_httpx_normalizes_request():
    from shortlist.engine.curator.openai import OpenAICurator

    calls: list[ProviderCall] = []
    configured = "HTTPS://API.OPENAI.TEST:443/v1/"
    route = respx.post("https://api.openai.test/v1/chat/completions").mock(return_value=httpx.Response(500))
    curator = OpenAICurator(
        api_key="sk-test", base_url=configured, provider_controls=_controls(guard=_recording_guard(calls))
    )

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1
    assert calls[0].destination == configured.rstrip("/")


@respx.mock
def test_controlled_anthropic_preserves_configured_destination_but_httpx_normalizes_request():
    from shortlist.engine.curator.anthropic import AnthropicCurator

    calls: list[ProviderCall] = []
    configured = "HTTPS://API.ANTHROPIC.TEST:443/"
    route = respx.post("https://api.anthropic.test/v1/messages").mock(return_value=httpx.Response(500))
    curator = AnthropicCurator(
        api_key="sk-ant-test", base_url=configured, provider_controls=_controls(guard=_recording_guard(calls))
    )

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1
    assert calls[0].destination == configured.rstrip("/")


@respx.mock
def test_controlled_google_preserves_configured_destination_but_httpx_normalizes_request():
    from shortlist.engine.curator.google import GoogleCurator

    calls: list[ProviderCall] = []
    configured = "HTTPS://API.GOOGLE.TEST:443/"
    route = respx.post(url__regex=r"https://api\.google\.test/.+generateContent").mock(return_value=httpx.Response(500))
    curator = GoogleCurator(
        api_key="AIzaTest", base_url=configured, provider_controls=_controls(guard=_recording_guard(calls))
    )

    assert curator.complete("system", "user") == ""
    assert len(route.calls) == 1
    assert calls[0].destination == configured.rstrip("/")


@respx.mock
def test_controlled_searxng_preserves_configured_destination_but_httpx_normalizes_request():
    calls: list[ProviderCall] = []
    configured = "HTTP://SEARX.LOCAL:80/"
    route = respx.get("http://searx.local/search").mock(return_value=httpx.Response(503))
    with pytest.raises(RuntimeError):
        SearxngClient(configured, provider_controls=_controls(guard=_recording_guard(calls))).search("q")

    assert len(route.calls) == 1
    assert calls[0].destination == configured.rstrip("/")


def test_context_builder_forwards_controls_and_uses_reviewed_native_endpoints():
    from shortlist.server.services.context_builder import curator_kwargs

    controls = _controls()
    values = {"curator.provider": "openai", "curator.api_key": "sk-test", "curator.model": "gpt-test"}
    kwargs = curator_kwargs(values.get, provider_controls=controls)
    assert kwargs["provider_controls"] is controls
    assert kwargs["base_url"] == "https://api.openai.com/v1"
    assert kwargs["max_retries"] == 0
    assert kwargs["follow_redirects"] is False

    compatible = {
        "curator.provider": "openai_compatible",
        "curator.openai_base_url": "http://local.test:8080/v1",
        "curator.api_key": "",
        "curator.model": "local-reviewed-model",
    }
    compatible_kwargs = curator_kwargs(compatible.get, provider_controls=controls)
    assert compatible_kwargs["base_url"] == "http://local.test:8080/v1"
    assert compatible_kwargs["model"] == "local-reviewed-model"


def test_context_builder_forwards_controls_to_external_search_and_studio(sessions):
    from shortlist.server.services.context_builder import make_search_client
    from shortlist.server.services.poster_service import make_studio

    controls = _controls()
    values = {"llm_web.search_provider": "exa", "exa.apikey": "k", "exa.search_type": "deep-lite"}
    search = make_search_client(values.get, provider_controls=controls)
    assert search is not None and search._provider_controls is controls

    store = MagicMock()
    store.get.side_effect = lambda key: {"curator.provider": "openai", "curator.api_key": "sk-test"}.get(key, "")
    studio = make_studio(store, sessions, provider_controls=controls)
    assert studio._ai is not None and studio._ai._provider_controls is controls


def test_controlled_provider_endpoints_override_supported_sdk_environment_overrides(monkeypatch):
    import anthropic
    import openai
    from google import genai

    from shortlist.engine.curator.anthropic import AnthropicCurator
    from shortlist.engine.curator.google import GoogleCurator
    from shortlist.engine.curator.openai import OpenAICurator

    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.openai.test/v1")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://env.anthropic.test")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "env-google-key")
    openai_client = MagicMock()
    anthropic_client = MagicMock()
    google_client = MagicMock()
    monkeypatch.setattr(openai, "OpenAI", MagicMock(return_value=openai_client))
    monkeypatch.setattr(anthropic, "Anthropic", MagicMock(return_value=anthropic_client))
    monkeypatch.setattr(genai, "Client", MagicMock(return_value=google_client))
    monkeypatch.setattr(openai, "DefaultHttpxClient", MagicMock())
    monkeypatch.setattr(anthropic, "DefaultHttpxClient", MagicMock())

    controls = _controls()
    OpenAICurator(api_key="sk-test", provider_controls=controls)
    AnthropicCurator(api_key="sk-ant-test", provider_controls=controls)
    GoogleCurator(api_key="AIzaTest", provider_controls=controls)

    assert openai.OpenAI.call_args.kwargs["base_url"] == "https://api.openai.com/v1"
    assert anthropic.Anthropic.call_args.kwargs["base_url"] == "https://api.anthropic.com"
    assert genai.Client.call_args.kwargs["vertexai"] is False
    assert genai.Client.call_args.kwargs["http_options"]["base_url"] == "https://generativelanguage.googleapis.com"
