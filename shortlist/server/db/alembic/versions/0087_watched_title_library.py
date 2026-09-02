"""Record which Plex library each cached watch came from (issue #111).

`watched_titles` is unique on `(user, section_key, rating_key)` — one row per library COPY — so a
title held in two libraries has always been two rows, and the Watched page listed it twice with two
Block buttons that both send the same TMDB id. The page now groups those rows into one line and names
the libraries on it, which needs the library's display NAME, and only `section_key` was ever stored.

Same pair `picks` already carries (`section_key` + `library`). Non-null with a `""` default rather
than nullable: "" is exactly what the page means by "we don't know yet", and it keeps every read a
plain string comparison.

No backfill. The name lives on the PMS, not in this database, so there is nothing here to derive it
from — `watch_sync` fills it in on each person's next sync (nightly, and every run pre-fills). Until
then those rows carry "" and the page shows no library line for them rather than a guess.

Revision ID: 0087
Revises: 0086
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if "library" not in _columns(bind, "watched_titles"):
        op.add_column(
            "watched_titles",
            sa.Column("library", sa.String(255), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if "library" in _columns(bind, "watched_titles"):
        op.drop_column("watched_titles", "library")
