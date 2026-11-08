"""The Overseerr account the owner chose for a person.

Issue #181: a "Your requests" row credits an Overseerr request to a person through the account's Plex
sign-in. 0 means automatic (that sign-in); a non-zero value is the Overseerr user id the owner picked by
hand. One account can belong to one person, so the non-zero values are unique.

Revision ID: 0115
Revises: 0114
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None

_INDEX = "ux_users_seerr_user_id"


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "seerr_user_id" not in _columns(bind, "users"):
        op.add_column("users", sa.Column("seerr_user_id", sa.Integer(), nullable=False, server_default="0"))
    if _INDEX not in _indexes(bind, "users"):
        op.create_index(
            _INDEX,
            "users",
            ["seerr_user_id"],
            unique=True,
            sqlite_where=sa.text("seerr_user_id != 0"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _INDEX in _indexes(bind, "users"):
        op.drop_index(_INDEX, table_name="users")
    if "seerr_user_id" in _columns(bind, "users"):
        op.drop_column("users", "seerr_user_id")
