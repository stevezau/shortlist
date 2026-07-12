"""picks.media_type — a TMDB id alone does not identify a title

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-12

TMDB ids are unique only WITHIN a namespace: movie 550 and TV 550 are different titles. The
staleness guard reads recent picks back by id, so without the media type a film silently
suppressed the show that shared its number for the next N runs. It is also what tells delivery
which library a pick belongs in — and a pick delivered to the wrong library produces a
collection Plex cannot hide from anyone.

Existing rows are backfilled as "movie" because nothing in the table can tell us otherwise —
and some of them really are shows (that is the bug this column exists to prevent). The only
consequence is that a mislabeled title becomes eligible for the row again one run early, which
is a freshness wobble, not a correctness problem: the next run records the true type.
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("picks", sa.Column("media_type", sa.String(16), nullable=False, server_default="movie"))


def downgrade() -> None:
    with op.batch_alter_table("picks") as batch:  # SQLite drops columns only via a table rebuild
        batch.drop_column("media_type")
