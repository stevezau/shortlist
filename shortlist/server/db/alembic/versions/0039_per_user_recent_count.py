"""Per-user watch-history depth override

The row-size and mute tweaks a person can already have on a single row (`collection_user_overrides`)
now gain a third: `recent_count`, how many of that person's most recent watches the AI web-search
source searches for that row. NULL (the default for every existing override) means "fall through to
the row's own recent_count, then the global recommendations.recent_count" — so existing rows behave
exactly as before until an override is set.
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collection_user_overrides")}
    if "recent_count" not in columns:
        op.add_column("collection_user_overrides", sa.Column("recent_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collection_user_overrides")}
    if "recent_count" in columns:
        op.drop_column("collection_user_overrides", "recent_count")
