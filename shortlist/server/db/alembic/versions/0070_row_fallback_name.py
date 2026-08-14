"""Give a row a name to use when its own name cannot be filled in.

A row titled `Because you watched {top_seed}` needs a pick that traces back to something the person
watched. Someone below the history threshold has none — their picks come from the cold-start fill
("Popular on this server") and carry no seed at all — so the title could not be rendered, and
`render_row_name` substituted the hardcoded English DEFAULT_ROW_NAME. Issue #84: on a 22-user server
whose row-name template is French, 19 of 22 people got "✨ Picked for You".

Shortlist no longer invents a name. Whoever asks for the row provides one, and a row with no name is
simply not built for that person — nothing is deleted, the collection they already have keeps its
label and stays hidden, it just stops being updated.

**Deliberately no backfill.** An earlier version of this migration copied the global row-name template
into every `{top_seed}` row, to keep those people's rows appearing. Every serious problem with this
change came from that one decision: it matched the wrong column (the Rows page stores a template in
`name`, not `name_template`), it wrote values `render_row_name` discards (when the global template
itself needs a seed), it could not see per-user overrides, and — worst — the global template IS the
default row's title, so it handed two of a person's rows the same name in the same library, which is
precisely the collision this whole change exists to prevent. Guessing on the operator's behalf was
the problem, not the implementation of the guess. The row editor asks them instead.

So: add the column, nothing else. Re-runnable because the add is guarded, per
`tests/integration/test_migration_recovery.py`.

Revision ID: 0070
Revises: 0069
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {row[1] for row in bind.execute(sa.text("PRAGMA table_info(collections)"))}


def upgrade() -> None:
    bind = op.get_bind()
    if "fallback_name" not in _columns(bind):
        op.add_column("collections", sa.Column("fallback_name", sa.String(255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "fallback_name" in _columns(bind):
        op.drop_column("collections", "fallback_name")
