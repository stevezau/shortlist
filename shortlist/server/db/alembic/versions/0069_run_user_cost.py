"""Record what each ROW cost a person, not just what the whole person cost.

`run_users` carries one `duration_ms` and one `llm_tokens` per person per run, so the rows-first run
view printed that same pair under every row the person was in — on run #7 (SFLIX, 2026-08-13) Alex
Mastroianni read "7m 22s · 15,917 AI tokens" identically under both his rows, which looks like two
rows each independently costing 7m 22s. Neither was true.

NULL means "not recorded", exactly as `rows_considered` uses `{}`, and is deliberately NOT
backfilled: a legacy run has no per-row measurement, and writing zeros would claim every historical
row took no time at all — the same confidently-wrong answer, pointed at the whole archive.

Re-runnable, per `tests/integration/test_migration_recovery.py`: a crash between the DDL and the
version stamp replays this revision, and a bare `add_column` would then fail with "duplicate column
name" and wedge every later upgrade.

Revision ID: 0069
Revises: 0068
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {row[1] for row in bind.execute(sa.text("PRAGMA table_info(run_users)"))}


def upgrade() -> None:
    bind = op.get_bind()
    if "cost" not in _columns(bind):
        op.add_column("run_users", sa.Column("cost", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "cost" in _columns(bind):
        op.drop_column("run_users", "cost")
