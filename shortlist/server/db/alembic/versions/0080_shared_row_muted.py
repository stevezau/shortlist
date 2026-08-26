"""`run_shared_rows.muted` — who had a shared row switched off, as a DENY-list.

Mutes used to be subtracted into `audience`, which forced a PUBLIC row to stop saying "everyone" the
moment one person muted it: the snapshot became a concrete list of whoever existed that night, and
anyone invited afterwards was permanently outside it. Credit is decided from the past and a watched
title is never re-delivered, so that miss is silent and unrecoverable.

Existing rows keep working unchanged: a public row that had mutes carries the old baked-in list in
`audience`, which still reads correctly as an allow-list — it simply cannot learn about people who
joined later, which is the behaviour this column fixes going forward rather than retroactively.

Revision ID: 0080
Revises: 0079
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "muted" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("run_shared_rows")}:
        return
    op.add_column("run_shared_rows", sa.Column("muted", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "muted" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("run_shared_rows")}:
        op.drop_column("run_shared_rows", "muted")
