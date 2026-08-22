"""Per-row override for tagging Sonarr/Radarr requests with the wanting person's slug.

The global switch (`requests.auto_user_tag`) is a settings row and needs no migration. This column is
the per-row override: NULL means "inherit the global", exactly as every other `req_*` column added in
0074 does, so an upgrade changes nothing on any live server until somebody sets one.

Nullable with no server default, and no backfill — a FALSE backfill would freeze every existing row
at "off" and the global switch would then do nothing to any of them, which is the opposite of what a
global switch is for.

Revision ID: 0075
Revises: 0074
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "req_auto_user_tag" not in _columns(op.get_bind(), "collections"):
        op.add_column("collections", sa.Column("req_auto_user_tag", sa.Boolean(), nullable=True))


def downgrade() -> None:
    if "req_auto_user_tag" in _columns(op.get_bind(), "collections"):
        op.drop_column("collections", "req_auto_user_tag")
