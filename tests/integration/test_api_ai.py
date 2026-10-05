"""POST /api/ai/web-prompt-preview: the exact system prompt AI web search would send (#138)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from shortlist.server.auth import SESSION_COOKIE
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration

URL = "/api/ai/web-prompt-preview"


def test_preview_shows_the_builtin_prompt_when_nothing_is_set(client: TestClient):
    body = client.post(URL, json={}).json()
    assert body["backend"] == "native"
    assert "Strongly prefer titles released in" in body["system"]
    assert body["builtin_guidance"].startswith("Based on what this person recently watched")
    assert body["inert"] is True  # a fresh install has no AI provider, so instructions do nothing yet


def test_preview_carries_the_builtin_guidance_as_a_template_for_write_your_own(client: TestClient):
    body = client.post(URL, json={}).json()
    assert body["builtin_template"].startswith("Based on what this person recently watched, give {count} titles")
    assert "released in {last_year} or {year};" in body["builtin_template"]


def test_preview_applies_a_rows_instructions_over_the_saved_default(client: TestClient):
    values = {"llm_web.instructions": "Favour classics.", "llm_web.search_provider": "exa"}
    assert client.put("/api/settings", json={"values": values}).status_code == 200
    body = client.post(URL, json={"ai_instructions": {"mode": "add", "text": "No kids films."}}).json()
    assert body["backend"] == "exa"
    assert "Favour classics. Pick up to" in body["system"]
    assert "The server owner adds, for this row: No kids films." in body["system"]


def test_preview_can_try_unsaved_server_text(client: TestClient):
    body = client.post(URL, json={"server_text": "Any decade."}).json()
    assert "Any decade. Give up to" in body["system"]


def test_exa_without_an_ai_provider_is_inert(client: TestClient):
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("llm_web.search_provider", "exa")
        store.set("curator.provider", "none")
    assert client.post(URL, json={}).json()["inert"] is True


@pytest.mark.parametrize(
    ("backend", "provider", "inert"),
    [
        ("native", "none", True),
        ("native", "", True),
        ("native", "anthropic", False),
        ("native", "openai", False),
        ("native", "google", False),
        # A provider with no search tool of its own: native AI web search never runs (`_web_search_capable`).
        ("native", "openai_compatible", True),
        ("native", "ollama", True),
        ("exa", "anthropic", False),
        ("exa", "openai_compatible", False),
        ("searxng", "ollama", False),
        ("searxng", "none", True),
    ],
)
def test_inert_follows_the_ai_provider_for_every_backend(client: TestClient, backend: str, provider: str, inert: bool):
    with client.app.state.sessions() as session:
        store = SettingsStore(session, client.app.state.secrets)
        store.set("llm_web.search_provider", backend)
        store.set("curator.provider", provider)
    assert client.post(URL, json={}).json()["inert"] is inert


def test_preview_writes_nothing(client: TestClient):
    from shortlist.server.db.models import Event

    with client.app.state.sessions() as session:
        events_before = session.query(Event).count()
    client.post(URL, json={"server_text": "Any decade."})
    with client.app.state.sessions() as session:
        assert SettingsStore(session, client.app.state.secrets).get("llm_web.instructions") == ""
        assert session.query(Event).count() == events_before


def test_preview_needs_the_owner(client: TestClient):
    client.cookies.delete(SESSION_COOKIE)
    assert client.post(URL, json={}).status_code in (401, 403)
