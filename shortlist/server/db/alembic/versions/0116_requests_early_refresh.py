"""A "Your requests" row can be rebuilt early when one of its requests lands.

Issue #191: a per-row switch, ON for every existing row (owner decision). It only matters when
`requests_row` is true.

Revision ID: 0116
Revises: 0115
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "requests_early_refresh" not in _columns(bind, "collections"):
        op.add_column(
            "collections",
            sa.Column("requests_early_refresh", sa.Boolean(), nullable=False, server_default="1"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "requests_early_refresh" in _columns(bind, "collections"):
        with op.batch_alter_table("collections") as batch:
            batch.drop_column("requests_early_refresh")
