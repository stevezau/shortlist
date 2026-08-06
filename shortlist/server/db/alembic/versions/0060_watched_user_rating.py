"""What each person rated a title in Plex, so a title they disliked stops seeding their row

Issue #69: someone watches a film, doesn't like it, and Shortlist keeps building their row out of
things similar to it. Plex already has their answer — `userRating` — and it arrives on the watched
read the sync already makes, scoped to the token it was read with (live-probed across 50 accounts on
a real server 2026-08-06: a title reading 6.2 for the owner returned no rating at all for all 49
viewers). Caching it here is what lets the engine see it without a second read.

NULL means "never rated", and that is almost every row — 0.27% of watched titles carried a rating on
the server this was measured against. So NULL must stay distinguishable from 0.0, which is a real
rating someone can give; the column is nullable and no backfill is attempted.

Existing rows get NULL and stay NULL until the next FULL watch re-read. That is not an oversight:
rating a title does not move its `lastViewedAt`, and the incremental read walks by that stamp, so an
incremental sync can never see a rating at all. `sync.watch_full_days` (default 7) is what refreshes
them, which is also the ceiling on how quickly a new rating changes anyone's row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("watched_titles")}
    if "user_rating" not in columns:
        op.add_column("watched_titles", sa.Column("user_rating", sa.Float(), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("watched_titles")}
    if "user_rating" in columns:
        op.drop_column("watched_titles", "user_rating")
