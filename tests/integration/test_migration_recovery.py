"""The 0003 collections migration must be idempotent.

SQLite auto-commits DDL, so a migration interrupted after creating the tables but before bumping
the version (two deployers racing — as happened live on SFLIX) leaves the tables present with
`alembic_version` still at 0002. A re-run must FINISH the job, not fail on "table already exists".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from rowarr.server.db import session as db_session

pytestmark = pytest.mark.integration


def test_0003_recovers_a_half_applied_migration(tmp_path: Path):
    db_session.run_migrations(tmp_path)
    engine = db_session.make_engine(tmp_path)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == "0003"
        assert conn.execute(sa.text("SELECT count(*) FROM collections")).scalar() == 1

    # Reproduce the live partial state: tables exist, version rolled back, collections emptied — AND
    # real settings present (the seed must not depend on parsing them; that's what broke on SFLIX).
    with engine.begin() as conn:
        conn.execute(sa.text("DELETE FROM collections"))
        conn.execute(sa.text("UPDATE alembic_version SET version_num='0002'"))
        conn.execute(
            sa.text("INSERT INTO settings (key, value, updated_at) VALUES ('row.size', '10', :t)"),
            {"t": "2026-01-01"},
        )

    # A re-run must not raise and must recover: re-seed the default row and reach head.
    db_session.run_migrations(tmp_path)
    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == "0003"
        assert [r[0] for r in conn.execute(sa.text("SELECT slug FROM collections")).fetchall()] == ["picked"]
