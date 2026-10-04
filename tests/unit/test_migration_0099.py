"""0099 adds the explore / over-time columns on `collections` and the `theme_history` table (#138 phase 4)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command

from shortlist.server.db.session import run_migrations
from tests.unit.test_migrations import _alembic

_NEW = frozenset({"theme_mode", "explore_brief", "theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows"})


def _collection_columns(config_dir: Path) -> dict[str, tuple[bool, str | None]]:
    with closing(sqlite3.connect(config_dir / "shortlist.db")) as con:
        return {r[1]: (bool(r[3]), r[4]) for r in con.execute("PRAGMA table_info(collections)")}


class TestExploreOverTime0099:
    def test_upgrade_adds_the_columns_with_off_defaults(self, tmp_path: Path):
        run_migrations(tmp_path)
        columns = _collection_columns(tmp_path)
        assert set(columns) >= _NEW
        assert columns["theme_mode"][1] == "'fixed'"
        assert columns["explore_brief"][1] == "''"
        for nullable in ("theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows"):
            assert columns[nullable] == (False, None)

    def test_existing_collection_keeps_today_s_behaviour(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0098")
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            before = con.execute("SELECT id, slug, name FROM collections").fetchall()
        assert before

        run_migrations(tmp_path)

        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            after = con.execute(
                "SELECT id, slug, name, theme_mode, explore_brief, theme_days, refresh_share,"
                " repeat_cooldown_days, avoid_rows FROM collections"
            ).fetchall()
            assert con.execute("SELECT COUNT(*) FROM theme_history").fetchone()[0] == 0
        assert [row[:3] for row in after] == before
        assert all(row[3:] == ("fixed", "", None, None, None, None) for row in after)

    def test_theme_history_table_shape(self, tmp_path: Path):
        run_migrations(tmp_path)
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            columns = {r[1]: bool(r[3]) for r in con.execute("PRAGMA table_info(theme_history)")}
        required = {"collection_id": True, "user_id": True, "state": True, "started_at": True}
        assert columns.items() >= required.items()
        assert {"theme_id", "theme_name", "due_at"} <= set(columns)

    def test_downgrade_drops_them_and_upgrade_is_repeatable(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0098")
        assert not (set(_collection_columns(tmp_path)) & _NEW)
        run_migrations(tmp_path)
        assert set(_collection_columns(tmp_path)) >= _NEW
