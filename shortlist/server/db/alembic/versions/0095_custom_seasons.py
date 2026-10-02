"""`seasons` — owner-defined seasons for Seasonal rows (issue #137).

The built-ins (Valentine's, Halloween, Christmas) stay in code; this table holds only the owner's own.
It starts empty, so every existing row follows exactly the seasons it followed before. A row names a
season by `slug`, which is made from the name at creation and never changes.

Re-runnable because the create is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0095
Revises: 0094
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "seasons" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "seasons",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(64), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("emoji", sa.String(16), nullable=False),
        sa.Column("rule_kind", sa.String(8), nullable=False),
        sa.Column("month", sa.Integer, nullable=False, server_default="1"),
        sa.Column("day", sa.Integer, nullable=False, server_default="1"),
        sa.Column("nth", sa.Integer, nullable=False, server_default="1"),
        sa.Column("weekday", sa.Integer, nullable=False, server_default="0"),
        sa.Column("easter_offset", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lead_days", sa.Integer, nullable=False, server_default="7"),
        sa.Column("after_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tags", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("genre", sa.Integer, nullable=True),
        sa.Column("excluded_genres", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("collections", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("picks", sa.JSON, nullable=False, server_default="[]"),
        sa.Column("preset", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("slug"),
    )


def downgrade() -> None:
    if "seasons" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("seasons")
