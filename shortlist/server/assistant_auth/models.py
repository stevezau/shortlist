"""Persistence models for named assistant grants and OAuth credentials."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shortlist.server.db.models import Base, utcnow


class AssistantGrant(Base):
    """Owner-approved authority for one named assistant connection."""

    __tablename__ = "assistant_grants"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_account_id: Mapped[int] = mapped_column(Integer, index=True)
    client_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(255))
    preset: Mapped[str] = mapped_column(String(32))
    capabilities: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    constraints: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantLocalCredential(Base):
    """Hashed local bearer credential; the plaintext is never persisted."""

    __tablename__ = "assistant_local_credentials"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    grant_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_grants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantOAuthClient(Base):
    """Pre-registered or compatibility-registered OAuth client metadata."""

    __tablename__ = "assistant_oauth_clients"

    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    client_name: Mapped[str] = mapped_column(String(255))
    redirect_uris: Mapped[list] = mapped_column(JSON, default=list, server_default="[]")
    grant_types: Mapped[list] = mapped_column(
        JSON,
        default=lambda: ["authorization_code", "refresh_token"],
        server_default='["authorization_code", "refresh_token"]',
    )
    response_types: Mapped[list] = mapped_column(JSON, default=lambda: ["code"], server_default='["code"]')
    token_endpoint_auth_method: Mapped[str] = mapped_column(String(32), default="none", server_default="none")
    client_secret_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def get_client_id(self) -> str:
        return self.client_id

    def get_default_redirect_uri(self) -> str | None:
        return self.redirect_uris[0] if self.redirect_uris else None

    def get_allowed_scope(self, scope: str) -> str:
        return scope

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        return redirect_uri in self.redirect_uris

    def check_client_secret(self, client_secret: str) -> bool:
        return False

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        return method == self.token_endpoint_auth_method

    def check_response_type(self, response_type: str) -> bool:
        return response_type in self.response_types

    def check_grant_type(self, grant_type: str) -> bool:
        return grant_type in self.grant_types


class AssistantOAuthCode(Base):
    """One-use authorization code bound to client, redirect, resource, and PKCE."""

    __tablename__ = "assistant_oauth_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    grant_id: Mapped[str] = mapped_column(ForeignKey("assistant_grants.id", ondelete="CASCADE"), index=True)
    owner_account_id: Mapped[int] = mapped_column(Integer)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_oauth_clients.client_id", ondelete="CASCADE"), index=True
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    resource: Mapped[str] = mapped_column(String(2048))
    scope: Mapped[str] = mapped_column(String(2048))
    code_challenge: Mapped[str] = mapped_column(String(128))
    code_challenge_method: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def get_redirect_uri(self) -> str:
        return self.redirect_uri

    def get_scope(self) -> str:
        return self.scope


class AssistantOAuthToken(Base):
    """One access/refresh pair in a rotating refresh-token family."""

    __tablename__ = "assistant_oauth_tokens"
    __table_args__ = (
        Index("ix_assistant_oauth_token_family", "refresh_family_id", "issued_at"),
        UniqueConstraint("previous_token_id", name="uq_assistant_oauth_token_previous"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grant_id: Mapped[str] = mapped_column(ForeignKey("assistant_grants.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_oauth_clients.client_id", ondelete="CASCADE"), index=True
    )
    issuer: Mapped[str] = mapped_column(String(2048))
    resource: Mapped[str] = mapped_column(String(2048))
    scope: Mapped[str] = mapped_column(String(2048))
    access_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    access_prefix: Mapped[str] = mapped_column(String(16))
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    access_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    refresh_prefix: Mapped[str] = mapped_column(String(16))
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_family_id: Mapped[str] = mapped_column(String(40), index=True)
    previous_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("assistant_oauth_tokens.id", ondelete="SET NULL"), nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def check_client(self, client: AssistantOAuthClient) -> bool:
        return self.client_id == client.client_id

    def get_scope(self) -> str:
        return self.scope


class AssistantConsentFlow(Base):
    """Short-lived browser consent state, including server-side CSRF binding."""

    __tablename__ = "assistant_consent_flows"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_account_id: Mapped[int] = mapped_column(Integer, index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("assistant_oauth_clients.client_id", ondelete="CASCADE"))
    grant_id: Mapped[str | None] = mapped_column(ForeignKey("assistant_grants.id", ondelete="CASCADE"), nullable=True)
    redirect_uri: Mapped[str] = mapped_column(String(2048))
    resource: Mapped[str] = mapped_column(String(2048))
    scope: Mapped[str] = mapped_column(String(2048))
    client_state: Mapped[str] = mapped_column(String(2048))
    code_challenge: Mapped[str] = mapped_column(String(128))
    code_challenge_method: Mapped[str] = mapped_column(String(8))
    csrf_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantOAuthRevokedFamily(Base):
    """Durable revocation even when a successor is being issued or old tokens are removed."""

    __tablename__ = "assistant_oauth_revoked_families"

    family_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    revoked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
