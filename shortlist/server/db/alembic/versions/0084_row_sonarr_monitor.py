"""Per-row Sonarr monitor mode.

Every show Shortlist requested was added with Sonarr's `monitor: all`, so a twelve-season show
started downloading twelve seasons the night it was picked (issue #100). The global choice lives in
`settings` and needs no migration; this is the per-row override beside the profile and root folder
that row already has, for the case those serve too — a kids row taking season 1 only while every
other row keeps the whole run of a show.

NULLable with no backfill and no server default: NULL means "inherit the global", exactly as the
`req_*` columns beside it already do. An upgrade therefore changes nothing on any live server, and
the global itself defaults to `all` — today's behaviour — until somebody picks another mode.

Revision ID: 0084
Revises: 0083
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "req_sonarr_monitor" not in _columns(op.get_bind(), "collections"):
        op.add_column("collections", sa.Column("req_sonarr_monitor", sa.String(length=32), nullable=True))


def downgrade() -> None:
    if "req_sonarr_monitor" in _columns(op.get_bind(), "collections"):
        op.drop_column("collections", "req_sonarr_monitor")
