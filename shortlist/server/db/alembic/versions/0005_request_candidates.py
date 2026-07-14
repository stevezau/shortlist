"""request_candidates — the Sonarr/Radarr approval inbox

Persists wanted-but-missing titles a run surfaced but did not auto-send, so the owner can approve or
reject each one by hand (the Hybrid request flow). One row per (tmdb_id, media_type); a re-surfaced
title refreshes demand/rating in place.

Idempotent like 0003/0004: SQLite auto-commits DDL, so a run interrupted mid-migration (two deployers
racing) can leave the change half-applied. Guarding the create lets a re-run finish rather than fail
on "table already exists".
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "request_candidates" not in set(inspector.get_table_names()):
        op.create_table(
            "request_candidates",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("tmdb_id", sa.Integer, nullable=False),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("year", sa.Integer, nullable=True),
            sa.Column("rating", sa.Float, nullable=False, server_default="0"),
            sa.Column("vote_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("demand", sa.Integer, nullable=False, server_default="1"),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("detail", sa.String(512), nullable=False, server_default=""),
            sa.Column("first_seen_run_id", sa.Integer, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint("tmdb_id", "media_type", name="uq_request_candidate_title"),
        )

    # Indexes guarded separately from the table (0004's pattern): SQLite auto-commits DDL, so a crash
    # between CREATE TABLE and CREATE INDEX would otherwise leave them uncreated, and the table guard
    # above would skip them on the re-run. Re-inspect because the table may have just been created.
    inspector = sa.inspect(bind)
    existing = {ix["name"] for ix in inspector.get_indexes("request_candidates")}
    if "ix_request_candidates_tmdb_id" not in existing:
        op.create_index("ix_request_candidates_tmdb_id", "request_candidates", ["tmdb_id"])
    if "ix_request_candidates_status" not in existing:
        op.create_index("ix_request_candidates_status", "request_candidates", ["status"])


def downgrade() -> None:
    op.drop_index("ix_request_candidates_status", table_name="request_candidates")
    op.drop_index("ix_request_candidates_tmdb_id", table_name="request_candidates")
    op.drop_table("request_candidates")
