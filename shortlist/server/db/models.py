"""SQLAlchemy models — schema v1 per the architecture doc §3."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
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


# The seeded "Picked for You" row (migration 0003). It is the one row whose name, size and curation
# style come from the global Settings rather than its own columns, so that the wizard and Settings
# stay the single place to change them. Every module that special-cases it uses this constant.
DEFAULT_SLUG = "picked"


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
    # What to call this person in a row title. `nickname` is the owner's own override and always
    # wins; `friendly_name` is whatever Tautulli knows them as, refreshed on each user sync. Neither
    # touches `slug`, so the `shortlist_<slug>` label every share filter excludes never moves —
    # renaming someone is cosmetic by construction and can't strand their privacy exclusions.
    nickname: Mapped[str] = mapped_column(String(255), default="")
    friendly_name: Mapped[str] = mapped_column(String(255), default="")
    user_type: Mapped[str] = mapped_column(String(16), default="shared")  # shared | managed | owner
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    cold_start: Mapped[bool] = mapped_column(Boolean, default=False)
    label: Mapped[str] = mapped_column(String(255), default="")  # as stored by Plex (title-cased)
    request_tag: Mapped[str] = mapped_column(String(64), default="")  # tag added to titles requested for them
    prefs: Mapped[dict] = mapped_column(JSON, default=dict)
    # High-water mark for the incremental watch-history sync: only plays newer than this are pulled
    # each run (NULL = never synced -> full backfill). See WatchEvent + WatchHistorySync.
    watch_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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
    # This row's own run schedule as a 5-field cron string; "" = never runs on a schedule (only
    # manual "run now"). There is NO global schedule — each row runs on its own cron, or not at all.
    schedule: Mapped[str] = mapped_column(String(64), default="")
    size: Mapped[int] = mapped_column(Integer, default=15)
    media: Mapped[str] = mapped_column(String(16), default="both")  # movie | show | both
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    name_template: Mapped[str] = mapped_column(String(255), default="")  # per_person display name
    # Per-row override of which discovery sources feed this row; [] -> inherit global candidates.sources.
    candidate_sources: Mapped[list] = mapped_column(JSON, default=list)
    # Per-row cap on already-finished titles, as a fraction (0.0 all fresh .. 1.0 no filtering).
    # NULL -> inherit the global recommendations.watched_pct.
    watched_pct: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # Per-row day-to-day variability, as a fraction (0.0 stable .. 1.0 fresh). NULL -> inherit the
    # global recommendations.freshness.
    freshness: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    # How many of a person's most recent watches the web-search source searches for this row (one
    # cached search each). NULL -> inherit the global recommendations.recent_count.
    recent_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    # Specific Plex library section keys this row builds in; [] -> every library of its media type.
    library_keys: Mapped[list] = mapped_column(JSON, default=list)
    min_watchers: Mapped[int] = mapped_column(Integer, default=2)  # shared: aggregate-privacy threshold
    request_tag: Mapped[str] = mapped_column(String(64), default="")  # tag added to titles requested via this row
    # Where the row shows once promoted: "both" (Home + Library Recommended), "home", or "library".
    placement: Mapped[str] = mapped_column(String(16), default="both")
    # Pin the row to the TOP of its library's Recommended shelf (server-wide order, not per-user).
    pin_top: Mapped[bool] = mapped_column(Boolean, default=False)
    # Per-library override of where THIS row sits in the Recommended shelf: {sectionKey: {anchor, before}}.
    # {} -> inherit the global default (settings `rows.hub_anchor`). A library absent here inherits too.
    hub_anchor: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt: Mapped[dict] = mapped_column(JSON, default=dict)  # PromptConfig recipe
    # Custom collection poster for this row. {} -> Plex's own artwork. Shape:
    # {"mode": "upload"|"generate", "title", "subtitle", "style"}. No image bytes live here — an
    # uploaded/generated image is stored in the `poster_assets` table, keyed by collection id / prompt.
    poster: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CollectionAudience(Base):
    """Who a subset-audience collection is built for / visible to. Empty for audience='everyone'."""

    __tablename__ = "collection_audience"

    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)


class PosterAsset(Base):
    """Binary image storage for row posters: uploaded originals and cached generated images.

    Kept in the DB (which lives under /config) rather than on the filesystem so a poster survives a
    container recreate and travels with a config backup. ``key`` namespaces the two kinds:
    ``upload:<collection_id>`` for a user's uploaded image, ``gen:<prompt_hash>`` for a generated one
    (so an identical prompt across users/runs is generated once, not every night per person)."""

    __tablename__ = "poster_assets"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    image: Mapped[bytes] = mapped_column(LargeBinary)
    content_type: Mapped[str] = mapped_column(String(64), default="image/png")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


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
    # Why a non-failing outcome happened (a `skipped` row that could not build). NOT an error: the
    # UI counts every non-null `error` as a failed user, which is how "skipped" ended up on screen
    # with no explanation at all (issue #3). NULL on legacy rows.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_tokens: Mapped[int] = mapped_column(Integer, default=0)
    # `llm_tokens` split by WHERE it went: {"curate": N, "llm_web": M, "llm_library": P}. {} on legacy
    # rows. Exa is counted apart from tokens — it bills per search request, not per token.
    llm_tokens_by_step: Mapped[dict] = mapped_column(JSON, default=dict)
    exa_searches: Mapped[int] = mapped_column(Integer, default=0)
    diff: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-(row, library) delivery breakdown for the run detail UI; [] on legacy rows (falls back to
    # the merged `diff` + `picks`). Each entry: row_slug/row_title, library_key/library_title,
    # added/removed/kept/deleted, created, and that library's own picks.
    breakdown: Mapped[list] = mapped_column(JSON, default=list)

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
    # The library this pick was delivered into: `section_key` is the stable Plex section key,
    # `library` its display name ("Movies"). A row spanning >1 library is one Plex collection PER
    # library, so the effectiveness report splits it into one line per library. Blank on pre-0020 rows.
    section_key: Mapped[str] = mapped_column(String(64), default="")
    library: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    seed_tmdb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # hit-rate


class WatchEvent(Base):
    """One play event from a user's Plex/Tautulli watch history — the local mirror that powers the
    already-watched filter.

    Plex's history API only returns the most recent ~200 plays per call, so a heavy watcher's older
    watches were invisible to the filter and got recommended again (SFLIX/MooHouse, 2026-07-20). We
    instead sync the FULL history incrementally into this table (per-user high-water mark on
    ``User.watch_synced_at``) and read the complete set at run time. One row PER play event (not per
    title) so the finished-show fraction can still count episode plays; the unique constraint dedups
    the overlap window between incremental syncs.
    """

    __tablename__ = "watch_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    rating_key: Mapped[int] = mapped_column(
        Integer
    )  # PMS ratingKey (grandparent for episodes) -> resolved to tmdb in-engine
    media_type: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(512), default="")
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completion: Mapped[float] = mapped_column(Float, default=1.0)  # 0..1; 1.0 for presence-only (Plex history)

    __table_args__ = (UniqueConstraint("user_id", "rating_key", "watched_at", name="uq_watch_event"),)


class RestrictionSnapshotRow(Base):
    __tablename__ = "restriction_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(String(32), default="initial")  # initial | sync | uninstall_restore
    filters_before: Mapped[dict] = mapped_column(JSON, default=dict)
    filters_after: Mapped[dict] = mapped_column(JSON, default=dict)


class CacheRow(Base):
    __tablename__ = "caches"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)  # tmdb | trakt | library_index
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
    imdb_id: Mapped[str] = mapped_column(String(16), default="")  # "tt…" -> inbox deep-links to IMDb
    rating: Mapped[float] = mapped_column(Float, default=0.0)  # on the chosen source (TMDB, or IMDb)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)  # vote count on that same source
    demand: Mapped[int] = mapped_column(Integer, default=1)  # distinct users whose picks wanted it
    tags: Mapped[list] = mapped_column(JSON, default=list)  # per-user/per-row tags to apply when sent
    wanters: Mapped[list] = mapped_column(JSON, default=list)  # usernames whose picks wanted it (the "who")
    # Full provenance: [{user, row, seed, source}] — which person, in which row, and why (the seed
    # "because you watched …") each request got here. Richer than `wanters`; drives the inbox detail.
    why: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | sent | rejected
    detail: Mapped[str] = mapped_column(String(512), default="")  # send outcome, or why it's queued
    # The arr's titleSlug, captured when the title is sent, so the inbox deep-links straight to its
    # Sonarr/Radarr page (Sonarr has only `/series/<slug>`, no id URL). None for titles queued/sent
    # before this was recorded — the inbox falls back to the arr's home page for those.
    arr_slug: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # On Sonarr/Radarr's import-exclusion list (usually from a past delete): surfaced in the inbox so
    # the owner knows approving it is a no-op until they remove the exclusion in the Arr.
    excluded: Mapped[bool] = mapped_column(Boolean, default=False)
    # Owner cleared this from the Sent log. The row STAYS `status="sent"` — a load-bearing tombstone
    # that stops a still-downloading title being re-requested (see delete_requests / _persist_request_queue)
    # — so we hide it from the UI instead of deleting it. Excluded from the inbox list; engine unaffected.
    hidden: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    first_seen_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # which run first surfaced it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
