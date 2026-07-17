"""imdb id for each requested title

Adds ``imdb_id`` (String, default ``""``) to ``request_candidates`` so the approval inbox can deep-link
straight to a title's IMDb page (``/title/tt…``) instead of an IMDb search. Empty (the default for
existing rows) means "not resolved yet"; the next run that re-surfaces the title fills it in from TMDB.
Idempotent like the migrations before it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("request_candidates")}
    if "imdb_id" not in cols:
        op.add_column(
            "request_candidates", sa.Column("imdb_id", sa.String(length=16), nullable=False, server_default="")
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("request_candidates")}
    with op.batch_alter_table("request_candidates") as batch:
        if "imdb_id" in cols:
            batch.drop_column("imdb_id")
