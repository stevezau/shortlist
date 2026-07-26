"""Split placement into owner vs friends

Adds a `placement_friends` column to collections so friends/shared users can have different
visibility from the owner/home users. The existing `placement` column now controls only
Own Home + Library for the owner, while `placement_friends` controls Friends Home + Library
for shared users. Defaults to "both" (same as current behaviour).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    columns = [c["name"] for c in inspect(conn).get_columns("collections")]
    if "placement_friends" in columns:
        return
    op.add_column("collections", sa.Column("placement_friends", sa.String(16), server_default="both", nullable=False))
    op.execute("UPDATE collections SET placement_friends = placement")


def downgrade() -> None:
    op.drop_column("collections", "placement_friends")
