"""Heuristic mode — the app is fully functional with zero LLM keys."""

from __future__ import annotations


class NullCurator:
    name = "none"
    supports_native_web_search = False  # not an LLM at all — the llm_web source no-ops for it
    last_tokens = 0  # no LLM call, so callers can read this uniformly without a getattr fallback

    def complete(self, system: str, user: str) -> str:
        """No model to call — heuristic mode contributes nothing to the external-search llm_web path."""
        return ""
