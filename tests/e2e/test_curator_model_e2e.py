"""The AI curator 'Model' field is a real dropdown that lists the provider being edited."""

from __future__ import annotations

import os

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import ShortlistApp

pytestmark = pytest.mark.e2e

LOAD = 20_000
# What each provider "offers" — the stub keys off provider so switching providers changes the list.
MODELS_BY_PROVIDER = {
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-5"],
    "google": ["gemini-2.5-flash", "gemini-2.5-pro"],
}


def _stub_models(monkeypatch) -> None:
    from types import SimpleNamespace

    def fake_make(provider, **_kw):
        return SimpleNamespace(list_models=lambda: MODELS_BY_PROVIDER.get(provider, []))

    monkeypatch.setattr("shortlist.engine.curator.make_curator", fake_make)


def test_curator_model_field_is_a_real_dropdown(page: Page, app: ShortlistApp, monkeypatch) -> None:
    app.api("PUT", "/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-fake"}})
    _stub_models(monkeypatch)

    page.goto("/settings")
    card = page.get_by_test_id("connection-llm")
    expect(card).to_be_visible(timeout=LOAD)
    card.get_by_role("button", name="Edit").click()

    # The Model field is a native <select> — a real dropdown, not a plain text input.
    select = card.locator("select")
    expect(select).to_be_visible(timeout=LOAD)
    assert select.evaluate("el => el.tagName") == "SELECT"

    # The saved provider's models are real options once the fetch resolves.
    for model in MODELS_BY_PROVIDER["anthropic"]:
        expect(select.get_by_role("option", name=model)).to_have_count(1, timeout=LOAD)
    expect(select.get_by_role("option", name="Custom…")).to_have_count(1)

    select.select_option("claude-sonnet-5")
    expect(select).to_have_value("claude-sonnet-5")


def test_switching_provider_relists_models_from_the_entered_key(page: Page, app: ShortlistApp, monkeypatch) -> None:
    # Reported by the maintainer: switching provider left the old provider's model in the dropdown. Switching must
    # clear the stale model and, once the new provider's key is entered, list THAT provider's models.
    app.api("PUT", "/api/settings", json={"values": {"curator.provider": "anthropic", "curator.api_key": "sk-fake"}})
    _stub_models(monkeypatch)

    page.goto("/settings")
    card = page.get_by_test_id("connection-llm")
    card.get_by_role("button", name="Edit").click()
    select = card.locator("select")
    expect(select.get_by_role("option", name="claude-sonnet-5")).to_have_count(1, timeout=LOAD)

    # Switch to Gemini: the stale claude model clears, and its options are gone.
    card.get_by_role("button", name="Gemini").click()
    expect(select).to_have_value("")
    expect(select.get_by_role("option", name="claude-sonnet-5")).to_have_count(0)

    # Enter the Gemini key → the dropdown relists Gemini's models (debounced live fetch).
    card.locator('input[type="password"]').fill("gemini-key")
    for model in MODELS_BY_PROVIDER["google"]:
        expect(select.get_by_role("option", name=model)).to_have_count(1, timeout=LOAD)
    # And none of the old provider's models linger.
    expect(select.get_by_role("option", name="claude-sonnet-5")).to_have_count(0)

    out = os.environ.get("MODEL_DROPDOWN_SHOT")
    if out:
        select.select_option("gemini-2.5-pro")
        card.screenshot(path=out)
