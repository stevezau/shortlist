"""Record when a run actually STARTED, separately from when it was queued.

`runs.started_at` is stamped by the column default at INSERT — when the run was ASKED for. A run that
sat in the queue for nine minutes and was then cancelled without ever executing therefore reported a
nine-minute duration on the Runs page, having done nothing at all (reported on SFLIX, 2026-08-13:
three runs queued together and cancelled, each claiming "9m 26s"). Duration has to be measured from
the moment the engine began, and a run that never got there has no duration to show.

NULL means "never ran", and the UI renders it as exactly that — so every row that DID run must be
given a value here, or the whole existing history reads "never ran" the moment this deploys. That is
the same error as the bug being fixed, pointed the other way and applied to everything.

Backfilled from `started_at` for `ok`/`error` runs only. Those two statuses are written solely after
execution has begun (`run_persistence.persist_report`, `RunService._mark_run_error` — both inside the
try that starts once the writer lock is held), so their old queued-to-finished duration is at worst
slightly generous by the queue wait, and is certainly not a claim that they never ran. `aborted` is
the one ambiguous status — it covers both "cancelled while queued" and "cancelled mid-run" — and it
is left NULL because the reported bug IS an aborted run, and over-claiming there is what we came to
fix.

Re-runnable, per `tests/integration/test_migration_recovery.py`: a crash between the DDL and the
version stamp replays this revision, and a bare `add_column` would then fail with "duplicate column
name" and wedge every later upgrade.

Revision ID: 0068
Revises: 0067
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {row[1] for row in bind.execute(sa.text("PRAGMA table_info(runs)"))}


def upgrade() -> None:
    bind = op.get_bind()
    if "began_at" not in _columns(bind):
        op.add_column("runs", sa.Column("began_at", sa.DateTime(timezone=True), nullable=True))
    # Re-runnable alongside the guard above: `WHERE began_at IS NULL` makes a replay a no-op, and it
    # can never overwrite a stamp a real run wrote.
    bind.execute(sa.text("UPDATE runs SET began_at = started_at WHERE began_at IS NULL AND status IN ('ok', 'error')"))


def downgrade() -> None:
    bind = op.get_bind()
    if "began_at" in _columns(bind):
        op.drop_column("runs", "began_at")
