"""Two composite `picks` indexes for the dashboard report.

`report_service._avg_days_to_watch` groups `picks` by (user, title) and takes `min(created_at)` and
`min(watched_at)`; without a covering index SQLite builds a temp B-tree over the whole table (376ms -> 56ms
on a 160k-row copy). `_viewing_share` looks up each person's first pick by `created_at` (211ms -> 18ms).

Indexes only — no column or data changes, so this is safe to run and to reverse at any point. SQLite
builds an index in place without rewriting the table. Guarded, so re-running is a no-op.

Revision ID: 0101
Revises: 0100
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_picks_user_title_dates", ["user_id", "tmdb_id", "media_type", "created_at", "watched_at"]),
    ("ix_picks_user_created", ["user_id", "created_at"]),
)


def _existing(bind) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes("picks")}


def upgrade() -> None:
    existing = _existing(op.get_bind())
    for name, columns in _INDEXES:
        if name not in existing:
            op.create_index(name, "picks", columns, unique=False)


def downgrade() -> None:
    existing = _existing(op.get_bind())
    for name, _ in _INDEXES:
        if name in existing:
            op.drop_index(name, table_name="picks")
