"""Record the settings a pick was built under, so changing one rebuilds the row

Freshness is a cadence that suppresses churn when nothing has changed. It was also, unintentionally,
delaying changes the owner made on purpose: nothing anywhere recorded which settings a row's picks
were chosen under, so a deliberate edit — "Recent releases", the watched cap, the sources — waited
behind the same cadence as an accidental one. On a real server, raising "Recent releases" left 36 of
42 rows redelivering byte-identical picks, for up to a fortnight. From the outside that is
indistinguishable from the setting not working.

This column stores `rows.row_recipe(...)`: a fingerprint of the settings that decide row CONTENTS
(media, libraries, sources, recency, watched cap, rewatch, unstarted-only, seed budget, seed window,
cold-start). The next run compares it and rebuilds the row on a mismatch, whatever the cadence says.

Deliberately NOT fingerprinted: `pick_order` (presentation — reordering must not force a rebuild),
`freshness` itself (changing the cadence must not trigger the rebuild the cadence schedules), and
the row's name, poster and placement (they change how a row looks, never what is in it).

NULL on every pick written before this, which reads as "unknown" and does NOT force a rebuild —
rebuilding every row on every server at upgrade is precisely the churn freshness exists to prevent.
Rows adopt the mechanism naturally as each next refresh writes a recipe.
"""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("picks")}
    if "recipe" not in columns:
        op.add_column("picks", sa.Column("recipe", sa.String(length=128), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("picks")}
    if "recipe" in columns:
        op.drop_column("picks", "recipe")
