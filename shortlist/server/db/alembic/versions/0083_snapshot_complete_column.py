"""Add `watch_state_snapshots.complete` to a database that ran 0082 before the column existed.

0082 gained the column after an earlier version of it had already run — against the development
server, via the rsync/`docker build` loop. A database stamped `0082` never replays it, so editing
0082 fixes fresh installs and nothing else: every snapshot insert and every
`GET /watching-account/snapshots` on the already-migrated machine raises `no such column`.

Loud rather than lossy, but the feature would be dead on exactly the box it was built against — and
that is the shape 0032 is remembered for, a migration that was a no-op on every real database.

Idempotent by inspection rather than by try/except: the column is present on any install that took
0082 in its final form, and this must be a clean no-op there.

Revision ID: 0083
Revises: 0082
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "watch_state_snapshots" not in inspector.get_table_names():
        return  # 0082 will create it complete; nothing to patch
    if "complete" in {c["name"] for c in inspector.get_columns("watch_state_snapshots")}:
        return
    # server_default true: existing rows were taken before the partial-read guard existed, and the
    # only snapshots that can be there were written by a build whose reads either succeeded outright
    # or raised. Defaulting them to "incomplete" would refuse an undo that is actually sound.
    op.add_column(
        "watch_state_snapshots",
        sa.Column("complete", sa.Boolean, nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "watch_state_snapshots" not in inspector.get_table_names():
        return
    if "complete" not in {c["name"] for c in inspector.get_columns("watch_state_snapshots")}:
        return
    op.drop_column("watch_state_snapshots", "complete")
