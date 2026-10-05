"""Owner-written guidance for the AI web search source (discussion #138).

The built-in web prompts (`curator/base.py`) mix two kinds of text. MECHANICS make an answer usable: today's
year, "search before answering", the exact title and release year, released titles only, the reply format.
GUIDANCE says what to favour. An owner may replace the guidance server-wide, and a row may add to it or
replace it; the mechanics are never theirs to change, because each one is load-bearing (see the comments
above `_WEB_SYSTEM`).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

INSTRUCTION_MODES = ("default", "add", "own")
MAX_INSTRUCTIONS_CHARS = 2000


@dataclass(frozen=True)
class AiInstructions:
    """One row's instructions, as the owner saved them.

    Attributes:
        mode: "default" (use the server's guidance), "add" (append to it) or "own" (replace it).
        text: The owner's words. Empty for "default".
    """

    mode: str = "default"
    text: str = ""

    @classmethod
    def from_stored(cls, value: object) -> AiInstructions | None:
        """Parse a row's stored JSON. Anything unrecognised reads as "no instructions".

        The column held curation settings until migration 0036 cleared it to ``{}``; a value this code does
        not recognise must never take a row, or a run, down.

        Args:
            value: The stored JSON value of the row's ``prompt`` column.

        Returns:
            The row's instructions, or None when it has none in effect.
        """
        if not isinstance(value, dict):
            return None
        mode, text = value.get("mode"), value.get("text")
        if mode not in ("add", "own") or not isinstance(text, str) or not text.strip():
            return None
        return cls(mode=mode, text=text.strip())


@dataclass(frozen=True)
class Guidance:
    """The guidance a prompt is built with.

    Attributes:
        replace: Text that replaces the built-in guidance. "" keeps it.
        extra: Text appended after the guidance. "" adds nothing.
    """

    replace: str = ""
    extra: str = ""

    @property
    def is_builtin(self) -> bool:
        """True when the prompt is exactly the one this source has always sent."""
        return not self.replace and not self.extra

    def fingerprint(self) -> str:
        """A short stable hash for pool keys and row recipes; "" when built-in, so nothing else changes."""
        if self.is_builtin:
            return ""
        return hashlib.blake2b(f"{self.replace}\x00{self.extra}".encode(), digest_size=8).hexdigest()


BUILTIN = Guidance()


def resolve_guidance(row: AiInstructions | None, server_text: str) -> Guidance:
    """Combine a row's instructions with the server-wide text.

    Args:
        row: The row's instructions, or None.
        server_text: The ``llm_web.instructions`` setting. "" means Shortlist's built-in guidance.

    Returns:
        The guidance for this row's prompts.
    """
    server = (server_text or "").strip()
    if row is None or row.mode not in ("add", "own") or not row.text.strip():
        return Guidance(replace=server)
    if row.mode == "own":
        return Guidance(replace=row.text.strip())
    return Guidance(replace=server, extra=row.text.strip())


def render_owner_text(text: str, *, k: int, year: int) -> str:
    """Fill ``{count}``, ``{year}`` and ``{last_year}``. Every other brace is kept exactly as typed.

    Owner text is never passed through ``str.format``: a stray brace would raise, and ``{k}`` would be
    filled with a value the owner never meant. ``{last_year}`` is what lets the built-in guidance, written
    out as a template (``curator.base.builtin_template``), keep moving with the calendar once an owner edits it.

    Args:
        text: The owner's words.
        k: How many titles the AI is asked for.
        year: The current year.

    Returns:
        The text, trimmed, with the three placeholders filled.
    """
    filled = text.strip().replace("{count}", str(k)).replace("{last_year}", str(year - 1))
    return filled.replace("{year}", str(year))
