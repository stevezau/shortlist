"""Explore mode and over-time controls: six columns on `collections` and the `theme_history` table (#138, phase 4)

Every new column defaults to today's behaviour (`theme_mode` "fixed", the rest empty or NULL), so an
existing row is unchanged. `theme_history` records which theme a row showed each person and when,
so "avoid the last N" and a repeat cooldown survive a deleted theme (the name is copied).

Re-runnable because each step is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0099
Revises: 0098
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

_INDEX = "ix_theme_history_target"
_COLUMNS = ("theme_mode", "explore_brief", "theme_days", "refresh_share", "repeat_cooldown_days", "avoid_rows")


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c["name"] for c in sa.inspect(bind).get_columns("collections")}
    with op.batch_alter_table("collections") as batch:
        if "theme_mode" not in existing:
            batch.add_column(sa.Column("theme_mode", sa.String(16), nullable=False, server_default="fixed"))
        if "explore_brief" not in existing:
            batch.add_column(sa.Column("explore_brief", sa.String(500), nullable=False, server_default=""))
        if "theme_days" not in existing:
            batch.add_column(sa.Column("theme_days", sa.Integer(), nullable=True))
        if "refresh_share" not in existing:
            batch.add_column(sa.Column("refresh_share", sa.Float(), nullable=True))
        if "repeat_cooldown_days" not in existing:
            batch.add_column(sa.Column("repeat_cooldown_days", sa.Integer(), nullable=True))
        if "avoid_rows" not in existing:
            batch.add_column(sa.Column("avoid_rows", sa.JSON(), nullable=True))
    if not sa.inspect(bind).has_table("theme_history"):
        op.create_table(
            "theme_history",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "collection_id", sa.Integer(), sa.ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("theme_id", sa.Integer(), sa.ForeignKey("themes.id", ondelete="SET NULL"), nullable=True),
            sa.Column("theme_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("due_at", sa.DateTime(), nullable=True),
        )
        op.create_index(_INDEX, "theme_history", ["collection_id", "user_id", "state"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("theme_history"):
        op.drop_index(_INDEX, table_name="theme_history")
        op.drop_table("theme_history")
    existing = {c["name"] for c in sa.inspect(bind).get_columns("collections")}
    present = [name for name in _COLUMNS if name in existing]
    if present:
        with op.batch_alter_table("collections") as batch:
            for name in present:
                batch.drop_column(name)
