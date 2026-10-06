"""Durable per-title assistant acquisition dispatches.

Revision ID: 0106
Revises: 0105
"""

import sqlalchemy as sa
from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("assistant_request_dispatches"):
        op.create_table(
            "assistant_request_dispatches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "operation_id",
                sa.String(64),
                sa.ForeignKey("assistant_operations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("origin", sa.String(16), nullable=False, server_default="assistant"),
            sa.Column("candidate_id", sa.Integer(), nullable=True),
            sa.Column("destination", sa.String(2048), nullable=False),
            sa.Column("request_body", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="reserved"),
            sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("external_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("operation_id", "candidate_id", name="uq_assistant_request_dispatch_candidate"),
        )
    if "ix_assistant_request_dispatch_operation" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("assistant_request_dispatches")
    }:
        op.create_index(
            "ix_assistant_request_dispatch_operation",
            "assistant_request_dispatches",
            ["operation_id", "status"],
        )
    if "ix_assistant_request_dispatches_status" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("assistant_request_dispatches")
    }:
        op.create_index(
            op.f("ix_assistant_request_dispatches_status"),
            "assistant_request_dispatches",
            ["status"],
        )


def downgrade() -> None:
    op.drop_index(op.f("ix_assistant_request_dispatches_status"), table_name="assistant_request_dispatches")
    op.drop_index("ix_assistant_request_dispatch_operation", table_name="assistant_request_dispatches")
    op.drop_table("assistant_request_dispatches")
