"""Index picks by time, so the dashboard can be windowed

The effectiveness report used to be lifetime-cumulative — every query scanned the whole `picks`
table, so no time index was needed. Windowing it (`?window=7|30|90|all`) puts `created_at` and
`watched_at` in the WHERE clause of a dozen aggregates instead, and `picks` is the one table in this
schema that grows without bound (~60 rows per person per row per run, kept forever as the impact
ledger).

Indexes only — no column or data changes, so this is safe to run and to reverse at any point.
"""

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_picks_watched_at", "watched_at"),
    ("ix_picks_created_at", "created_at"),
)


def upgrade() -> None:
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("picks")}
    for name, column in _INDEXES:
        if name not in existing:
            op.create_index(name, "picks", [column], unique=False)


def downgrade() -> None:
    existing = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("picks")}
    for name, _ in _INDEXES:
        if name in existing:
            op.drop_index(name, table_name="picks")
