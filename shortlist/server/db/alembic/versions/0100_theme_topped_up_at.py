"""themes.topped_up_at: when an AI row's theme was extended by its one extra AI call (#138)

NULL means never topped up, which is every existing theme, so nothing changes on upgrade. The nightly
`themes.rotate` job makes at most one extra AI call per theme and stamps this the moment it does.

Re-runnable because the step is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0100
Revises: 0099
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("themes")}
    if "topped_up_at" not in existing:
        with op.batch_alter_table("themes") as batch:
            batch.add_column(sa.Column("topped_up_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("themes")}
    if "topped_up_at" in existing:
        with op.batch_alter_table("themes") as batch:
            batch.drop_column("topped_up_at")
