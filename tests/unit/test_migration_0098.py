"""0098 adds the `themes` table and the AI-row columns on `collections` (#138 phase 3)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from alembic import command

from shortlist.server.db.session import run_migrations
from tests.unit.test_migrations import _alembic

_NEW = frozenset({"theme_id", "ai_paused", "ai_tokens"})


def _collection_columns(config_dir: Path) -> dict[str, tuple[bool, str | None]]:
    with closing(sqlite3.connect(config_dir / "shortlist.db")) as con:
        return {r[1]: (bool(r[3]), r[4]) for r in con.execute("PRAGMA table_info(collections)")}


def _tables(config_dir: Path) -> set[str]:
    with closing(sqlite3.connect(config_dir / "shortlist.db")) as con:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class TestAiRowThemes0098:
    def test_upgrade_creates_the_themes_table(self, tmp_path: Path):
        run_migrations(tmp_path)
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            columns = {r[1] for r in con.execute("PRAGMA table_info(themes)")}
        assert columns >= {
            "id",
            "slug",
            "name",
            "emoji",
            "brief",
            "origin",
            "media",
            "tags",
            "genres",
            "excluded_genres",
            "collections",
            "picks",
            "rules",
            "content_hash",
            "created_at",
            "ai_tokens",
            "stats",
        }

    def test_existing_collection_keeps_today_s_behaviour(self, tmp_path: Path):
        command.upgrade(_alembic(tmp_path), "0097")
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            before = con.execute("SELECT id, slug, name FROM collections").fetchall()
        assert before

        run_migrations(tmp_path)

        assert set(_collection_columns(tmp_path)) >= _NEW
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            after = con.execute("SELECT id, slug, name, theme_id, ai_paused, ai_tokens FROM collections").fetchall()
        assert [r[:3] for r in after] == before
        assert all(r[3:] == (None, 0, 0) for r in after)

    def test_deleting_a_theme_unlinks_its_rows(self, tmp_path: Path):
        run_migrations(tmp_path)
        with closing(sqlite3.connect(tmp_path / "shortlist.db")) as con:
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("INSERT INTO themes (id, slug, name, origin) VALUES (1, 't', 'T', 'ai')")
            con.execute("UPDATE collections SET theme_id = 1")
            con.execute("DELETE FROM themes WHERE id = 1")
            assert {r[0] for r in con.execute("SELECT theme_id FROM collections")} == {None}

    def test_running_it_again_over_an_already_migrated_database_is_a_no_op(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.stamp(_alembic(tmp_path), "0097")
        run_migrations(tmp_path)
        assert "themes" in _tables(tmp_path)
        assert set(_collection_columns(tmp_path)) >= _NEW

    def test_the_downgrade_round_trips(self, tmp_path: Path):
        run_migrations(tmp_path)
        command.downgrade(_alembic(tmp_path), "0097")
        assert "themes" not in _tables(tmp_path)
        assert not (_NEW & set(_collection_columns(tmp_path)))
        run_migrations(tmp_path)
        assert "themes" in _tables(tmp_path)
