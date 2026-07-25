"""Add restricted flag to users

Tracks whether a user has Plex parental controls (restricted="1" in the plex.tv roster). Restricted
accounts are skipped during runs: Plex hides all content (including collections) from them based on
their age rating profile, so building a row they can't see is pointless. The flag is synced from
plex.tv on each user sync.
"""

import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "restricted" not in columns:
        op.add_column("users", sa.Column("restricted", sa.Boolean(), server_default="0", nullable=False))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "restricted" in columns:
        op.drop_column("users", "restricted")
