"""0115 adds users.seerr_user_id (0 = automatic) with a unique index over the non-zero values."""

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


def _indexes(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[1] for row in conn.execute(f"pragma index_list({table})")}


def _insert_user(conn: sqlite3.Connection, plex_id: int, seerr_user_id: int | None = None) -> None:
    """A raw insert: the ORM would also write the column this migration adds."""
    row = {"plex_account_id": plex_id, "username": f"u{plex_id}", "slug": f"u{plex_id}"}
    if seerr_user_id is not None:
        row["seerr_user_id"] = seerr_user_id
    for _, name, kind, notnull, default, pk in conn.execute("pragma table_info(users)").fetchall():
        if notnull and default is None and not pk and name not in row:
            row[name] = 0 if "INT" in kind.upper() or "BOOL" in kind.upper() else ""
    names = ", ".join(row)
    conn.execute(f"insert into users ({names}) values ({', '.join('?' * len(row))})", list(row.values()))


def test_0115_adds_the_column_defaulting_to_automatic_and_downgrade_removes_it(tmp_path):
    cfg = _alembic(tmp_path)
    command.upgrade(cfg, "0114")
    db = tmp_path / "shortlist.db"
    with closing(sqlite3.connect(db)) as conn:
        _insert_user(conn, 1001)
        conn.commit()
    assert "seerr_user_id" not in _columns(db, "users")

    command.upgrade(cfg, "0115")

    assert "seerr_user_id" in _columns(db, "users")
    assert "ux_users_seerr_user_id" in _indexes(db, "users")
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("select seerr_user_id from users where plex_account_id = 1001").fetchone() == (0,)

    command.downgrade(cfg, "0114")
    assert "seerr_user_id" not in _columns(db, "users")
    assert "ux_users_seerr_user_id" not in _indexes(db, "users")
    command.upgrade(cfg, "0115")
    assert "seerr_user_id" in _columns(db, "users")


def test_the_unique_index_covers_chosen_accounts_only(tmp_path):
    cfg = _alembic(tmp_path)
    command.upgrade(cfg, "0115")
    db = tmp_path / "shortlist.db"
    with closing(sqlite3.connect(db)) as conn:
        _insert_user(conn, 1)
        _insert_user(conn, 2)  # two people on "automatic" are fine
        _insert_user(conn, 3, seerr_user_id=12)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_user(conn, 4, seerr_user_id=12)
