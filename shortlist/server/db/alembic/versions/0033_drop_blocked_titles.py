"""Drop blocked_titles — the per-title ignore list, withdrawn pending clarification

Built for issue #5, then pulled back before anyone relied on it: the requester described blocking
individual titles, but the underlying want ("don't let Westerns / sports / adult shape my row") may
be better served at a category level, and shipping a UI on our reading of it risked committing to
the wrong shape. Asked him how he'd want it to work instead.

Dropped rather than left in place because `tests/unit/test_migration_initial.py` holds the migration
tree and the ORM models in lockstep — an orphan table would fail that guard.

0031 CREATED this table and is deliberately left intact: it is already applied on instances running
`dev`, and deleting it would strand them at a revision alembic can no longer resolve.
"""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "blocked_titles" in sa.inspect(bind).get_table_names():
        op.drop_table("blocked_titles")


def downgrade() -> None:
    """Re-create it exactly as 0031 did, so the pair round-trips."""
    bind = op.get_bind()
    if "blocked_titles" in sa.inspect(bind).get_table_names():
        return
    op.create_table(
        "blocked_titles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("block_pick", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("block_seed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "tmdb_id", "media_type", name="uq_blocked_title"),
    )
    op.create_index("ix_blocked_titles_user_id", "blocked_titles", ["user_id"])
