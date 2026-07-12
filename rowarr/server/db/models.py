"""SQLAlchemy models — schema v1 per the architecture doc §3."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None) -> str | None:
    """Serialize a DB datetime with an explicit UTC offset.

    SQLite hands timezone-aware columns back as naive datetimes; without re-attaching UTC,
    `isoformat()` has no offset and browsers parse it as local time — shifting the audit
    trail by the viewer's UTC offset.
    """
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()


class Base(DeclarativeBase):
    type_annotation_map: ClassVar = {dict: JSON, list: JSON}


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Server(Base):
    __tablename__ = "server"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    machine_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str] = mapped_column(String(512))
    token_enc: Mapped[str] = mapped_column(Text)  # Fernet-encrypted; never stored in the clear
    version: Mapped[str] = mapped_column(String(64), default="")
    owner_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plex_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plex_account_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    avatar_url: Mapped[str] = mapped_column(String(512), default="")
    user_type: Mapped[str] = mapped_column(String(16), default="shared")  # shared | managed | owner
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cold_start: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(255), default="")  # as stored by Plex (title-cased)
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)

    run_users: Mapped[list[RunUser]] = relationship(back_populates="user")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(16))  # schedule | manual | wizard
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued | running | ok | error | aborted
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)

    users: Mapped[list[RunUser]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunUser(Base):
    __tablename__ = "run_users"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped[Run] = relationship(back_populates="users")
    user: Mapped[User] = relationship(back_populates="run_users")


class PickRow(Base):
    __tablename__ = "picks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    tmdb_id: Mapped[int] = mapped_column(Integer)
    rating_key: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(512), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    seed_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # hit-rate


class RestrictionSnapshotRow(Base):
    __tablename__ = "restriction_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(String(32), default="initial")  # initial | sync | uninstall_restore
    filters_before: Mapped[dict] = mapped_column(JSON, default=dict)
    filters_after: Mapped[dict] = mapped_column(JSON, default=dict)


class PrivacyCheck(Base):
    __tablename__ = "privacy_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    tier: Mapped[str] = mapped_column(String(8))
    passed: Mapped[bool] = mapped_column(Boolean)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class CacheRow(Base):
    __tablename__ = "caches"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)  # tmdb | library_index
    key: Mapped[str] = mapped_column(String(512), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[float] = mapped_column(Float)  # unix timestamp


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    scope: Mapped[str] = mapped_column(String(64), index=True)  # e.g. run.user, privacy.sync, collection
    message: Mapped[dict] = mapped_column(JSON, default=dict)  # structured diff/audit payload
