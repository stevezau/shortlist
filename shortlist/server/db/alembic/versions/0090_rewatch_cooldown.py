"""A rewatch row's cooldown: how long a just-finished title stays out of it (issue #114).

"Happy to see again" is now built from the person's own finished titles rather than from the
similar-titles pool, which almost never held them. Without a cooldown the freshest of those — last
night's film — would lead a shelf meant for old favourites, so `rewatch_cooldown_days` leaves out
anything finished within that many days. 0 means no cooldown.

NOT NULL with a server default of 30, rather than NULL-inherits-a-global like `refresh_days`: there is
no global for this, the owner asked for a per-row setting defaulting to 30 days, and a server default
fills every existing row in the same statement — including rows written by a path that bypasses the
ORM. Only rewatch rows read it, and those are the rows this fixes, so the backfill changes nothing on
any other row.

Revision ID: 0090
Revises: 0089
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "rewatch_cooldown_days" not in _columns(op.get_bind(), "collections"):
        op.add_column(
            "collections",
            sa.Column("rewatch_cooldown_days", sa.Integer(), nullable=False, server_default="30"),
        )


def downgrade() -> None:
    if "rewatch_cooldown_days" in _columns(op.get_bind(), "collections"):
        op.drop_column("collections", "rewatch_cooldown_days")
