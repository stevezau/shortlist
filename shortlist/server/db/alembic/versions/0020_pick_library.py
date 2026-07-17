"""library each pick was delivered into

Adds ``section_key`` (String, the stable Plex section key) and ``library`` (String, its display name
like "Movies") to ``picks``. A row that targets more than one library becomes one Plex collection PER
library, so the effectiveness report splits it into one line per library — which needs to know each
pick's library. Both default ``""``; existing rows (pre-0020) stay blank and fall into a single
"library unknown" bucket, since a past run's per-library split can't be reconstructed. Idempotent.
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("picks")}
    if "section_key" not in cols:
        op.add_column("picks", sa.Column("section_key", sa.String(length=64), nullable=False, server_default=""))
    if "library" not in cols:
        op.add_column("picks", sa.Column("library", sa.String(length=255), nullable=False, server_default=""))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("picks")}
    with op.batch_alter_table("picks") as batch:
        if "library" in cols:
            batch.drop_column("library")
        if "section_key" in cols:
            batch.drop_column("section_key")
