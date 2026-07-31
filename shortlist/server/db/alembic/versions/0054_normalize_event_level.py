"""Normalize events.level 'warn' -> 'warning'.

Five writers stamped `level="warn"` while every reader — the notification severities, the events API
consumer, and `notifications.py`'s own `info|warning|error` vocabulary — used the full word. Nothing
crashed; a filter for warnings simply returned none of them, which is the quiet kind of wrong.

The writers are fixed in code, but an operator's `events` table already holds the short form, and
those rows outlive several releases (retention prunes by age, not by schema). Rewriting them is what
makes the level column answer the same question for old rows and new ones.

Revision ID: 0054
Revises: 0053
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE events SET level = 'warning' WHERE level = 'warn'"))


def downgrade() -> None:
    op.execute(sa.text("UPDATE events SET level = 'warn' WHERE level = 'warning'"))
