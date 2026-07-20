"""request_candidates.hidden

Adds ``hidden`` (Boolean, default False) to ``request_candidates``: lets the owner clear a title from
the Sent log without deleting the row. The row stays ``status="sent"`` — a load-bearing tombstone that
stops a still-downloading title being re-requested — so we hide it from the inbox rather than drop it.
Existing rows get False (visible). Idempotent like the migrations before it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("request_candidates")}
    if "hidden" not in cols:
        op.add_column(
            "request_candidates",
            sa.Column("hidden", sa.Boolean(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("request_candidates")}
    if "hidden" in cols:
        with op.batch_alter_table("request_candidates") as batch:
            batch.drop_column("hidden")
