"""Make eleven columns NOT NULL, so production matches what the ORM claims

Migrations 0049 and 0050 created `run_log_lines`, `watched_titles` and `watch_sync_state` with
eleven columns nullable that the ORM declares NOT NULL. Nothing caught it because the test suite
builds its schema with `Base.metadata.create_all` — so the TEST database was stricter than every
real one, and a NULL production would happily accept was unreachable in a test.

Every writer goes through the ORM and supplies a value, so no real row should need the backfill
below. It runs anyway: a hand-edited database or a raw INSERT is exactly the case this constraint
exists to stop, and an ALTER that fails halfway through boot is worse than a defensive UPDATE.

Backfills, and why each is the honest value rather than merely a legal one:

* `run_log_lines.ts` — the parent run's `started_at`. A narration line without a timestamp happened
  during its run; "now" would date a three-month-old line to this boot.
* `run_log_lines.{user_slug,stage,counts,reason,level}` — the ORM's own defaults ('', {}, 'info').
* `watched_titles.title` — ''. `_upsert` already writes `item.title or ""`.
* `watched_titles.watch_count` — 1. The row exists because the title was watched, so one play is the
  truthful minimum and the ORM's default.
* `watched_titles.viewed_at` — `updated_at`, else the epoch. NOT "now": `get_history` orders by this
  column to choose a person's most recent watches as seeds, so a backfilled "now" would let a title
  nobody can date masquerade as tonight's watch and displace a real one. The epoch is already this
  codebase's "we don't know when" (`watch_cache._to_item`), and sorts such a row last.
* `watched_titles.updated_at` — now. It means "when did we last touch this row", and this migration
  is that moment.
* `watch_sync_state.item_count` — 0.

The downgrade only widens the columns again; it does not restore NULLs, because nothing records
which values were backfilled and a false NULL is worse than a true default.
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None

# column -> its declared type, per table. The type is required: SQLite cannot ALTER a column, so
# batch mode rebuilds the table and needs to know what it is rebuilding.
_TIGHTEN: dict[str, dict[str, sa.types.TypeEngine]] = {
    "run_log_lines": {
        "ts": sa.DateTime(timezone=True),
        "user_slug": sa.String(length=255),
        "stage": sa.String(length=64),
        "counts": sa.JSON(),
        "reason": sa.String(length=1024),
        "level": sa.String(length=8),
    },
    "watched_titles": {
        "title": sa.String(length=512),
        "watch_count": sa.Integer(),
        "viewed_at": sa.DateTime(timezone=True),
        "updated_at": sa.DateTime(timezone=True),
    },
    "watch_sync_state": {
        "item_count": sa.Integer(),
    },
}

_BACKFILLS = [
    # Orphaned narration cannot exist (run_id is CASCADE), but a missing run must not block the ALTER.
    "UPDATE run_log_lines SET ts = COALESCE("
    "(SELECT started_at FROM runs WHERE runs.id = run_log_lines.run_id), CURRENT_TIMESTAMP"
    ") WHERE ts IS NULL",
    "UPDATE run_log_lines SET user_slug = '' WHERE user_slug IS NULL",
    "UPDATE run_log_lines SET stage = '' WHERE stage IS NULL",
    "UPDATE run_log_lines SET counts = '{}' WHERE counts IS NULL",
    "UPDATE run_log_lines SET reason = '' WHERE reason IS NULL",
    "UPDATE run_log_lines SET level = 'info' WHERE level IS NULL",
    "UPDATE watched_titles SET title = '' WHERE title IS NULL",
    "UPDATE watched_titles SET watch_count = 1 WHERE watch_count IS NULL",
    "UPDATE watched_titles SET viewed_at = COALESCE(updated_at, '1970-01-01 00:00:00') WHERE viewed_at IS NULL",
    "UPDATE watched_titles SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL",
    "UPDATE watch_sync_state SET item_count = 0 WHERE item_count IS NULL",
]


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if not _TIGHTEN.keys() <= tables:
        return  # 0049/0050 never ran on this DB; their own create_table guards handle it

    for statement in _BACKFILLS:
        op.execute(statement)

    for table, columns in _TIGHTEN.items():
        with op.batch_alter_table(table) as batch_op:
            for name, type_ in columns.items():
                batch_op.alter_column(name, existing_type=type_, nullable=False)


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if not _TIGHTEN.keys() <= tables:
        return

    for table, columns in _TIGHTEN.items():
        with op.batch_alter_table(table) as batch_op:
            for name, type_ in columns.items():
                batch_op.alter_column(name, existing_type=type_, nullable=True)
