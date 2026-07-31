"""Two row settings that make two templates true: `rewatch` and `unstarted_only`

Both existed as templates in the gallery before the engine could honour them:

* **Happy to see again** promised a rewatch shelf, but `watched_pct` is a CEILING — the ranking shows
  unwatched titles first and merely PERMITS up to that fraction of finished ones, so even at 1.0 a
  library with plenty of unwatched candidates yields a mostly-unwatched row. `rewatch` inverts the
  preference: finished titles lead, unwatched only fill what's left.
* **More TV to watch** promised series to start, but the normal filter only drops shows a person has
  FINISHED — one they are three episodes into stayed eligible. `unstarted_only` drops any series with
  a single viewed episode.

Both default to FALSE, which is exactly today's behaviour, so every existing row is unchanged and no
backfill is needed. `server_default="0"` matters as well as the Python default: rows inserted by a
path that doesn't go through the ORM (or an older build mid-rollout) must still land false rather
than NULL, since the engine reads them with `bool()`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

_COLUMNS = ("rewatch", "unstarted_only")


def upgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    for name in _COLUMNS:
        if name not in existing:
            op.add_column(
                "collections",
                sa.Column(name, sa.Boolean(), nullable=False, server_default="0"),
            )


def downgrade() -> None:
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    for name in _COLUMNS:
        if name in existing:
            op.drop_column("collections", name)
