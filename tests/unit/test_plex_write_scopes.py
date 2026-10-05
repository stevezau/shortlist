"""`PLEX_WRITE_SCOPES` names only scopes the code really writes, so the "Changes on Plex" filter cannot rot.

A scope renamed at its writer and not here would silently drop that change from the filtered feed, with
every test that seeds the constant's own strings still passing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from shortlist.server.services import audit
from shortlist.server.services.audit import PLEX_WRITE_SCOPES, RESTRICTION_RESTORED_SCOPE

SHORTLIST = Path(__file__).resolve().parents[2] / "shortlist"
AUDIT = Path(audit.__file__).resolve()


def _source_outside_the_constant() -> str:
    """Every `.py` under `shortlist/`, minus the lines that define `PLEX_WRITE_SCOPES` itself."""
    texts = []
    for path in sorted(SHORTLIST.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.resolve() == AUDIT:
            node = next(
                n
                for n in ast.parse(text).body
                if isinstance(n, ast.AnnAssign | ast.Assign)
                and "PLEX_WRITE_SCOPES" in {t.id for t in ast.walk(n) if isinstance(t, ast.Name)}
            )
            lines = text.splitlines()
            text = "\n".join(lines[: node.lineno - 1] + lines[node.end_lineno :])
        texts.append(text)
    return "\n".join(texts)


class TestPlexWriteScopes:
    @pytest.mark.parametrize("scope", sorted(PLEX_WRITE_SCOPES))
    def test_every_scope_is_written_somewhere_in_shortlist(self, scope: str):
        source = _source_outside_the_constant()

        assert f'"{scope}"' in source or f"'{scope}'" in source, f"nothing in shortlist/ writes {scope!r}"

    def test_the_restored_restriction_scope_is_a_plex_write(self):
        assert RESTRICTION_RESTORED_SCOPE in PLEX_WRITE_SCOPES

    def test_unpausing_a_person_is_a_plex_write(self):
        """`promote_user_rows` puts the person's rows back on Home — as much a visibility change as the
        pause that hid them."""
        assert "user.unpause.restore" in PLEX_WRITE_SCOPES
