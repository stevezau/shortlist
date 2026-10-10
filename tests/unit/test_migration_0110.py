"""The unused assistant bootstrap-flow table is dropped, and an older binary can get it back."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from tests.unit.test_migrations import _alembic

pytestmark = pytest.mark.real_migrations

_COLUMNS = [
    "id",
    "client_id",
    "client_name",
    "deployment_proof_digest",
    "expected_machine_id",
    "owner_account_id",
    "verified_machine_id",
    "grant_id",
    "status",
    "created_at",
    "expires_at",
    "consumed_at",
]


def _columns(db: sqlite3.Connection) -> list[str]:
    return [row[1] for row in db.execute("PRAGMA table_info(assistant_bootstrap_flows)")]


def _has_table(db: sqlite3.Connection) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='assistant_bootstrap_flows'").fetchone() is not None


def test_0110_drops_the_bootstrap_table(tmp_path):
    command.upgrade(_alembic(tmp_path), "0109")
    with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
        assert _columns(db) == _COLUMNS

    command.upgrade(_alembic(tmp_path), "0110")

    with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
        assert not _has_table(db)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_0110_downgrade_recreates_the_table_as_0103_built_it(tmp_path):
    command.upgrade(_alembic(tmp_path), "0110")
    command.downgrade(_alembic(tmp_path), "0109")

    with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
        assert _columns(db) == _COLUMNS
        foreign_keys = db.execute("PRAGMA foreign_key_list(assistant_bootstrap_flows)").fetchall()
        assert [(row[2], row[3], row[4], row[6]) for row in foreign_keys] == [
            ("assistant_grants", "grant_id", "id", "SET NULL")
        ]
        table_sql = db.execute("SELECT sql FROM sqlite_master WHERE name='assistant_bootstrap_flows'").fetchone()[0]
        assert "UNIQUE" in table_sql.upper()

    command.upgrade(_alembic(tmp_path), "0110")
    with closing(sqlite3.connect(tmp_path / "shortlist.db")) as db:
        assert not _has_table(db)
