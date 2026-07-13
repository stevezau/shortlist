"""per-row attribution + per-user row overrides

Two additions that let the user page show a person's rows individually:
 - ``picks.collection_slug`` attributes each stored pick to the row that produced it, so a
   person's picks can be grouped by row.
 - ``collection_user_overrides`` lets one person mute / resize / restyle one row without changing
   it for everyone else.

Idempotent like 0003: SQLite auto-commits DDL, so a run interrupted mid-migration (two deployers
racing — as happened live) can leave the change half-applied. Guarding each step lets a re-run
finish the job instead of failing on "duplicate column" / "table already exists".
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    pick_columns = {col["name"] for col in inspector.get_columns("picks")}
    if "collection_slug" not in pick_columns:
        op.add_column("picks", sa.Column("collection_slug", sa.String(255), nullable=False, server_default=""))
    # Guarded separately from the column: a half-apply that added the column then died would leave
    # the index uncreated, so a re-run must still be able to create it.
    if "ix_picks_collection_slug" not in {ix["name"] for ix in inspector.get_indexes("picks")}:
        op.create_index("ix_picks_collection_slug", "picks", ["collection_slug"])

    if "collection_user_overrides" not in set(inspector.get_table_names()):
        op.create_table(
            "collection_user_overrides",
            sa.Column(
                "collection_id",
                sa.Integer,
                sa.ForeignKey("collections.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "user_id",
                sa.Integer,
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("muted", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("row_size", sa.Integer, nullable=True),
            sa.Column("prompt", sa.JSON, nullable=False, server_default="{}"),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )


def downgrade() -> None:
    op.drop_table("collection_user_overrides")
    op.drop_index("ix_picks_collection_slug", table_name="picks")
    op.drop_column("picks", "collection_slug")
