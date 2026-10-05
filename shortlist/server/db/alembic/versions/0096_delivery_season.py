"""`deliveries.season` — the season each seasonal collection was last built for (issue #137).

A seasonal row that finds nothing for its new season in a library delivers nothing there, so that library
keeps last season's collection: its title and its films. Promotion now hides such a collection instead of
showing it, and this column is how it knows: ``slug@anchor`` of the season the films were chosen for, "" for a
row that was not seasonal.

Nullable with no default, so every existing ledger row comes out of this migration as "not recorded".
Promotion then falls back to the season named by the stored picks' recipe, and with neither it promotes the
collection exactly as before — no row loses its place on the night this ships.

Re-runnable because the add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0096
Revises: 0095
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("deliveries")}


def upgrade() -> None:
    bind = op.get_bind()
    if "season" not in _columns(bind):
        op.add_column("deliveries", sa.Column("season", sa.String(255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "season" in _columns(bind):
        op.drop_column("deliveries", "season")
