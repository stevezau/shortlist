"""0100 adds the nullable `themes.topped_up_at` (#138): every existing theme is never-topped-up."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command

from shortlist.server.db.session import run_migrations
from tests.unit.test_migrations import _alembic


def _theme_columns(config_dir: Path) -> dict[str, bool]:
    with closing(sqlite3.connect(config_dir / "shortlist.db")) as con:
        return {r[1]: bool(r[3]) for r in con.execute("PRAGMA table_info(themes)")}


class TestToppedUpAt0100:
    def test_upgrade_adds_a_nullable_column(self, tmp_path: Path):
        run_migrations(tmp_path)

        assert _theme_columns(tmp_path)["topped_up_at"] is False

    def test_an_existing_theme_is_never_topped_up(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0099")
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            con.execute("INSERT INTO themes (slug, name) VALUES ('t', 'T')")
            con.commit()

        run_migrations(tmp_path)

        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            assert con.execute("SELECT topped_up_at FROM themes").fetchall() == [(None,)]

    def test_downgrade_drops_the_column(self, tmp_path: Path):
        run_migrations(tmp_path)

        command.downgrade(_alembic(tmp_path), "0099")

        assert "topped_up_at" not in _theme_columns(tmp_path)
