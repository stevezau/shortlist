"""Record where each pick came from, and how strongly its source vouched for it

A beta user's medical-drama row filled up with fantasy and sci-fi, and answering "why?" meant
querying TMDB by hand and reading the ranking code. The pool already knew — `Candidate.sources` and
the new `affinity` — but none of it survived into the pick, so the UI could only ever say "Because
you watched X" with no indication of how strong that claim was.

Existing rows keep the neutral defaults: blank sources ("we didn't record it") and affinity 1.0, so
old picks are never rendered as though they were weak matches.
"""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("picks")}
    if "sources" not in columns:
        op.add_column("picks", sa.Column("sources", sa.String(length=255), nullable=False, server_default=""))
    if "affinity" not in columns:
        op.add_column("picks", sa.Column("affinity", sa.Float(), nullable=False, server_default="1.0"))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("picks")}
    for name in ("sources", "affinity"):
        if name in columns:
            op.drop_column("picks", name)
