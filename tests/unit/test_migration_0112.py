"""0112 adds the nullable favourite_count and older_count columns and keeps every existing row."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy import insert
from sqlalchemy.orm import Session

from shortlist.server.db.models import Collection, User
from shortlist.server.db.session import make_engine
from tests.db_helpers import disposing_engine
from tests.unit.test_migrations import _alembic

pytestmark = pytest.mark.real_migrations

_TABLES = ("collections", "collection_user_overrides")


def _columns(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def test_0112_adds_nullable_columns_and_downgrade_removes_them(tmp_path):
    cfg = _alembic(tmp_path)
    command.upgrade(cfg, "0111")
    db = tmp_path / "shortlist.db"
    with disposing_engine(make_engine(tmp_path)) as engine, Session(engine) as session:
        session.add(User(id=1, plex_account_id=1, username="bob", slug="bob"))
        # A core insert: the ORM would also write the columns this migration adds.
        session.execute(insert(Collection).values(id=500, slug="s", name="S"))
        session.commit()
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "insert into collection_user_overrides (collection_id, user_id, muted, recent_count, updated_at) "
            "values (500, 1, 0, 5, '2026-01-01 00:00:00')"
        )
        conn.commit()
    assert all(not {"favourite_count", "older_count"} & _columns(db, table) for table in _TABLES)

    command.upgrade(cfg, "0112")

    for table in _TABLES:
        assert {"favourite_count", "older_count"} <= _columns(db, table)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute(
            "select recent_count, favourite_count, older_count from collection_user_overrides"
        ).fetchall() == [(5, None, None)]
        rows = conn.execute("select favourite_count, older_count from collections").fetchall()
        assert rows and set(rows) == {(None, None)}

    command.downgrade(cfg, "0111")
    assert all(not {"favourite_count", "older_count"} & _columns(db, table) for table in _TABLES)
    command.upgrade(cfg, "0112")
    assert {"favourite_count", "older_count"} <= _columns(db, "collections")
