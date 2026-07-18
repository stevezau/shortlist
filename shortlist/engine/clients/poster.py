"""Poster-art client contract: turn a row's poster text into image bytes for its Plex collection.

The engine only knows this Protocol; the server injects a concrete studio that can render two ways:
a built-in text poster (Pillow — always available, no API key), or an AI image (OpenAI/Google, reusing
the curator key). ``engine`` selects which. A poster is cosmetic: ``render`` may return ``None`` (or
raise) and delivery carries on with Plex's own artwork.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PosterArtist(Protocol):
    def render(self, *, title: str, subtitle: str, style: str, engine: str) -> bytes | None:
        """Produce a portrait poster image.

        Args:
            title: Headline text, placeholders already substituted for this user/library.
            subtitle: Secondary text, likewise substituted.
            style: Free-text art-style guidance (used by the AI engine; a hint for the text engine).
            engine: "text" for the built-in renderer, "ai" for the image model.

        Returns:
            Encoded image bytes (PNG/JPEG) for ``uploadPoster``, or ``None`` if none could be made
            (e.g. AI engine requested but no image-capable provider is configured).
        """
        ...
