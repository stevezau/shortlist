"""Per-row limits: longest runtime, release-year range and lowest rating (issue #138)

Four nullable columns on `collections`. NULL means "no limit", so every row already in the database
keeps today's behaviour and no recipe changes on the night this ships.

Re-runnable because each add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0097
Revises: 0096
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("max_runtime", sa.Integer),
    ("min_year", sa.Integer),
    ("max_year", sa.Integer),
    ("min_rating", sa.Float),
)


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("collections")}


def upgrade() -> None:
    existing = _columns(op.get_bind())
    for name, kind in _COLUMNS:
        if name not in existing:
            op.add_column("collections", sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    existing = _columns(op.get_bind())
    present = [name for name, _ in _COLUMNS if name in existing]
    if present:
        # SQLite cannot drop several columns in one ALTER; batch mode rebuilds the table once.
        with op.batch_alter_table("collections") as batch:
            for name in present:
                batch.drop_column(name)
