"""per-row watched cap

Adds ``collections.watched_pct`` (float, nullable): the largest share of this row that may be
already-finished titles (0.0 = all fresh, 1.0 = no filtering). NULL = inherit the global
``recommendations.watched_pct`` setting. Existing rows get NULL (inherit), so behaviour is
unchanged until an owner sets one.

Idempotent like the migrations before it: the add is guarded so a re-run after an interrupted
migration finishes cleanly.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("collections")}
    if "watched_pct" not in cols:
        op.add_column("collections", sa.Column("watched_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("collections")}
    if "watched_pct" in cols:
        with op.batch_alter_table("collections") as batch:
            batch.drop_column("watched_pct")
