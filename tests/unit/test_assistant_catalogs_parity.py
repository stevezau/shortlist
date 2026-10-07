from __future__ import annotations

from pathlib import Path

from shortlist.server.catalogs.generate_typescript import render_row_templates_typescript
from shortlist.server.catalogs.templates import BROWSER_ROW_INPUT_DEFAULTS, ROW_INPUT_DEFAULTS


def test_generated_frontend_row_templates_are_current() -> None:
    generated = Path("web/src/lib/row-templates.generated.ts")

    assert generated.read_text() == render_row_templates_typescript()


def test_browser_transient_defaults_are_not_advertised_to_assistants() -> None:
    """The browser needs its REST request default without making it a persistent MCP field."""
    assert BROWSER_ROW_INPUT_DEFAULTS["defer_rename"] is False
    assert "defer_rename" not in ROW_INPUT_DEFAULTS
