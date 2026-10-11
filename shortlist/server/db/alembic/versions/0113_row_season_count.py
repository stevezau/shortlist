"""Per-row season count for "first/last N seasons" of a requested show.

The mode (`req_sonarr_monitor`) already says first or last; this is how many (issue #154). NULLable
with no server default: NULL means "inherit the global `requests.season_count`", exactly as the
`req_*` columns beside it do. A row already set to `firstSeason`/`lastSeason` is backfilled to 1,
because one season is all those could mean when the row chose them — left NULL, it would follow the
global count the day the owner raises it for other rows.

Revision ID: 0113
Revises: 0112
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "req_season_count" not in _columns(op.get_bind(), "collections"):
        op.add_column("collections", sa.Column("req_season_count", sa.Integer(), nullable=True))
        op.execute(
            "UPDATE collections SET req_season_count = 1 WHERE req_sonarr_monitor IN ('firstSeason', 'lastSeason')"
        )


def downgrade() -> None:
    if "req_season_count" in _columns(op.get_bind(), "collections"):
        op.drop_column("collections", "req_season_count")
