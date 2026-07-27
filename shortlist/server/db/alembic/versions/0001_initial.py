"""initial schema

The single baseline migration. The 28 incremental migrations that built the schema up to here were
squashed into this one before the first release (nothing was released, so no upgrade path from them is
needed). A fresh install runs just this; a pre-release DB stamped at one of the (now-removed) revisions
is re-stamped to `0001` in place by ``run_migrations`` — its schema already matches. See
tests/unit/test_migration_initial.py, which guards that this migration and the models stay in lockstep.
Post-release schema changes add normal incremental migrations after this.

Idempotent by design (project rule): SQLite auto-commits DDL, so a crash mid-migration can leave a
half-built schema. Every ``create_table`` is guarded by existence and the default-row seed is guarded
by a count, so a re-run FINISHES the job rather than failing on "table already exists".
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    def new(name: str) -> bool:
        return name not in existing

    if new("caches"):
        op.create_table(
            "caches",
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column("key", sa.String(length=512), nullable=False),
            sa.Column("value", sa.JSON(), nullable=False),
            sa.Column("expires_at", sa.Float(), nullable=False),
            sa.PrimaryKeyConstraint("kind", "key"),
        )
    if new("collections"):
        op.create_table(
            "collections",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("build", sa.String(length=16), nullable=False),
            sa.Column("audience", sa.String(length=16), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("schedule", sa.String(length=64), nullable=False),
            sa.Column("size", sa.Integer(), nullable=False),
            sa.Column("media", sa.String(length=16), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("name_template", sa.String(length=255), nullable=False),
            sa.Column("candidate_sources", sa.JSON(), nullable=False),
            sa.Column("watched_pct", sa.Float(), nullable=True),
            sa.Column("freshness", sa.Float(), nullable=True),
            sa.Column("recent_count", sa.Integer(), nullable=True),
            sa.Column("library_keys", sa.JSON(), nullable=False),
            sa.Column("min_watchers", sa.Integer(), nullable=False),
            sa.Column("request_tag", sa.String(length=64), nullable=False),
            sa.Column("placement", sa.String(length=16), nullable=False),
            sa.Column("placement_friends", sa.String(length=16), nullable=False, server_default="both"),
            sa.Column("pin_top", sa.Boolean(), nullable=False),
            sa.Column("hub_anchor", sa.JSON(), nullable=False),
            sa.Column("prompt", sa.JSON(), nullable=False),
            sa.Column("poster", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_collections_slug"), "collections", ["slug"], unique=True)
    if new("events"):
        op.create_table(
            "events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
            sa.Column("level", sa.String(length=8), nullable=False),
            sa.Column("scope", sa.String(length=64), nullable=False),
            sa.Column("message", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_events_scope"), "events", ["scope"], unique=False)
        op.create_index(op.f("ix_events_ts"), "events", ["ts"], unique=False)
    if new("poster_assets"):
        op.create_table(
            "poster_assets",
            sa.Column("key", sa.String(length=80), nullable=False),
            sa.Column("image", sa.LargeBinary(), nullable=False),
            sa.Column("content_type", sa.String(length=64), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )
    if new("request_candidates"):
        op.create_table(
            "request_candidates",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=False),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("year", sa.Integer(), nullable=True),
            sa.Column("imdb_id", sa.String(length=16), nullable=False),
            sa.Column("rating", sa.Float(), nullable=False),
            sa.Column("vote_count", sa.Integer(), nullable=False),
            sa.Column("demand", sa.Integer(), nullable=False),
            sa.Column("tags", sa.JSON(), nullable=False),
            sa.Column("wanters", sa.JSON(), nullable=False),
            sa.Column("why", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("detail", sa.String(length=512), nullable=False),
            sa.Column("arr_slug", sa.String(length=256), nullable=True),
            sa.Column("excluded", sa.Boolean(), nullable=False),
            sa.Column("hidden", sa.Boolean(), server_default="0", nullable=False),
            sa.Column("first_seen_run_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("tmdb_id", "media_type", name="uq_request_candidate_title"),
        )
        op.create_index(op.f("ix_request_candidates_status"), "request_candidates", ["status"], unique=False)
        op.create_index(op.f("ix_request_candidates_tmdb_id"), "request_candidates", ["tmdb_id"], unique=False)
    if new("runs"):
        op.create_table(
            "runs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("trigger", sa.String(length=16), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("dry_run", sa.Boolean(), nullable=False),
            sa.Column("stats", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if new("server"):
        op.create_table(
            "server",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("machine_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("url", sa.String(length=512), nullable=False),
            sa.Column("token_enc", sa.Text(), nullable=False),
            sa.Column("version", sa.String(length=64), nullable=False),
            sa.Column("owner_account_id", sa.Integer(), nullable=True),
            sa.Column("plex_pass", sa.Boolean(), nullable=False),
            sa.Column("capabilities", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("machine_id"),
        )
    if new("settings"):
        op.create_table(
            "settings",
            sa.Column("key", sa.String(length=128), nullable=False),
            sa.Column("value", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("key"),
        )
    if new("users"):
        op.create_table(
            "users",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("plex_account_id", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(length=255), nullable=False),
            sa.Column("slug", sa.String(length=255), nullable=False),
            sa.Column("avatar_url", sa.String(length=512), nullable=False),
            sa.Column("user_type", sa.String(length=16), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("cold_start", sa.Boolean(), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("request_tag", sa.String(length=64), nullable=False),
            sa.Column("prefs", sa.JSON(), nullable=False),
            sa.Column("watch_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_users_plex_account_id"), "users", ["plex_account_id"], unique=True)
        op.create_index(op.f("ix_users_slug"), "users", ["slug"], unique=True)
    if new("collection_audience"):
        op.create_table(
            "collection_audience",
            sa.Column("collection_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("collection_id", "user_id"),
        )
    if new("collection_user_overrides"):
        op.create_table(
            "collection_user_overrides",
            sa.Column("collection_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("muted", sa.Boolean(), nullable=False),
            sa.Column("row_size", sa.Integer(), nullable=True),
            sa.Column("prompt", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["collection_id"], ["collections.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("collection_id", "user_id"),
        )
    if new("picks"):
        op.create_table(
            "picks",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=False),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("rating_key", sa.Integer(), nullable=False),
            sa.Column("rank", sa.Integer(), nullable=False),
            sa.Column("collection_slug", sa.String(length=255), nullable=False),
            sa.Column("section_key", sa.String(length=64), nullable=False),
            sa.Column("library", sa.String(length=255), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=False),
            sa.Column("seed_tmdb_id", sa.Integer(), nullable=True),
            sa.Column("seed_title", sa.String(length=512), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("watched_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_picks_collection_slug"), "picks", ["collection_slug"], unique=False)
        op.create_index(op.f("ix_picks_run_id"), "picks", ["run_id"], unique=False)
        op.create_index(op.f("ix_picks_user_id"), "picks", ["user_id"], unique=False)
    if new("restriction_snapshots"):
        op.create_table(
            "restriction_snapshots",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reason", sa.String(length=32), nullable=False),
            sa.Column("filters_before", sa.JSON(), nullable=False),
            sa.Column("filters_after", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_restriction_snapshots_user_id"), "restriction_snapshots", ["user_id"], unique=False)
    if new("run_users"):
        op.create_table(
            "run_users",
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False),
            sa.Column("llm_tokens", sa.Integer(), nullable=False),
            sa.Column("llm_tokens_by_step", sa.JSON(), nullable=False),
            sa.Column("exa_searches", sa.Integer(), nullable=False),
            sa.Column("diff", sa.JSON(), nullable=False),
            sa.Column("breakdown", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["runs.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("run_id", "user_id"),
        )
    if new("watch_events"):
        op.create_table(
            "watch_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("rating_key", sa.Integer(), nullable=False),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=False),
            sa.Column("year", sa.Integer(), nullable=True),
            sa.Column("watched_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completion", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "rating_key", "watched_at", name="uq_watch_event"),
        )
        op.create_index(op.f("ix_watch_events_user_id"), "watch_events", ["user_id"], unique=False)

    _seed_default_row(bind)


def _seed_default_row(bind: sa.engine.Connection) -> None:
    """Seed the single default 'picked' row so a fresh install has a working "Picked for You" out of
    the box. Idempotent (only when no collection exists), so a crash re-run never duplicates it and a
    server that already has rows is left alone. Name/size follow Settings > Defaults at run time."""
    if bind.execute(sa.text("SELECT COUNT(*) FROM collections")).scalar():
        return
    now = datetime.now(UTC).isoformat()
    bind.execute(
        sa.text(
            "INSERT INTO collections "
            "(slug, name, build, audience, enabled, schedule, size, media, sort_order, name_template, "
            " candidate_sources, watched_pct, freshness, recent_count, library_keys, min_watchers, "
            " request_tag, placement, placement_friends, pin_top, hub_anchor, prompt, poster, created_at, updated_at) "
            "VALUES "
            "('picked', :name, 'per_person', 'everyone', 1, '', 15, 'both', 0, '', "
            " '[]', NULL, NULL, NULL, '[]', 2, '', 'both', 'both', 0, '{}', '{}', '{}', :now, :now)"
        ),
        {"name": "✨ Picked for You", "now": now},
    )


def downgrade() -> None:
    for table in (
        "watch_events",
        "run_users",
        "restriction_snapshots",
        "picks",
        "collection_user_overrides",
        "collection_audience",
        "users",
        "settings",
        "server",
        "runs",
        "request_candidates",
        "poster_assets",
        "events",
        "collections",
        "caches",
    ):
        op.drop_table(table)
