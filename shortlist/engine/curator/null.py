"""Heuristic mode — the app is fully functional with zero LLM keys."""

from __future__ import annotations


class NullCurator:
    name = "none"
    supports_native_web_search = False  # not an LLM at all — it has no search tool to offer
    # There is no model to ask, so callers must not spend a search and then throw the answer away.
    # Every real curator leaves this at its `getattr(..., True)` default; only this one says no.
    can_complete = False
    last_tokens = 0  # no LLM call, so callers can read this uniformly without a getattr fallback

    def complete(self, system: str, user: str) -> str:
        """No model to call.

        Returns "" so the SearXNG and native paths degrade to nothing, which is correct for both:
        raw snippets need reading, and native search IS the model. The Exa path never asks — it
        checks `can_complete` and uses Exa's own extracted titles instead.
        """
        return ""
