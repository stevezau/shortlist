"""Drop `collections.shown_state`, which an unreleased draft of 0088 created.

The day-schedule job (`rows.visibility`, issue #102) briefly cached the last-applied answer per row so
a night with no transition could skip its work. The cache was not worth it: today's answer is
`row_is_shown(show_days, now)` — the schedule plus the calendar, and nothing else — so the column was
a second source of truth for something already free to recompute, and it produced two bugs on its own.
It recorded rows as converged under `paused_all` (nothing had been promoted), and again for a
collection the pass had SKIPPED because it could not identify it. Both left a row visible on a day its
schedule said to hide it, permanently, because the cache then agreed there was nothing left to do.

The job now recomputes every time and holds nothing, which is both smaller and self-healing: whatever
one pass cannot do, the next one does.

This only ever existed between two commits on a feature branch, so on almost every install the column
is absent and this migration does nothing. It exists for the one case that is real — a maintainer
running a build from that branch on their own server, whose database already carries the column. The
drop is guarded, so it is safe either way.

Revision ID: 0089
Revises: 0088
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "shown_state" in _columns(op.get_bind(), "collections"):
        op.drop_column("collections", "shown_state")


def downgrade() -> None:
    # Recreated nullable and empty. Nothing reads it, so there is no value to restore — a downgrade
    # only has to leave the schema shaped the way 0088's draft left it.
    if "shown_state" not in _columns(op.get_bind(), "collections"):
        op.add_column("collections", sa.Column("shown_state", sa.Boolean(), nullable=True))
