"""collections + collection_audience — curated rows as first-class definitions

Shortlist grows from one hard-coded per-user row to any number of curated rows. A ``Collection``
carries how it's built (per_person | shared), who it's for (audience), and its recipe (size,
name, prompt). The single default ``picked`` collection seeded here reproduces today's
"Picked for You" row exactly, so an upgrade is behaviour-neutral until the owner adds more.
"""

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: SQLite auto-commits DDL, so a run interrupted mid-migration (e.g. two deployers
    # racing) can leave the tables present but the version unbumped. Creating with checkfirst and
    # seeding only when empty lets a re-run finish the job instead of failing on "already exists".
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "collections" not in existing:
        op.create_table(
            "collections",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("slug", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("build", sa.String(16), nullable=False, server_default="per_person"),
            sa.Column("audience", sa.String(16), nullable=False, server_default="everyone"),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("size", sa.Integer, nullable=False, server_default="15"),
            sa.Column("media", sa.String(16), nullable=False, server_default="both"),
            sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
            sa.Column("name_template", sa.String(255), nullable=False, server_default=""),
            sa.Column("source", sa.String(16), nullable=False, server_default="all_users"),
            sa.Column("min_watchers", sa.Integer, nullable=False, server_default="2"),
            sa.Column("prompt", sa.JSON, nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )
        op.create_index("ix_collections_slug", "collections", ["slug"], unique=True)
    if "collection_audience" not in existing:
        op.create_table(
            "collection_audience",
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
        )

    # Seed the default per-person row (only if not already present) with fixed defaults. Its actual
    # name and size follow the live Settings > Defaults values at RUN time (the adapter's _build_rows
    # keeps the 'picked' row in sync), so nothing here needs to read those settings — which also
    # avoids parsing the JSON `settings` column from a migration, whose stored form is unreliable.
    if bind.execute(sa.text("SELECT count(*) FROM collections")).scalar():
        return
    # Only columns with no server_default are listed; the rest (incl. the since-dropped `source`,
    # removed in 0007) take their defaults. Naming a column here that a later migration drops would
    # break this seed when it re-runs during recovery against an already-migrated schema.
    now = datetime.now(UTC).isoformat()
    bind.execute(
        sa.text(
            "INSERT INTO collections "
            "(slug, name, build, audience, enabled, size, media, sort_order, name_template, "
            " min_watchers, prompt, created_at, updated_at) "
            "VALUES ('picked', '✨ Picked for You', 'per_person', 'everyone', 1, 15, 'both', 0, '', "
            " 2, '{}', :now, :now)"
        ),
        {"now": now},
    )


def downgrade() -> None:
    op.drop_table("collection_audience")
    op.drop_index("ix_collections_slug", table_name="collections")
    op.drop_table("collections")
