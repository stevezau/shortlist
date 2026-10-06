"""Named assistant grants, hashed credentials, OAuth, consent, and bootstrap state.

Credential bodies are never stored. OAuth codes and tokens are HMAC digests,
and every issued credential references an independently revocable grant.

Revision ID: 0103
Revises: 0102
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create assistant identity tables without changing existing owner auth."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("assistant_grants"):
        op.create_table(
            "assistant_grants",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column("owner_account_id", sa.Integer, nullable=False),
            sa.Column("client_id", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("preset", sa.String(32), nullable=False),
            sa.Column("capabilities", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
            sa.Column("constraints", sa.JSON, nullable=False, server_default=sa.text("'{}'")),
            sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_assistant_grants_owner_account_id", "assistant_grants", ["owner_account_id"])
        op.create_index("ix_assistant_grants_client_id", "assistant_grants", ["client_id"])

    inspector = sa.inspect(bind)
    if not inspector.has_table("assistant_local_credentials"):
        op.create_table(
            "assistant_local_credentials",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column(
                "grant_id",
                sa.String(40),
                sa.ForeignKey("assistant_grants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
            sa.Column("token_prefix", sa.String(16), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_assistant_local_credentials_grant_id", "assistant_local_credentials", ["grant_id"])

    inspector = sa.inspect(bind)
    if not inspector.has_table("assistant_oauth_clients"):
        op.create_table(
            "assistant_oauth_clients",
            sa.Column("client_id", sa.String(255), primary_key=True),
            sa.Column("client_name", sa.String(255), nullable=False),
            sa.Column("redirect_uris", sa.JSON, nullable=False, server_default=sa.text("'[]'")),
            sa.Column(
                "grant_types",
                sa.JSON,
                nullable=False,
                server_default=sa.text('\'["authorization_code", "refresh_token"]\''),
            ),
            sa.Column("response_types", sa.JSON, nullable=False, server_default=sa.text("'[\"code\"]'")),
            sa.Column("token_endpoint_auth_method", sa.String(32), nullable=False, server_default="none"),
            sa.Column("client_secret_digest", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        )

    inspector = sa.inspect(bind)
    if not inspector.has_table("assistant_oauth_codes"):
        op.create_table(
            "assistant_oauth_codes",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("code_digest", sa.String(64), nullable=False, unique=True),
            sa.Column(
                "grant_id",
                sa.String(40),
                sa.ForeignKey("assistant_grants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("owner_account_id", sa.Integer, nullable=False),
            sa.Column(
                "client_id",
                sa.String(255),
                sa.ForeignKey("assistant_oauth_clients.client_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("redirect_uri", sa.String(2048), nullable=False),
            sa.Column("resource", sa.String(2048), nullable=False),
            sa.Column("scope", sa.String(2048), nullable=False),
            sa.Column("code_challenge", sa.String(128), nullable=False),
            sa.Column("code_challenge_method", sa.String(8), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_assistant_oauth_codes_grant_id", "assistant_oauth_codes", ["grant_id"])
        op.create_index("ix_assistant_oauth_codes_client_id", "assistant_oauth_codes", ["client_id"])

    inspector = sa.inspect(bind)
    if not inspector.has_table("assistant_oauth_tokens"):
        op.create_table(
            "assistant_oauth_tokens",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column(
                "grant_id",
                sa.String(40),
                sa.ForeignKey("assistant_grants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "client_id",
                sa.String(255),
                sa.ForeignKey("assistant_oauth_clients.client_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("issuer", sa.String(2048), nullable=False),
            sa.Column("resource", sa.String(2048), nullable=False),
            sa.Column("scope", sa.String(2048), nullable=False),
            sa.Column("access_digest", sa.String(64), nullable=False, unique=True),
            sa.Column("access_prefix", sa.String(16), nullable=False),
            sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("access_revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("refresh_digest", sa.String(64), nullable=False, unique=True),
            sa.Column("refresh_prefix", sa.String(16), nullable=False),
            sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("refresh_revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("refresh_family_id", sa.String(40), nullable=False),
            sa.Column(
                "previous_token_id",
                sa.Integer,
                sa.ForeignKey("assistant_oauth_tokens.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("previous_token_id", name="uq_assistant_oauth_token_previous"),
        )
        op.create_index("ix_assistant_oauth_tokens_grant_id", "assistant_oauth_tokens", ["grant_id"])
        op.create_index("ix_assistant_oauth_tokens_client_id", "assistant_oauth_tokens", ["client_id"])
        op.create_index("ix_assistant_oauth_token_family", "assistant_oauth_tokens", ["refresh_family_id", "issued_at"])

    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("assistant_oauth_tokens")}
    if "ix_assistant_oauth_tokens_refresh_family_id" not in indexes:
        op.create_index("ix_assistant_oauth_tokens_refresh_family_id", "assistant_oauth_tokens", ["refresh_family_id"])

    inspector = sa.inspect(bind)
    if not inspector.has_table("assistant_consent_flows"):
        op.create_table(
            "assistant_consent_flows",
            sa.Column("id", sa.String(40), primary_key=True),
            sa.Column("owner_account_id", sa.Integer, nullable=False),
            sa.Column(
                "client_id",
                sa.String(255),
                sa.ForeignKey("assistant_oauth_clients.client_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "grant_id",
                sa.String(40),
                sa.ForeignKey("assistant_grants.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("redirect_uri", sa.String(2048), nullable=False),
            sa.Column("resource", sa.String(2048), nullable=False),
            sa.Column("scope", sa.String(2048), nullable=False),
            sa.Column("client_state", sa.String(2048), nullable=False),
            sa.Column("code_challenge", sa.String(128), nullable=False),
            sa.Column("code_challenge_method", sa.String(8), nullable=False),
            sa.Column("csrf_digest", sa.String(64), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_assistant_consent_flows_owner_account_id", "assistant_consent_flows", ["owner_account_id"])

    inspector = sa.inspect(bind)
    if not inspector.has_table("assistant_bootstrap_flows"):
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


def downgrade() -> None:
    """Remove assistant auth data without touching legacy owner sessions or tokens."""
    bind = op.get_bind()
    for table in (
        "assistant_bootstrap_flows",
        "assistant_consent_flows",
        "assistant_oauth_tokens",
        "assistant_oauth_codes",
        "assistant_oauth_clients",
        "assistant_local_credentials",
        "assistant_grants",
    ):
        if sa.inspect(bind).has_table(table):
            op.drop_table(table)
