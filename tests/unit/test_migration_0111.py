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

    command.upgrade(cfg, "0111")

    assert "prompt" not in _columns(db)
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("select muted, row_size, recent_count from collection_user_overrides").fetchall() == [
            (1, 12, 5)
        ]

    command.downgrade(cfg, "0110")
    assert "prompt" in _columns(db)
    command.upgrade(cfg, "0111")
    assert "prompt" not in _columns(db)
