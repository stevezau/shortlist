"""The engine's default model and the wizard's must be the same string.

Two defaults exist for one decision. `web/src/lib/providers.ts` carries `defaultModel`, and the
setup wizard WRITES that value into the `curator.model` setting; the engine's `DEFAULT_MODEL`
applies only when that setting is blank (a pre-wizard upgrade, or an owner who cleared the field).
They drifted — `gpt-5-mini` in the wizard against `gpt-4o-mini` in the engine — so clearing the
field silently changed which model ran, with nothing anywhere saying so.

Reading the TypeScript from Python is ugly, and it is the only way to assert the two agree: the
values live in different languages by necessity (the engine cannot import from the SPA) and nothing
else compares them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from shortlist.engine.curator.anthropic import DEFAULT_MODEL as ANTHROPIC_DEFAULT
from shortlist.engine.curator.google import DEFAULT_MODEL as GOOGLE_DEFAULT
from shortlist.engine.curator.openai import DEFAULT_MODEL as OPENAI_DEFAULT

_PROVIDERS_TS = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "providers.ts"

# Each provider entry is `id: "x", ... defaultModel: "y"` with fields in between; capture the pair.
_ENTRY = re.compile(r'id:\s*"(?P<id>[\w_]+)".*?defaultModel:\s*"(?P<model>[^"]*)"', re.DOTALL)


def _wizard_defaults() -> dict[str, str]:
    """`{provider id: defaultModel}` as the SPA declares it."""
    return {m["id"]: m["model"] for m in _ENTRY.finditer(_PROVIDERS_TS.read_text())}


class TestTheTwoDefaultsAgree:
    def test_the_provider_table_was_found_at_all(self):
        # Without this, a rename of providers.ts turns every assertion below into a vacuous pass.
        found = _wizard_defaults()
        assert {"anthropic", "openai", "google"} <= set(found), found

    @pytest.mark.parametrize(
        ("provider", "engine_default"),
        [("anthropic", ANTHROPIC_DEFAULT), ("openai", OPENAI_DEFAULT), ("google", GOOGLE_DEFAULT)],
    )
    def test_wizard_and_engine_name_the_same_model(self, provider: str, engine_default: str):
        assert _wizard_defaults()[provider] == engine_default

    @pytest.mark.parametrize(
        ("provider", "engine_default"),
        [("anthropic", ANTHROPIC_DEFAULT), ("openai", OPENAI_DEFAULT), ("google", GOOGLE_DEFAULT)],
    )
    def test_no_default_carries_a_date(self, provider: str, engine_default: str):
        """A dated snapshot id rots on the provider's retirement schedule.

        `gemini-2.5-flash` was pinned until Google retired it for new users, and every fresh Google
        install then 404'd on its first run with nothing in the UI explaining it. Aliases cannot fail
        that way, so no default may carry an 8-digit date.
        """
        assert not re.search(r"\d{8}", engine_default), engine_default
