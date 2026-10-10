"""Durable assistant proposals and receipts; execution uses the existing jobs table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from shortlist.server.db.models import Base, utcnow


class AssistantChange(Base):
    """An expiring exact intent; approval cannot survive changes to its digest or grant."""

    __tablename__ = "assistant_changes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_account_id: Mapped[int] = mapped_column(Integer)
    client_id: Mapped[str] = mapped_column(String(255))
    grant_revision: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    intent: Mapped[dict] = mapped_column(JSON)
    dependencies: Mapped[dict] = mapped_column(JSON)
    requirements: Mapped[dict] = mapped_column(JSON)
    effects: Mapped[list] = mapped_column(JSON)
    summary: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    approved_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approval_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)


class AssistantOperation(Base):
    """A durable receipt retained independently of ephemeral OAuth credentials."""

    __tablename__ = "assistant_operations"
    __table_args__ = (
        UniqueConstraint("grant_id", "client_id", "idempotency_key", name="uq_assistant_operation_idempotency"),
        Index("ix_assistant_operations_grant_created", "grant_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(64))
    owner_account_id: Mapped[int] = mapped_column(Integer)
    client_id: Mapped[str] = mapped_column(String(255))
    change_id: Mapped[str] = mapped_column(String(64), unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="configuration_saved")
    authorization_basis: Mapped[str] = mapped_column(String(32))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    job_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantOperationKey(Base):
    """Bind every retry key, including aliases used to replay an already consumed plan."""

    __tablename__ = "assistant_operation_keys"

    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(64), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssistantRequestDispatch(Base):
    """One acquisition attempt with a durable before-call uncertainty boundary."""

    __tablename__ = "assistant_request_dispatches"
    __table_args__ = (
        UniqueConstraint("operation_id", "candidate_id", name="uq_assistant_request_dispatch_candidate"),
        Index("ix_assistant_request_dispatch_operation", "operation_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("assistant_operations.id", ondelete="SET NULL"), nullable=True
    )
    origin: Mapped[str] = mapped_column(String(16), default="assistant", server_default="assistant")
    candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination: Mapped[str] = mapped_column(String(2048), nullable=False)
    request_body: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="reserved", index=True)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    external_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantRunCall(Base):
    """One admitted outbound provider call; uncertain calls still consume the reservation."""

    __tablename__ = "assistant_run_calls"
    __table_args__ = (Index("ix_assistant_run_calls_run_status", "run_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer)
    operation_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    destination: Mapped[str] = mapped_column(String(2048))
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    native_tool_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="external_started")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
