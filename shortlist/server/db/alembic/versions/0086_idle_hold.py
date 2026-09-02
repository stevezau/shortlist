"""The idle hold: don't re-pick a row for someone who has watched nothing since it was built.

Two columns, one feature (issue #109). `picks.built_at` records when a row's CONTENTS were last
chosen; `collections.idle_hold_days` is that row's ceiling on how long it may wait.

`built_at` is deliberately not `picks.created_at`. A carried-forward row is re-persisted under every
run, so `created_at` means "last delivered" — measuring against it would report every row as freshly
built, the ceiling could never expire, and no watch could ever look newer than the row.

Both NULLable, no backfill, no server default. NULL `built_at` reads as "unknown", which falls back
to the plain refresh cadence — the same convention `picks.recipe` uses, and the reason an upgrade
cannot freeze a server's rows for the ceiling's length on the night it lands. NULL
`idle_hold_days` inherits the global, which itself defaults to 0 (off), so an upgrade changes
nothing on any live server until somebody turns it on.

Revision ID: 0086
Revises: 0085
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "built_at" not in _columns(bind, "picks"):
        op.add_column("picks", sa.Column("built_at", sa.DateTime(timezone=True), nullable=True))
    if "idle_hold_days" not in _columns(bind, "collections"):
        op.add_column("collections", sa.Column("idle_hold_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "built_at" in _columns(bind, "picks"):
        op.drop_column("picks", "built_at")
    if "idle_hold_days" in _columns(bind, "collections"):
        op.drop_column("collections", "idle_hold_days")
