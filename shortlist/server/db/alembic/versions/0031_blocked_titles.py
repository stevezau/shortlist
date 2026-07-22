"""blocked_titles — per-user "never suggest this" / "never seed from this" (issue #5)

Not everyone wants everything they watch to shape what they're shown: one Western, one football
match, one thing watched out of curiosity, and the row fills up with more of the same. Two
independent switches per title, because "stop suggesting this" and "stop taking inspiration from
this" are different requests.

Numbered from 0029 upward — 0002-0028 are reserved for the squashed pre-release revisions, and
anything inside that range is re-stamped backward and replayed on every boot.
"""

import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    bind = op.get_bind()
    if "blocked_titles" in sa.inspect(bind).get_table_names():
        op.drop_table("blocked_titles")
