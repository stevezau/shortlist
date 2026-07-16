"""per-row Recommended-shelf anchor override

Adds ``hub_anchor`` (JSON, default ``{}``) to ``collections``: a per-library override of where THIS
row sits in Plex's Recommended shelf — ``{sectionKey: {"anchor": "<collection title>", "before":
bool}}``. Empty (the default for existing rows) means the row inherits the global default from
settings ``rows.hub_anchor``, so behaviour is unchanged until an owner sets a per-row anchor.
Idempotent like the migrations before it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("collections")}
    if "hub_anchor" not in cols:
        op.add_column("collections", sa.Column("hub_anchor", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("collections")}
    with op.batch_alter_table("collections") as batch:
        if "hub_anchor" in cols:
            batch.drop_column("hub_anchor")
