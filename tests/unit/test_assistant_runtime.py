"""Opt-in routing must use configured canonical URLs and preserve proxy prefixes."""

import pytest

from shortlist.server.assistant.runtime import settings_from_environment


def test_disabled_by_default():
    assert settings_from_environment({}, base_path="").enabled is False


@pytest.mark.parametrize(
    "base,url", [("", "http://127.0.0.1:5959/mcp"), ("/shortlist", "https://media.example/shortlist/mcp")]
)
def test_exact_canonical_path(base, url):
    settings = settings_from_environment({"SHORTLIST_MCP_URL": url}, base_path=base)
    settings.validate()
    assert settings.resource == url
    assert settings.issuer == url.removesuffix("/mcp") + "/assistant/oauth"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/wrong",
        "https://user:secret@example.com/mcp",
        "https://example.com/mcp?token=bad",
        "http://192.0.2.1/mcp",
    ],
)
def test_ambiguous_or_insecure_canonical_urls_are_rejected(url):
    with pytest.raises(ValueError):
        settings_from_environment({"SHORTLIST_MCP_URL": url}, base_path="")
