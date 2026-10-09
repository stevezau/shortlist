"""Drop the unused assistant bootstrap-flow table.

The first-run bootstrap proof was never wired to any route or caller; the table only ever held
nothing. Downgrade recreates it exactly as 0103 did so an older binary finds its schema.
"""

import sqlalchemy as sa
from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("assistant_bootstrap_flows"):
        op.drop_table("assistant_bootstrap_flows")


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("assistant_bootstrap_flows"):
        return
    op.create_table(
        "assistant_bootstrap_flows",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("client_name", sa.String(255), nullable=False),
        sa.Column("deployment_proof_digest", sa.String(64), nullable=False, unique=True),
        sa.Column("expected_machine_id", sa.String(128), nullable=True),
        sa.Column("owner_account_id", sa.Integer, nullable=True),
        sa.Column("verified_machine_id", sa.String(128), nullable=True),
        sa.Column(
            "grant_id",
            sa.String(40),
            sa.ForeignKey("assistant_grants.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
