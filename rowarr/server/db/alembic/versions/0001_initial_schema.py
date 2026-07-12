"""initial schema v1

Revision ID: 0001
Revises:
Create Date: 2026-07-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "server",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("machine_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("url", sa.String(512), nullable=False),
        sa.Column("token_enc", sa.Text(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("owner_account_id", sa.Integer(), nullable=True),
        sa.Column("plex_pass", sa.Boolean(), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plex_account_id", sa.Integer(), nullable=False, unique=True, index=True),
        sa.Column("username", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True, index=True),
        sa.Column("avatar_url", sa.String(512), nullable=False),
        sa.Column("user_type", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cold_start", sa.Boolean(), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("prefs", sa.JSON(), nullable=False),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("stats", sa.JSON(), nullable=False),
    )
    op.create_table(
        "run_users",
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("llm_tokens", sa.Integer(), nullable=False),
        sa.Column("diff", sa.JSON(), nullable=False),
    )
    op.create_table(
        "picks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column("rating_key", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("seed_tmdb_id", sa.Integer(), nullable=True),
        sa.Column("seed_title", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("watched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "restriction_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("filters_before", sa.JSON(), nullable=False),
        sa.Column("filters_after", sa.JSON(), nullable=False),
    )
    op.create_table(
        "privacy_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tier", sa.String(8), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_table(
        "caches",
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("key", sa.String(512), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("level", sa.String(8), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False, index=True),
        sa.Column("message", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "events",
        "caches",
        "privacy_checks",
        "restriction_snapshots",
        "picks",
        "run_users",
        "runs",
        "users",
        "server",
        "settings",
    ):
        op.drop_table(table)
