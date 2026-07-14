"""SQLAlchemy models — schema v1 per the architecture doc §3."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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


class Collection(Base):
    """A curated-row definition, combining a build mode, an audience, and a recipe.

    The default ``picked`` collection is seeded on migration and reproduces today's single
    per-user "Picked for You" row, so an upgrade changes nothing until the owner adds more.
    """

    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    build: Mapped[str] = mapped_column(String(16), default="per_person")  # per_person | shared
    audience: Mapped[str] = mapped_column(String(16), default="everyone")  # everyone | subset
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    size: Mapped[int] = mapped_column(Integer, default=15)
    media: Mapped[str] = mapped_column(String(16), default="both")  # movie | show | both
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    name_template: Mapped[str] = mapped_column(String(255), default="")  # per_person display name
    source: Mapped[str] = mapped_column(String(16), default="all_users")  # shared: all_users | opted_in
    min_watchers: Mapped[int] = mapped_column(Integer, default=2)  # shared: aggregate-privacy threshold
    prompt: Mapped[dict] = mapped_column(JSON, default=dict)  # PromptConfig recipe
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CollectionAudience(Base):
    """Who a subset-audience collection is built for / visible to. Empty for audience='everyone'."""

    __tablename__ = "collection_audience"

    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class CollectionUserOverride(Base):
    """One person's tweaks to one row: mute it for them, resize it, or restyle its curation.

    Absence of a row means "use the collection's own settings". A row a person is not in the
    audience of has no override and is simply never built for them.
    """

    __tablename__ = "collection_user_overrides"

    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)  # this person doesn't get this row
    row_size: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None -> the row's own size
    prompt: Mapped[dict] = mapped_column(JSON, default=dict)  # PromptConfig override; {} -> the row's own
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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
    # A TMDB id is unique only within its namespace, so the pair (tmdb_id, media_type) is what
    # identifies a title — the staleness guard reads these back and would otherwise let a movie
    # suppress the show that shares its number.
    media_type: Mapped[str] = mapped_column(String(16))  # no default: a forgotten one is the bug
    rating_key: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int] = mapped_column(Integer)
    # Which row this pick belongs to (Collection.slug). Blank on pre-0004 rows and legacy single-row
    # runs; the user page groups a person's picks by this so each row shows its own titles.
    collection_slug: Mapped[str] = mapped_column(String(255), default="", index=True)
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


class RequestCandidate(Base):
    """A wanted-but-missing title in the approval inbox: surfaced by a run, awaiting the owner's call.

    One row per (tmdb_id, media_type): a title re-surfaced by a later run refreshes its demand and
    rating in place rather than duplicating. ``status`` is pending (waiting on the owner), sent (asked
    of Sonarr/Radarr), or rejected (dismissed — never re-queued, so a "no" can't nag every night).
    """

    __tablename__ = "request_candidates"
    __table_args__ = (UniqueConstraint("tmdb_id", "media_type", name="uq_request_candidate_title"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tmdb_id: Mapped[int] = mapped_column(Integer, index=True)
    media_type: Mapped[str] = mapped_column(String(16))  # movie | show
    title: Mapped[str] = mapped_column(String(512))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[float] = mapped_column(Float, default=0.0)  # on the chosen source (TMDB, or IMDb)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)  # vote count on that same source
    demand: Mapped[int] = mapped_column(Integer, default=1)  # distinct users whose picks wanted it
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | sent | rejected
    detail: Mapped[str] = mapped_column(String(512), default="")  # send outcome, or why it's queued
    first_seen_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # which run first surfaced it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
