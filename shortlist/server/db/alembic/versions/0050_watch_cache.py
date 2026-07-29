"""Cache the watched set, so it stops being re-read in full every night

The watched set drives every recommendation and the dashboard's hit rate, and it was read COMPLETE —
per user, per library, 500 titles a page with full metadata and GUIDs — on the nightly sync and again
inside every run. On a 40-user server that is hundreds of large XML responses a night for a set that
changes by a handful of items.

`watched_titles` holds the set; `watch_sync_state` holds how far each (person, library) has been read.
The nightly sync then asks the PMS only for `lastViewedAt >= cursor`.

The cache is NOT the source of truth. An incremental read cannot see an un-watch, a deletion, or an
item whose `lastViewedAt` never moved, so `last_full_at` drives a periodic complete re-read. Empty
tables mean "never read", which forces a full read — so this migration needs no backfill and the
first sync after it behaves exactly as before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "watched_titles" not in tables:
        op.create_table(
            "watched_titles",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("section_key", sa.String(length=64), nullable=False),
            sa.Column("rating_key", sa.Integer(), nullable=False),
            sa.Column("tmdb_id", sa.Integer(), nullable=True),
            sa.Column("media_type", sa.String(length=16), nullable=False),
            sa.Column("title", sa.String(length=512), nullable=True),
            sa.Column("year", sa.Integer(), nullable=True),
            sa.Column("watch_count", sa.Integer(), nullable=True),
            sa.Column("viewed_leaf_count", sa.Integer(), nullable=True),
            sa.Column("leaf_count", sa.Integer(), nullable=True),
            sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "section_key", "rating_key", name="uq_watched_title"),
        )
        op.create_index(op.f("ix_watched_titles_user_id"), "watched_titles", ["user_id"], unique=False)
        op.create_index(op.f("ix_watched_titles_tmdb_id"), "watched_titles", ["tmdb_id"], unique=False)
        op.create_index("ix_watched_titles_user_viewed", "watched_titles", ["user_id", "viewed_at"], unique=False)

    if "watch_sync_state" not in tables:
        op.create_table(
            "watch_sync_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("section_key", sa.String(length=64), nullable=False),
            sa.Column("cursor_viewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_full_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_incremental_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("item_count", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("user_id", "section_key", name="uq_watch_sync_state"),
        )
        op.create_index(op.f("ix_watch_sync_state_user_id"), "watch_sync_state", ["user_id"], unique=False)


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "watch_sync_state" in tables:
        op.drop_index(op.f("ix_watch_sync_state_user_id"), table_name="watch_sync_state")
        op.drop_table("watch_sync_state")
    if "watched_titles" in tables:
        op.drop_index("ix_watched_titles_user_viewed", table_name="watched_titles")
        op.drop_index(op.f("ix_watched_titles_tmdb_id"), table_name="watched_titles")
        op.drop_index(op.f("ix_watched_titles_user_id"), table_name="watched_titles")
        op.drop_table("watched_titles")
