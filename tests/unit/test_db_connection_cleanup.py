"""Migration housekeeping closes SQLite connections without changing transactions."""

from __future__ import annotations

import sqlite3
from contextlib import closing, nullcontext
from pathlib import Path

import pytest

from shortlist.server.db import session as database

pytestmark = pytest.mark.real_migrations


@pytest.mark.parametrize("fail_drop", [False, True])
def test_batch_sweep_closes_connection_and_preserves_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_drop: bool
) -> None:
    path = tmp_path / "shortlist.db"
    connect = sqlite3.connect
    with closing(connect(path)) as connection:
        connection.execute("CREATE TABLE transaction_probe (value INTEGER)")
        connection.execute("CREATE TABLE _alembic_tmp_leftover (value INTEGER)")

    class TrackingConnection(sqlite3.Connection):
        closed = False

        def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            if sql.startswith("SELECT name FROM sqlite_master"):
                super().execute("INSERT INTO transaction_probe VALUES (1)")
            if fail_drop and sql.startswith("DROP TABLE"):
                raise sqlite3.OperationalError("simulated drop failure")
            return super().execute(sql, parameters)

        def close(self) -> None:
            self.closed = True
            super().close()

    opened: list[TrackingConnection] = []

    def tracked_connect(path: Path) -> TrackingConnection:
        connection = connect(path, factory=TrackingConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(database.sqlite3, "connect", tracked_connect)
    expected = pytest.raises(sqlite3.OperationalError, match="simulated drop failure") if fail_drop else nullcontext()
    with expected:
        database._sweep_batch_leftovers(tmp_path)

    assert len(opened) == 1
    assert opened[0].closed
    with closing(connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM transaction_probe").fetchone() == (0 if fail_drop else 1,)
        leftover = connection.execute("SELECT name FROM sqlite_master WHERE name = '_alembic_tmp_leftover'").fetchone()
        assert bool(leftover) is fail_drop
