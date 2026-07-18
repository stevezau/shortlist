"""custom poster config per row

Adds ``poster`` (JSON) to ``collections``: how a row's Plex collection poster is produced —
``{}`` (the default) means leave Plex's own artwork alone; otherwise
``{"mode": "upload"|"generate", "title", "subtitle", "style"}``. Existing rows stay ``{}``
(no custom poster), so the upgrade is behaviour-neutral. The image bytes live in the ``poster_assets``
table this also creates (uploaded originals + cached generated images), not in the JSON. Idempotent.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    cols = {c["name"] for c in inspector.get_columns("collections")}
    if "poster" not in cols:
        op.add_column("collections", sa.Column("poster", sa.JSON(), nullable=False, server_default="{}"))
    if "poster_assets" not in inspector.get_table_names():
        op.create_table(
            "poster_assets",
            sa.Column("key", sa.String(length=80), primary_key=True),
            sa.Column("image", sa.LargeBinary(), nullable=False),
            sa.Column("content_type", sa.String(length=64), nullable=False, server_default="image/png"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "poster_assets" in inspector.get_table_names():
        op.drop_table("poster_assets")
    cols = {c["name"] for c in inspector.get_columns("collections")}
    if "poster" in cols:
        with op.batch_alter_table("collections") as batch:
            batch.drop_column("poster")
