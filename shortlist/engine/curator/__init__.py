"""LLM curators behind one protocol. Providers are lazy-imported so extras stay optional."""

from __future__ import annotations

from shortlist.engine.curator.base import Curator, CuratorError
from shortlist.engine.curator.null import NullCurator

__all__ = ["Curator", "CuratorError", "NullCurator", "make_curator"]


def make_curator(provider: str, **kwargs) -> Curator:
    """Build a curator by provider name: anthropic | openai | openai_compatible | google | none.

    Raises:
        ValueError: Unknown provider name.
        ImportError: Provider SDK extra not installed (message names the extra).
    """
    provider = provider.lower()
    if provider in ("none", "null", ""):
        return NullCurator()
    if provider == "anthropic":
        from shortlist.engine.curator.anthropic import AnthropicCurator

        return AnthropicCurator(**kwargs)
    if provider == "openai":
        from shortlist.engine.curator.openai import OpenAICurator

        return OpenAICurator(**kwargs)
    # "ollama" is the pre-merge name for the same thing — kept so an instance configured before the
    # merge keeps working without the owner having to touch their settings.
    if provider in ("openai_compatible", "openai-compatible", "local", "ollama"):
        from shortlist.engine.curator.openai_compatible import OpenAICompatibleCurator

        return OpenAICompatibleCurator(**kwargs)
    if provider == "google":
        from shortlist.engine.curator.google import GoogleCurator

        return GoogleCurator(**kwargs)
    raise ValueError(f"unknown curator provider {provider!r}")
