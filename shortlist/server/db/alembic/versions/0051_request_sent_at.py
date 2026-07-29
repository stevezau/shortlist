"""Record when a request was actually sent

`report.requests.watched_after_sent` — "we asked Sonarr/Radarr for this and someone then watched it"
— had no send timestamp to compare against, so it used `updated_at`. That column has `onupdate`, so
it moves in both wrong directions: clearing a months-old title from the Sent log bumps it and pulls
that request into a recent window, while any edit after the send pushes a genuine one out.

`sent_at` is stamped ONCE, on the transition to `status="sent"`, by both send paths (the run's
auto-send and the inbox's manual send). NULL on rows sent before this column existed; the report
falls back to `updated_at` for those, which is exactly as good as it used to be — so this migration
needs no backfill and changes nothing for existing data.
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("request_candidates")}
    if "sent_at" not in columns:
        op.add_column("request_candidates", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("request_candidates")}
    if "sent_at" in columns:
        op.drop_column("request_candidates", "sent_at")
