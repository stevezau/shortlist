"""`run_shared_rows.delivered_at` — when a shared row's contents actually landed on Plex.

Shared-row membership was timed off `Run.started_at`, which is the clock the per-person path was
deliberately moved off and left a comment about: a run persists each row as it finishes, so the run's
start trails the delivery by minutes to tens of minutes (a TV collection write alone costs ~16.5s,
times 47 people).

Judging a play against the run's START means judging it against the row the run was BUILDING rather
than the one Plex was still serving. Both directions are wrong and only one self-heals:

* A title this run DROPPED is dated out of the row at run start, so someone who plays it thirty
  minutes into an 88-minute run — while Plex is still serving the old collection — gets no credit.
  A watched title is never re-delivered, so no later play can rescue it. That one is permanent.
* A title this run ADDED is dated in at run start, so a play made before the collection existed is
  credited to it.

`_load_per_person` derives its equivalent from `min(picks.created_at)`. A shared row writes no picks
(see `RunSharedRow`), so it has to be stamped explicitly.

NULL on every existing row, and the readers fall back to `Run.started_at` for those — the same
behaviour they had before, rather than a backfill inventing a precision the old data never had.

Revision ID: 0079
Revises: 0078
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "delivered_at" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("run_shared_rows")}:
        return
    op.add_column("run_shared_rows", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if "delivered_at" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("run_shared_rows")}:
        op.drop_column("run_shared_rows", "delivered_at")
