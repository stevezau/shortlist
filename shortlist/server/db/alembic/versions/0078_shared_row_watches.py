"""`shared_row_watches` — where a credit for a SHARED row can land.

Shared rows write no `picks` rows (`RunSharedRow` records why: `PickRow.user_id` is non-nullable and
RESTRICT-keyed to a real account, and nullable-user pick rows would put rows nobody watched into
every per-user hit-rate query). That left shared rows out of watch tracking entirely, because a
credit had nowhere to be written — the owner asked for them to count from the start.

Not folded into `picks` and not into `run_shared_rows.picks`. The first would reintroduce exactly the
nullable-user rows that JSON was chosen to avoid; the second is a per-RUN record of what a row
contained, and a watch is a per-PERSON fact that outlives the run, so stamping it there would either
be lost on the next run or make a historical record mutable.

Revision ID: 0078
Revises: 0077
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "shared_row_watches" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "shared_row_watches",
        # RESTRICT, matching `picks`: a person's watch history is not collateral of a user delete.
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("collection_slug", sa.String(255), primary_key=True),
        sa.Column("tmdb_id", sa.Integer, primary_key=True),
        # In the key, not just a column: TMDB ids are namespaced per media type, so movie 1399 and
        # show 1399 are different titles and must not collide.
        sa.Column("media_type", sa.String(16), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False, server_default=""),
        sa.Column("watched_at", sa.DateTime, nullable=True),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("max_percent", sa.Integer, nullable=True),
    )
    # No separate index on `user_id`: it LEADS the composite primary key, whose index already serves
    # the only query here ("this person's shared-row watches"). Adding one drifts from the ORM.


def downgrade() -> None:
    # Guarded like 0066's and 0076's, so a partially-applied downgrade is re-runnable.
    if "shared_row_watches" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("shared_row_watches")
