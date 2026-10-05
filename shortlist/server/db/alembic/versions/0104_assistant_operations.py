"""Durable assistant proposals, exact approvals, receipts and transaction-owned job intents.

Revision ID: 0104
Revises: 0103
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create durable automation records without rewriting existing configuration or jobs."""
    if not sa.inspect(op.get_bind()).has_table("assistant_changes"):
        op.create_table(
            "assistant_changes",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("grant_id", sa.String(64), nullable=False),
            sa.Column("owner_account_id", sa.Integer, nullable=False),
            sa.Column("client_id", sa.String(255), nullable=False),
            sa.Column("grant_revision", sa.Integer, nullable=False),
            sa.Column("kind", sa.String(64), nullable=False),
            sa.Column("schema_version", sa.Integer, nullable=False),
            sa.Column("intent", sa.JSON, nullable=False),
            sa.Column("dependencies", sa.JSON, nullable=False),
            sa.Column("requirements", sa.JSON, nullable=False),
            sa.Column("effects", sa.JSON, nullable=False),
            sa.Column("summary", sa.JSON, nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("approved_by", sa.Integer, nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("approved_hash", sa.String(64), nullable=True),
            sa.Column("approval_consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("operation_id", sa.String(64), nullable=True, unique=True),
        )
    if "ix_assistant_changes_grant_id" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("assistant_changes")
    }:
        op.create_index("ix_assistant_changes_grant_id", "assistant_changes", ["grant_id"])
    if "ix_assistant_changes_expires_at" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("assistant_changes")
    }:
        op.create_index("ix_assistant_changes_expires_at", "assistant_changes", ["expires_at"])
    if not sa.inspect(op.get_bind()).has_table("assistant_operations"):
        op.create_table(
            "assistant_operations",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("grant_id", sa.String(64), nullable=False),
            sa.Column("owner_account_id", sa.Integer, nullable=False),
            sa.Column("client_id", sa.String(255), nullable=False),
            sa.Column("change_id", sa.String(64), nullable=False, unique=True),
            sa.Column("idempotency_key", sa.String(128), nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("authorization_basis", sa.String(32), nullable=False),
            sa.Column("result", sa.JSON, nullable=False),
            sa.Column("job_ids", sa.JSON, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("grant_id", "client_id", "idempotency_key", name="uq_assistant_operation_idempotency"),
        )
    if "ix_assistant_operations_grant_created" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("assistant_operations")
    }:
        op.create_index("ix_assistant_operations_grant_created", "assistant_operations", ["grant_id", "created_at"])
    if not sa.inspect(op.get_bind()).has_table("assistant_operation_keys"):
        op.create_table(
            "assistant_operation_keys",
            sa.Column("grant_id", sa.String(64), primary_key=True),
            sa.Column("client_id", sa.String(255), primary_key=True),
            sa.Column("key", sa.String(128), primary_key=True),
            sa.Column("operation_id", sa.String(64), nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "ix_assistant_operation_keys_operation_id" not in {
        item["name"] for item in sa.inspect(op.get_bind()).get_indexes("assistant_operation_keys")
    }:
        op.create_index("ix_assistant_operation_keys_operation_id", "assistant_operation_keys", ["operation_id"])
    if "operation_id" not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("jobs")}:
        op.add_column("jobs", sa.Column("operation_id", sa.String(64), nullable=True))
    if "effect_key" not in {item["name"] for item in sa.inspect(op.get_bind()).get_columns("jobs")}:
        op.add_column("jobs", sa.Column("effect_key", sa.String(128), nullable=True))
    if "uq_jobs_operation_effect" not in {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("jobs")}:
        op.create_index("uq_jobs_operation_effect", "jobs", ["operation_id", "effect_key"], unique=True)


def downgrade() -> None:
    """Drop only the assistant operation bookkeeping and optional job correlation."""
    op.drop_index("uq_jobs_operation_effect", table_name="jobs")
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("effect_key")
        batch.drop_column("operation_id")
    op.drop_table("assistant_operation_keys")
    op.drop_table("assistant_operations")
    op.drop_table("assistant_changes")
