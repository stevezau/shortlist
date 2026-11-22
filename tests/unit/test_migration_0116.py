"""0116 adds collections.requests_early_refresh, switched ON for every existing row."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from tests.unit.test_migrations import _alembic

pytestmark = pytest.mark.real_migrations


def _columns(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[1] for row in conn.execute(f"pragma table_info({table})")}


def _insert_collection(conn: sqlite3.Connection) -> None:
    """A raw insert: the ORM would also write the column this migration adds."""
    row: dict[str, object] = {}
    for _, name, kind, notnull, default, pk in conn.execute("pragma table_info(collections)").fetchall():
        if notnull and default is None and not pk:
            row[name] = 0 if "INT" in kind.upper() or "BOOL" in kind.upper() else "x"
    names = ", ".join(row)
    conn.execute(f"insert into collections ({names}) values ({', '.join('?' * len(row))})", list(row.values()))


def test_0116_adds_the_column_on_for_existing_rows_and_downgrade_removes_it(tmp_path):
    cfg = _alembic(tmp_path)
    command.upgrade(cfg, "0115")
    db = tmp_path / "shortlist.db"
    with closing(sqlite3.connect(db)) as conn:
        _insert_collection(conn)
        conn.commit()
    assert "requests_early_refresh" not in _columns(db, "collections")

    command.upgrade(cfg, "0116")

    assert "requests_early_refresh" in _columns(db, "collections")
    with closing(sqlite3.connect(db)) as conn:
        flags = conn.execute("select requests_early_refresh from collections").fetchall()
    assert flags
    assert set(flags) == {(1,)}

    command.downgrade(cfg, "0115")
    assert "requests_early_refresh" not in _columns(db, "collections")
    command.upgrade(cfg, "0116")
    assert "requests_early_refresh" in _columns(db, "collections")
