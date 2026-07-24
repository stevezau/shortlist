"""Drop the watch-history mirror — watched state is now read live, per user, from Plex

Watched state used to be MIRRORED into a local ``watch_events`` table, synced incrementally from
Plex's play-history API (per-user high-water mark on ``users.watch_synced_at``). Two problems made
that a dead end: the history API caps at ~200 plays per call, and it never returns a
mark-as-watched at all (issue #12) — so a heavy watcher's older titles, and everyone's marks, were
invisible to the already-watched filter.

The replacement reads each user's COMPLETE watched set live, straight from the PMS, using the
per-user server token plex.tv mints for every share (``ShareTokenWatchSource``). That set carries
their own ``viewCount``/``viewedLeafCount`` — marks included — so there is nothing to mirror and no
high-water mark to keep. This removes:

  * table ``watch_events`` — the local play mirror.
  * column ``users.watch_synced_at`` — the incremental sync high-water mark.
  * setting ``plex.db_path`` — the PMS-database mount path, only ever used by the (now removed)
    one-off "reconcile watched from DB" repair, which the share-token read makes unnecessary.

Idempotent: each drop is guarded on existence, so a re-run finds nothing to do. The ``watch_events``
drop pairs with 0001's create; ``op.drop_column`` on ``users`` matches the pattern 0030 established.
"""

import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "watch_events" in inspector.get_table_names():
        op.drop_table("watch_events")

    if "watch_synced_at" in {c["name"] for c in inspector.get_columns("users")}:
        op.drop_column("users", "watch_synced_at")

    # Absence reads as the app default (an empty path = off), so no downgrade re-seed is needed.
    bind.execute(sa.text("delete from settings where key = 'plex.db_path'"))


def downgrade() -> None:
    """Re-create the table + column exactly as 0001 did, so the pair round-trips (empty of data — the
    mirrored plays cannot be reconstructed, but the structure can)."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "watch_synced_at" not in {c["name"] for c in inspector.get_columns("users")}:
        op.add_column("users", sa.Column("watch_synced_at", sa.DateTime(timezone=True), nullable=True))

    if "watch_events" not in inspector.get_table_names():
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
