"""per-(row, library) run breakdown

Adds ``run_users.breakdown`` (JSON, default ``[]``): the per-(row, library) delivery result for a
run, so the UI can show "added X to Movies, Y to TV" and each library's own ranked picks instead of
one merged list. Legacy rows get ``[]`` and the UI falls back to the merged ``diff`` + ``picks``.

Idempotent like the migrations before it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("run_users")}
    if "breakdown" not in cols:
        op.add_column("run_users", sa.Column("breakdown", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("run_users")}
    if "breakdown" in cols:
        with op.batch_alter_table("run_users") as batch:
            batch.drop_column("breakdown")
