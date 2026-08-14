"""Give a row a name to use when its own name cannot be filled in.

A row titled `Because you watched {top_seed}` needs a pick that traces back to something the person
watched. People below the history threshold have none — their picks come from the cold-start fill
("Popular on this server") and carry no seed at all — so the title could not be rendered, and
`render_row_name` substituted the hardcoded English DEFAULT_ROW_NAME. Issue #84: on a 22-user server
with the row-name template set to French, 19 of 22 people got "✨ Picked for You".

Shortlist no longer invents a name. Whoever asks for the row provides one, and a row with no name is
not built for that person.

**Backfilled deliberately, and this is the whole point of the column being nullable-with-a-value
rather than just nullable.** Existing rows inherit `cold_start = NULL` -> the global setting ->
"popular", i.e. they have already chosen "build it for everyone". So they must be given a name, or
the new rule would delete rows on upgrade — which is exactly what an earlier attempt at this issue
did (reverted in 33ba725). Every `{top_seed}` row therefore inherits the operator's OWN global
row-name template, so nothing disappears and nothing is in a language they did not choose.

Rows whose template needs no seed are left NULL: they can always render, so they never reach a
fallback, and writing one would be inventing configuration the operator never asked for.

Re-runnable, per `tests/integration/test_migration_recovery.py`: a crash between the DDL and the
version stamp replays this revision, so both the add and the backfill are guarded.

Revision ID: 0070
Revises: 0069
"""

from __future__ import annotations

import json

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

    # The operator's own default row name, read from where the app keeps it. Empty (or absent) means
    # this instance has never set one, and there is nothing honest to backfill with — those rows keep
    # NULL and simply are not built for anyone who cannot be named, which is the new rule.
    #
    # `settings.value` is a JSON column holding SettingsStore's envelope — `{"v": "Picked for You"}`,
    # not the bare string. Reading it as text would write `{"v": ...}` onto everybody's Plex, and
    # reading it as a plain JSON string would find a dict, decide there was nothing to backfill, and
    # silently leave every existing row nameless — which under the new rule means not built.
    raw = bind.execute(sa.text("SELECT value FROM settings WHERE key = 'row.name_template'")).scalar()
    try:
        decoded = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        decoded = None
    global_name = decoded.get("v") if isinstance(decoded, dict) else decoded
    if not isinstance(global_name, str) or not global_name.strip():
        return

    # Only rows that can actually fail to render, and only where the operator has not already said
    # something. `IS NULL` rather than `= ''` on purpose: an empty string is a deliberate "no
    # fallback, skip these people", and a re-run of this migration must not overwrite it.
    bind.execute(
        sa.text(
            "UPDATE collections SET fallback_name = :name "
            "WHERE fallback_name IS NULL AND name_template LIKE '%{top_seed}%'"
        ),
        {"name": global_name},
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "fallback_name" in _columns(bind):
        op.drop_column("collections", "fallback_name")
