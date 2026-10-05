"""Persist OAuth refresh-family revocation across concurrent successor issuance.

Revision ID: 0107
Revises: 0106
"""

import sqlalchemy as sa
from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("assistant_oauth_revoked_families"):
        op.create_table(
            "assistant_oauth_revoked_families",
            sa.Column("family_id", sa.String(40), primary_key=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("assistant_oauth_revoked_families")
