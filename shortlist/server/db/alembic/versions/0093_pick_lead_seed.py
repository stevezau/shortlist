"""Record on each pick the watch its `{top_seed}` row was built from (issue #133).

A "Because you watched {top_seed}" row took its name only from picks that carry a seed, and discover and
web-search picks never do. When a new watch had no look-alikes in the library, nothing in the rebuilt row
carried a seed, the name rendered empty, and delivery left the old collection — last watch's title and
items — on Plex. The engine now names such a row after the watch it was built from, and needs last run's
value to tell whether that watch has since moved on.

Both NULLable, no backfill, no server default. NULL reads as "unknown", which keeps the behaviour before
this column existed — the same convention `picks.recipe` and `picks.built_at` use — so an upgrade changes
nothing until a row is next built.

Revision ID: 0093
Revises: 0092
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None

_COLUMNS = ("lead_seed_tmdb_id", "lead_seed_title")


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("picks")}


def upgrade() -> None:
    existing = _columns(op.get_bind())
    if "lead_seed_tmdb_id" not in existing:
        op.add_column("picks", sa.Column("lead_seed_tmdb_id", sa.Integer(), nullable=True))
    if "lead_seed_title" not in existing:
        op.add_column("picks", sa.Column("lead_seed_title", sa.String(512), nullable=True))


def downgrade() -> None:
    existing = _columns(op.get_bind())
    for name in _COLUMNS:
        if name in existing:
            op.drop_column("picks", name)
