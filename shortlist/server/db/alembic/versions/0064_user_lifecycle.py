"""Tell "they left the server" apart from "I switched them off", and let the owner file them away

`enabled=0` was carrying two unrelated meanings. The owner sets it to stop building someone a row;
the daily roster sweep sets it because plex.tv no longer lists that account at all. The Users list
rendered both identically, so a departed account became an unexplained row you could not act on —
and the excludes hiding their (now deleted) collection stayed in every other account's share filter
for ever, with nothing to prompt a clean-up.

Two nullable timestamps, because they answer different questions and either can be true alone:

* ``departed_at`` — FACT, owned by the sweep: plex.tv stopped listing this account. Cleared again the
  moment they reappear, so a re-invite (or a roster blip that slipped past the two guards in
  ``user_sync``) heals itself on the next sync rather than needing a hand.
* ``removed_at`` — INTENT, owned by the owner: they pressed Remove. Their picks and run history are
  dropped and the row leaves the Users list.

Neither DELETES the users row, and that is deliberate rather than a shortcut. ``restriction_snapshots``
is keyed to ``users.id`` with ``ON DELETE RESTRICT`` (migration 0055) and holds the one copy of that
person's share filters as they were BEFORE Shortlist touched them — what uninstall restores from
(plex-safety rule 2). Uninstall skips any snapshot whose user row has gone, so deleting the row would
silently cost that account its restore. Archiving keeps the anchor and hides the row.

Additive and nullable, so an existing database needs no backfill: every current row reads as "here and
not removed", which is exactly what it was.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("users")}


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    # Guarded individually: a half-applied upgrade (interrupted between the two adds) must be able to
    # finish rather than fail on the column it already created.
    with op.batch_alter_table("users") as batch:
        if "departed_at" not in existing:
            batch.add_column(sa.Column("departed_at", sa.DateTime(timezone=True), nullable=True))
        if "removed_at" not in existing:
            batch.add_column(sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    existing = _columns(bind)
    with op.batch_alter_table("users") as batch:
        if "removed_at" in existing:
            batch.drop_column("removed_at")
        if "departed_at" in existing:
            batch.drop_column("departed_at")
