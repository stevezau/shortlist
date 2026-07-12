"""LLM curators behind one protocol. Providers are lazy-imported so extras stay optional."""

from __future__ import annotations

from rowarr.engine.curator.base import Curator, CuratorError
from rowarr.engine.curator.null import NullCurator

__all__ = ["Curator", "CuratorError", "NullCurator", "make_curator"]


def make_curator(provider: str, **kwargs) -> Curator:
    """Build a curator by provider name: anthropic | openai | google | ollama | none.

    Raises:
        ValueError: Unknown provider name.
        ImportError: Provider SDK extra not installed (message names the extra).
    """
    provider = provider.lower()
    if provider in ("none", "null", ""):
        return NullCurator()
    if provider == "anthropic":
        from rowarr.engine.curator.anthropic import AnthropicCurator

        return AnthropicCurator(**kwargs)
    if provider == "openai":
        from rowarr.engine.curator.openai import OpenAICurator

        return OpenAICurator(**kwargs)
    if provider == "google":
        from rowarr.engine.curator.google import GoogleCurator

        return GoogleCurator(**kwargs)
    if provider == "ollama":
        from rowarr.engine.curator.ollama import OllamaCurator

        return OllamaCurator(**kwargs)
    raise ValueError(f"unknown curator provider {provider!r}")
