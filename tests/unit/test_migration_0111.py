"""0111 drops the dead collection_user_overrides.prompt column and keeps the rest of the row."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy.orm import Session

from shortlist.server.db.models import Collection, User
from shortlist.server.db.session import make_engine
from tests.db_helpers import disposing_engine
from tests.unit.test_migrations import _alembic

pytestmark = pytest.mark.real_migrations


def _columns(db_path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[1] for row in conn.execute("pragma table_info(collection_user_overrides)")}


def _keys(db_path) -> tuple[list, list]:
    """The table's foreign keys and indexes (the primary key's autoindex included); a rebuild must keep both."""
    with closing(sqlite3.connect(db_path)) as conn:
        return (
            # A rebuild renumbers the keys (column 0), so compare them by what they reference.
            sorted(row[2:] for row in conn.execute("pragma foreign_key_list(collection_user_overrides)")),
            conn.execute("pragma index_list(collection_user_overrides)").fetchall(),
        )


def test_0111_drops_prompt_and_keeps_the_other_columns(tmp_path):
    cfg = _alembic(tmp_path)
    command.upgrade(cfg, "0110")
    db = tmp_path / "shortlist.db"
    with disposing_engine(make_engine(tmp_path)) as engine, Session(engine) as session:
        session.add_all(
            [User(id=1, plex_account_id=1, username="bob", slug="bob"), Collection(id=500, slug="s", name="S")]
        )
        session.commit()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "insert into collection_user_overrides (collection_id, user_id, muted, row_size, recent_count, prompt, "
            "updated_at) values (500, 1, 1, 12, 5, '{}', '2026-01-01 00:00:00')"
        )
        conn.commit()
    assert "prompt" in _columns(db)
    keys = _keys(db)

    command.upgrade(cfg, "0111")

    assert "prompt" not in _columns(db)
    assert _keys(db) == keys
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("select muted, row_size, recent_count from collection_user_overrides").fetchall() == [
            (1, 12, 5)
        ]

    command.downgrade(cfg, "0110")
    assert "prompt" in _columns(db)
    command.upgrade(cfg, "0111")
    assert "prompt" not in _columns(db)


def test_0036_still_clears_a_filled_override_prompt_below_0111(tmp_path):
    """0036 gained a column-exists guard for replays at head; on a real pre-0036 database it must still clear."""
    cfg = _alembic(tmp_path)
    command.upgrade(cfg, "0035")
    db = tmp_path / "shortlist.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "insert into collection_user_overrides (collection_id, user_id, muted, prompt, updated_at) "
            """values (500, 1, 0, '{"tone": "z"}', '2026-01-01 00:00:00')"""
        )
        conn.commit()

    command.upgrade(cfg, "0036")

    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("select prompt from collection_user_overrides").fetchall() == [("{}",)]
