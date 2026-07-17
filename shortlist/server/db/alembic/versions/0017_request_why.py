"""provenance for each requested title

Adds ``why`` (JSON, default ``[]``) to ``request_candidates``: one entry per (person, row) that
wanted the missing title — ``{user, row, seed, source}`` — so the approval inbox and the sent log can
explain which row a request came from and why (the seed "because you watched …"), not just a count.
Empty (the default for existing rows) means "not recorded yet"; the next run that re-surfaces the
title fills it in. Idempotent like the migrations before it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("request_candidates")}
    if "why" not in cols:
        op.add_column("request_candidates", sa.Column("why", sa.JSON(), nullable=False, server_default="[]"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("request_candidates")}
    with op.batch_alter_table("request_candidates") as batch:
        if "why" in cols:
            batch.drop_column("why")
