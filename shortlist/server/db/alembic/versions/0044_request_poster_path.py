"""Add poster_path to request candidates

The Requests inbox is a decision about a film or show, and it was a wall of text: no artwork, so
every title had to be judged from its name and a rating. This stores TMDB's poster path ("/abc.jpg")
per candidate; the UI builds the image URL from it.

Path, not a URL: TMDB's image host and the size buckets are TMDB's to change, and storing a full URL
would bake today's CDN hostname into every row. Existing rows backfill on the next run that
re-surfaces the title — the column is empty-string default, and the inbox shows a placeholder tile
for a title with no artwork, so nothing breaks in the meantime.
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("request_candidates")}
    if "poster_path" not in columns:
        op.add_column(
            "request_candidates",
            sa.Column("poster_path", sa.String(255), server_default="", nullable=False),
        )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("request_candidates")}
    if "poster_path" in columns:
        op.drop_column("request_candidates", "poster_path")
