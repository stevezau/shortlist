"""Per-row day-of-week visibility schedule ("When it appears", issue #102).

A row was a permanent fixture: once delivered it sat on Home until it was switched off or deleted.
`show_days` lets one row take days off — hidden, not deleted, so it keeps its titles and returns
without a rebuild (a hub visibility write is ~5ms on every library; changing what is IN a collection
costs up to 26s on a large TV library, and this path never does that).

`show_days` is a JSON list of ISO weekdays (1=Mon .. 7=Sun). **Empty means every day**, which is what
every existing row gets here — so an upgrade changes nothing until somebody picks days. There is
deliberately no way to spell "never": that is what switching the row off already means, and a second
spelling of it would be two controls for one outcome.

The job that applies this keeps NO state of its own — today's answer is the schedule plus the
calendar, so there is nothing to cache. An earlier draft carried a `shown_state` column to skip work
on quiet nights; it produced two bugs by itself and was removed before release. Migration 0089 drops
it for anyone who ran that draft from a host build.

Revision ID: 0088
Revises: 0087
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind, "collections")
    if "show_days" not in existing:
        # server_default rather than a backfill UPDATE: rows written by an older build that is still
        # running mid-upgrade get the same "every day" answer as the ones already here.
        op.add_column("collections", sa.Column("show_days", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind, "collections")
    if "show_days" in existing:
        op.drop_column("collections", "show_days")
