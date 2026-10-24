"""Per-row list of libraries a row does not request titles for.

An exclusion list (issue #169): NULL and an empty list both mean "nothing skipped", so every existing row
keeps asking for titles meant for all of its libraries, and a library added to Plex later is on by default.

Revision ID: 0114
Revises: 0113
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    if "req_skip_library_keys" not in _columns(op.get_bind(), "collections"):
        op.add_column("collections", sa.Column("req_skip_library_keys", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "req_skip_library_keys" in _columns(op.get_bind(), "collections"):
        op.drop_column("collections", "req_skip_library_keys")
