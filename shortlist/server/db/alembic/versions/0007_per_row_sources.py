"""per-row candidate sources, and removal of the never-wired `source` column

Adds ``collections.candidate_sources`` (JSON): a per-row override of which discovery sources feed
that row. Empty list means "inherit the global ``candidates.sources`` setting", so existing rows are
unchanged until the owner picks sources for one.

Also drops ``collections.source`` (``all_users``/``opted_in``): it shipped in 0003 but was never read
by the engine — dead configuration that collided in spirit with the new per-row *sources*. Dropping
it uses batch mode so SQLite (which can't ALTER-DROP in place) rebuilds the table.

Idempotent like the migrations before it: the add is guarded, and the drop only runs if the column
is still present, so a re-run after an interrupted migration finishes cleanly.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = _columns(inspector, "collections")
    if "candidate_sources" not in cols:
        op.add_column(
            "collections",
            sa.Column("candidate_sources", sa.JSON, nullable=False, server_default="[]"),
        )
    if "source" in cols:
        with op.batch_alter_table("collections") as batch:
            batch.drop_column("source")


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = _columns(inspector, "collections")
    if "source" not in cols:
        op.add_column(
            "collections",
            sa.Column("source", sa.String(16), nullable=False, server_default="all_users"),
        )
    if "candidate_sources" in cols:
        with op.batch_alter_table("collections") as batch:
            batch.drop_column("candidate_sources")
