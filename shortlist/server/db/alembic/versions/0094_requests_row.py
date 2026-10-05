"""A "Your requests" row (issue #127): three per-row settings and one per-person tag.

`collections.requests_row` marks a row as built from what each person asked for in Overseerr /
Radarr / Sonarr; `requests_window_days` is how long a landed request stays on it; and
`requests_tag_pattern` is how the *arrs tag a person's requests. `users.requested_by_tag` is one
person's own tag, which wins over the pattern. Every default is the off/empty value, so every
existing row and person comes out of this migration unchanged.

Re-runnable because every add is guarded, per `tests/integration/test_migration_recovery.py`.

Revision ID: 0094
Revises: 0093
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("collections", sa.Column("requests_row", sa.Boolean(), nullable=False, server_default="0")),
    ("collections", sa.Column("requests_window_days", sa.Integer(), nullable=False, server_default="90")),
    ("collections", sa.Column("requests_tag_pattern", sa.String(128), nullable=False, server_default="")),
    ("users", sa.Column("requested_by_tag", sa.String(64), nullable=False, server_default="")),
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    for table, column in _COLUMNS:
        if column.name not in _columns(bind, table):
            op.add_column(table, column)


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in reversed(_COLUMNS):
        if column.name in _columns(bind, table):
            op.drop_column(table, column.name)
