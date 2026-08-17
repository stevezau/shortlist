"""Per-row Sonarr/Radarr request settings, and the row slug on a queued request.

Every request setting used to be global, so a run with several rows filed everything into one root
folder at one quality profile, gated everything on one set of floors, and let whichever row held the
highest-demand titles consume the whole `max_per_run` — every night, since that ranking barely moves.

Two groups of columns:

`collections` gains this row's own floors and target. All NULLable, no backfill: NULL means "inherit
the global setting", exactly as `watched_pct`, `recency`, `refresh_days` and `cold_start` already do,
so an upgrade changes nothing on any live server until somebody sets an override. Only the PROFILE
and ROOT FOLDER are per row — URL and API key stay global, because the case this serves is one Radarr
filing a kids row into /data/Kids at a lower profile, not a second Radarr.

`request_candidates` gains `row_slug`: which row claimed the title. `RequestWhy.row` is the RENDERED
row name ("Because you watched Bluey"), carrying the person's display name and their own seed title,
so it identifies nothing stable. Without the slug, a title queued tonight and approved next month has
no way to resolve which row's target to send it under. NULL on every existing row, and the approve
path falls back to the global config for those — which is what they were queued under anyway.

Deliberately NOT added: `max_per_run`, the rating source and its API key. Those are the run's ceiling
and its one MDBList account; a row that could raise either would make the owner's global setting a
suggestion rather than a limit.

Revision ID: 0074
Revises: 0073
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

# (column, type) — every one nullable with no server default, so NULL reads as "inherit".
_COLLECTION_COLUMNS = (
    ("req_min_rating", sa.Float()),
    ("req_min_votes", sa.Integer()),
    ("req_min_demand", sa.Integer()),
    ("req_min_year", sa.Integer()),
    ("req_max_year", sa.Integer()),
    ("req_auto_send", sa.Boolean()),
    ("req_auto_min_demand", sa.Integer()),
    ("req_auto_min_rating", sa.Float()),
    ("req_max_per_row", sa.Integer()),
    ("req_radarr_quality_profile_id", sa.Integer()),
    ("req_radarr_root_folder", sa.String(length=512)),
    ("req_sonarr_quality_profile_id", sa.Integer()),
    ("req_sonarr_root_folder", sa.String(length=512)),
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind, "collections")
    for name, type_ in _COLLECTION_COLUMNS:
        if name not in existing:
            op.add_column("collections", sa.Column(name, type_, nullable=True))
    if "row_slug" not in _columns(bind, "request_candidates"):
        op.add_column("request_candidates", sa.Column("row_slug", sa.String(length=255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind, "collections")
    for name, _type in _COLLECTION_COLUMNS:
        if name in existing:
            op.drop_column("collections", name)
    if "row_slug" in _columns(bind, "request_candidates"):
        op.drop_column("request_candidates", "row_slug")
