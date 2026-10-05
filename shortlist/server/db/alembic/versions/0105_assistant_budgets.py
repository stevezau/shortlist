"""Reserve assistant provider calls atomically with approved operations.

Revision ID: 0105
Revises: 0104
"""

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("assistant_budgets"):
        op.create_table(
            "assistant_budgets",
            sa.Column("grant_id", sa.String(64), primary_key=True),
            sa.Column("provider_calls_reserved", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    op.drop_table("assistant_budgets")
