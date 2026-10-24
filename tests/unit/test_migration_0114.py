"""0114 adds the nullable per-row library skip list and keeps every existing row."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy import insert
from sqlalchemy.orm import Session

from shortlist.server.db.models import Collection
from shortlist.server.db.session import make_engine
from tests.db_helpers import disposing_engine
from tests.unit.test_migrations import _alembic

pytestmark = pytest.mark.real_migrations


def _columns(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def test_0114_adds_a_nullable_column_and_downgrade_removes_it(tmp_path):
    cfg = _alembic(tmp_path)
    command.upgrade(cfg, "0113")
    db = tmp_path / "shortlist.db"
    with disposing_engine(make_engine(tmp_path)) as engine, Session(engine) as session:
        # A core insert: the ORM would also write the column this migration adds.
        session.execute(insert(Collection).values(id=500, slug="s", name="S"))
        session.commit()
    assert "req_skip_library_keys" not in _columns(db, "collections")

    command.upgrade(cfg, "0114")

    assert "req_skip_library_keys" in _columns(db, "collections")
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("select req_skip_library_keys from collections where id = 500").fetchone() == (None,)

    command.downgrade(cfg, "0113")
    assert "req_skip_library_keys" not in _columns(db, "collections")
    command.upgrade(cfg, "0114")
    assert "req_skip_library_keys" in _columns(db, "collections")
