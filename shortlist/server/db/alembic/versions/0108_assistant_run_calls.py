"""Checkpoint every bounded assistant-run provider invocation.

Revision ID: 0108
Revises: 0107
"""

import sqlalchemy as sa
from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("assistant_run_calls"):
        op.create_table(
            "assistant_run_calls",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("operation_id", sa.String(64), nullable=False),
            sa.Column("kind", sa.String(32), nullable=False),
            sa.Column("provider", sa.String(64), nullable=False),
            sa.Column("destination", sa.String(2048), nullable=False),
            sa.Column("model", sa.String(255), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("native_tool_uses", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_assistant_run_calls_run_status", "assistant_run_calls", ["run_id", "status"])


def downgrade() -> None:
    op.drop_table("assistant_run_calls")
