"""AI custom rows: the `themes` table and three columns on `collections` (issue #138, phase 3)

A theme is a named, resolved set of titles a row can be built from. `collections.theme_id` points a
row at one; ON DELETE SET NULL so removing a theme unlinks its rows rather than deleting them.
`ai_paused` and `ai_tokens` default to off / zero, so every existing row behaves as before.

Re-runnable because each step is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0098
Revises: 0097
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None

_FK = "fk_collections_theme_id_themes"


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("collections")}


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("themes"):
        op.create_table(
            "themes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("slug", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("emoji", sa.String(16), nullable=True),
            sa.Column("brief", sa.Text(), nullable=False, server_default=""),
            sa.Column("origin", sa.String(16), nullable=False, server_default="manual"),
            sa.Column("media", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("genres", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("excluded_genres", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("collections", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("picks", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("rules", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.current_timestamp()
            ),
            sa.Column("ai_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("stats", sa.JSON(), nullable=False, server_default="{}"),
        )
        op.create_index("ix_themes_slug", "themes", ["slug"], unique=True)

    existing = _columns(bind)
    if "ai_paused" not in existing:
        op.add_column("collections", sa.Column("ai_paused", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "ai_tokens" not in existing:
        op.add_column("collections", sa.Column("ai_tokens", sa.Integer(), nullable=False, server_default="0"))
    if "theme_id" not in existing:
        # SQLite cannot ADD a foreign key; batch mode rebuilds the table to carry it.
        with op.batch_alter_table("collections") as batch:
            batch.add_column(sa.Column("theme_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(_FK, "themes", ["theme_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    existing = _columns(op.get_bind())
    present = [name for name in ("theme_id", "ai_paused", "ai_tokens") if name in existing]
    if present:
        with op.batch_alter_table("collections") as batch:
            if "theme_id" in present:
                batch.drop_constraint(_FK, type_="foreignkey")
            for name in present:
                batch.drop_column(name)
    if sa.inspect(op.get_bind()).has_table("themes"):
        op.drop_table("themes")
