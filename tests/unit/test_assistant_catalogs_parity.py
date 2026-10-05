from __future__ import annotations

from pathlib import Path

from shortlist.server.catalogs.generate_typescript import render_row_templates_typescript


def test_generated_frontend_row_templates_are_current() -> None:
    generated = Path("web/src/lib/row-templates.generated.ts")

    assert generated.read_text() == render_row_templates_typescript()
