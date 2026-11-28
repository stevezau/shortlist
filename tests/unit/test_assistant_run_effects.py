"""The paid-effect contract names the external search host a bounded run may call."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shortlist.server.assistant.policy import ChangeError
from shortlist.server.assistant.run_effects import paid_effect_contract


def _contract(settings: dict, *, max_provider_calls: int = 5):
    row = SimpleNamespace(candidate_sources=["llm_web"], seasons=False, theme=None)
    config = SimpleNamespace(
        rows=[row], should_build=lambda _row: True, candidate_sources=["llm_web"], dry_run=True, requests=None
    )
    store = SimpleNamespace(get=settings.get)
    intent = SimpleNamespace(max_provider_calls=max_provider_calls)
    return paid_effect_contract(config, store, intent, config_hash=lambda _cfg: "hash")


_OPENAI = {"curator.provider": "openai", "curator.api_key": "ciphertext", "curator.model": "gpt-test"}


class TestKeenableEffects:
    def test_a_keyless_keenable_run_declares_the_search_host_and_the_model_call(self):
        out = _contract({**_OPENAI, "llm_web.search_provider": "keenable"})

        search, completion = out["providers"]
        assert (search["kind"], search["provider"], search["destination"]) == (
            "external_search",
            "keenable",
            "https://api.keenable.ai",
        )
        assert completion["kind"] == "completion"  # Keenable returns snippets; the curator reads them
        assert out["provider_managed_search"] is False

    def test_a_saved_key_changes_nothing_about_the_declared_effect(self):
        keyless = _contract({**_OPENAI, "llm_web.search_provider": "keenable"})
        keyed = _contract({**_OPENAI, "llm_web.search_provider": "keenable", "keenable.apikey": "secret"})
        assert keyed == keyless
        assert "secret" not in repr(keyed)

    def test_keenable_without_a_model_is_refused_like_searxng(self):
        with pytest.raises(ChangeError, match="Keenable recommendations also require a configured model"):
            _contract({"llm_web.search_provider": "keenable"})

    def test_a_run_needs_an_explicit_provider_call_budget(self):
        with pytest.raises(ChangeError):
            _contract({**_OPENAI, "llm_web.search_provider": "keenable"}, max_provider_calls=0)
