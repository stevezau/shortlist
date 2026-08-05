"""The true watch date for a title transferred from another account

Plex cannot backdate a watch. Marking a title played is a scrobble, and the PMS stamps a scrobble
`now` — there is no documented way to set another account's `lastViewedAt`. So transferring an
owner's history onto a new watching account leaves the server believing every one of those titles
was watched today, and the next watch sync writes exactly that into `watched_titles.viewed_at`.

That would be quietly destructive rather than merely untidy: Shortlist picks seeds from the MOST
RECENT watches, so a set where every row shares one timestamp orders arbitrarily, and the new
account's recommendations come out of a coin toss. The migration that gives them their history back
would be the one that broke their picks.

`source_viewed_at` is the escape: the transfer writes the real date here, the watch sync neither
overwrites nor deletes a row that has one, and every "how recently?" read prefers it. NULL — the
value every existing row gets, and every row Plex reports directly — means "viewed_at is the truth",
so nothing changes for anyone who never runs a transfer.
"""

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("watched_titles")}
    if "source_viewed_at" not in columns:
        op.add_column("watched_titles", sa.Column("source_viewed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("watched_titles")}
    if "source_viewed_at" in columns:
        op.drop_column("watched_titles", "source_viewed_at")
