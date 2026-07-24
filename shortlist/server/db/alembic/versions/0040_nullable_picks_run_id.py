"""Make picks.run_id nullable so picks survive run deletion

The dashboard's effectiveness report (delivered-vs-watched, hit rate, time-to-watch) is built
entirely from `picks`. The old `clear_runs` and the retention pruner both deleted picks alongside
their runs, which silently wiped the dashboard. Making `run_id` nullable lets picks outlive their
run — the pruner and clear action null it instead of deleting the pick, so accumulated metrics
survive indefinitely while run history is cleaned up.

SQLite can't ALTER COLUMN to change nullability, so this uses the standard batch-alter pattern:
create a new table, copy the data, drop the old one, rename. The index on `run_id` is preserved.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("picks") as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Orphaned picks (run_id=NULL) would violate the restored NOT NULL. Set them to 0 (a run that
    # doesn't exist, but satisfies the constraint — the report still reads them fine).
    op.execute("UPDATE picks SET run_id = 0 WHERE run_id IS NULL")
    with op.batch_alter_table("picks") as batch_op:
        batch_op.alter_column("run_id", existing_type=sa.Integer(), nullable=False)
