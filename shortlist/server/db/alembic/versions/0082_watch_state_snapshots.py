"""`watch_state_snapshots` — what an account's watch history looked like before a transfer.

The watching-account transfer used to only ADD. It now mirrors, which means it un-marks anything the
source has not watched — the only path in Shortlist that can remove someone's watch history. Mirroring
is what makes the result a replica, and what repairs an account the old show-key transfer spoiled
(1,098 episodes marked from one write), but it also means an account that had watches of its own can
lose them.

Rule 2 covers exactly this: snapshot before the first mutation, restore from the snapshot on undo.
The old transfer took no snapshot and had no undo at all, while writing ~1,600 changes.

Counts and offsets are stored, not just watched/unwatched. A restore that re-marked a rewatched film
once, or re-marked a part-watched episode as finished, would leave a third state that existed on
neither account — a failure that looks like a success.

Revision ID: 0082
Revises: 0081
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "watch_state_snapshots" in inspector.get_table_names():
        # COLUMN-aware, not just table-aware. `complete` was added to this migration after an earlier
        # version of it had already run — and a database stamped 0082 never replays it, so a
        # table-level guard would leave that column missing for ever and every snapshot insert and
        # every `/watching-account/snapshots` read would raise `no such column`. It fails loudly
        # rather than losing data, but it kills the feature on exactly the machine it was developed
        # against, which is the shape 0032 is remembered for.
        if "complete" not in {c["name"] for c in inspector.get_columns("watch_state_snapshots")}:
            op.add_column(
                "watch_state_snapshots",
                sa.Column("complete", sa.Boolean, nullable=False, server_default=sa.true()),
            )
        return
    op.create_table(
        "watch_state_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        # RESTRICT, matching `restriction_snapshots`: there is no second copy of this anywhere, and
        # Plex keeps no history of what a viewCount used to be.
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Integer, nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        # Did the read behind this snapshot see every library? A snapshot taken from a partial read
        # describes less than the account held, and the restore is a MIRROR of it — so it would
        # un-mark every watch the snapshot never recorded. `undo_transfer` refuses when this is false
        # rather than discovering the gap by deleting.
        sa.Column("complete", sa.Boolean, nullable=False, server_default=sa.true()),
        # [[rating_key, view_count, view_offset_ms, media_type, show_rating_key], ...] — read whole or
        # not at all, so a compact list rather than a row per leaf. A heavy account is ~11,000 entries.
        # Rows written before the fifth element existed carry four; the restore handles both.
        sa.Column("state", sa.JSON, nullable=False),
    )
    op.create_index("ix_watch_state_snapshots_user_id", "watch_state_snapshots", ["user_id"])
    op.create_index("ix_watch_state_snapshots_job_id", "watch_state_snapshots", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_watch_state_snapshots_job_id", table_name="watch_state_snapshots")
    op.drop_index("ix_watch_state_snapshots_user_id", table_name="watch_state_snapshots")
    op.drop_table("watch_state_snapshots")
