"""Add the delivery ledger: which Plex collection is which row, for whom, in which library

Every on-demand reconcile — delete a row, switch it off, narrow its audience or its libraries, rename
it — has to find the collections it is about. Until now that was done by matching a TITLE: either the
one the row's name template renders to, or the one the latest completed run recorded.

Both fail, in ways that are silent:

* A ``{top_seed}`` template renders a different title every run, so nothing computed from config can
  find it.
* Rows have their own crons, so "the latest completed run" is routinely scoped to a DIFFERENT row.
  Delete row B the morning after row A ran and nothing matched — audited as "removed 0", with no
  second chance for a row that no longer exists.
* ``DELETE /api/runs`` erased that record outright, while its own docstring promised it changed
  nothing on Plex.

This table stores the ratingKey instead, which is the identity Plex itself writes on and the only
handle that survives a title changing. Rows are written as each collection is delivered.

Keyed by (collection_slug, user_slug, library_key) as plain strings rather than foreign keys: the row
this describes is usually being DELETED at the moment the ledger is read, and a cascade would take the
answer with it.

Nothing backfills — there is no source to backfill FROM, which is the whole problem. Existing installs
populate on their next run, and until then the reconciles fall back to rendering the template, exactly
as they do today. So this is additive: strictly more addressing, never less.
"""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "deliveries" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "deliveries",
        sa.Column("collection_slug", sa.String(255), primary_key=True, nullable=False),
        sa.Column("user_slug", sa.String(255), primary_key=True, nullable=False),
        sa.Column("library_key", sa.String(32), primary_key=True, nullable=False),
        sa.Column("rating_key", sa.Integer, nullable=False),
        sa.Column("title", sa.String(512), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_deliveries_rating_key", "deliveries", ["rating_key"])


def downgrade() -> None:
    if "deliveries" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_deliveries_rating_key", table_name="deliveries")
        op.drop_table("deliveries")
