"""Add overview (TMDB synopsis) to request candidates

The Requests inbox shows a title, a year, a rating and who wanted it — enough to judge a film you
already know, and nothing at all for one you don't. Discussion #87: sorting a pile of unfamiliar
titles meant opening TMDB in a tab per row. This stores TMDB's synopsis so the decision can be made
on the page.

Free where it matters: `overview` rides in the same TMDB list response `poster_path` already comes
from, so the common case costs no extra call — only a title a non-TMDB source surfaced (Trakt, the
web search) buys one, and it shares the cached detail request the poster lookup already makes.

Text, not a bounded String: TMDB publishes no length limit on the field, and a synopsis silently cut
off mid-sentence in the database is worse than a long one the UI clamps to three lines.

Existing rows backfill on the next run that re-surfaces the title (`_refresh_pending`), exactly as
0044 did for artwork. Until then the column is empty and the inbox omits the paragraph, so nothing
breaks in the meantime.

Revision ID: 0071
Revises: 0070
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("request_candidates")}


def upgrade() -> None:
    bind = op.get_bind()
    if "overview" not in _columns(bind):
        op.add_column(
            "request_candidates",
            sa.Column("overview", sa.Text(), server_default="", nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "overview" in _columns(bind):
        op.drop_column("request_candidates", "overview")
