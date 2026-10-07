"""Optional per-request controls supplied by a caller that owns paid-work authority."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProviderCall:
    """Describe one outgoing request without its prompt, credentials or response."""

    kind: Literal["completion", "native_search", "external_search", "image"]
    provider: str
    destination: str
    model: str | None = None
    output_tokens: int | None = None
    native_tool_uses: int | None = None


ProviderCallGuard = Callable[[ProviderCall], AbstractContextManager[None]]


class ProviderCallRefused(RuntimeError):
    """The requested provider operation cannot satisfy the caller's bounds."""


@dataclass(frozen=True)
class ProviderCallControls:
    """Bound individual requests; the supplied guard owns durable admission/counting."""

    guard: ProviderCallGuard
    max_output_tokens: int
    max_native_tool_uses: int
    allow_provider_managed_search: bool = False

    def output_limit(self, requested: int | None = None) -> int:
        """Return the lower of the requested output and this invocation's ceiling."""
        return self.max_output_tokens if requested is None else min(requested, self.max_output_tokens)


def provider_call(controls: ProviderCallControls | None, call: ProviderCall) -> AbstractContextManager[None]:
    """Enter the caller's request guard, leaving ordinary engine callers unchanged."""
    return nullcontext() if controls is None else controls.guard(call)
