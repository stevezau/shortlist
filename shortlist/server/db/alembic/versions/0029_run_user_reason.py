"""run_users.reason — why a skipped user/row was skipped

A `skipped` result carried no explanation anywhere the owner could see it: the reason existed only
in the server log, and `error` could not be borrowed for it because the UI counts every non-null
`error` as a FAILED user. A beta user hit this with a shared row on a one-user server — the row can
never reach its 2-watcher floor — and reasonably read the bare "Skipped" as a bug (issue #3).

Numbered 0029, not 0002: revisions 0002-0028 are RESERVED — they are the pre-release migrations that
were squashed into 0001, and `_heal_squashed_revision` re-stamps any DB carrying one of them back to
the 0001 baseline. A migration numbered inside that range is therefore un-stamped and replayed on
every single boot. Post-baseline migrations start at 0029.

Idempotent like 0001: SQLite auto-commits DDL, so a crash mid-migration must leave a state a re-run
can finish rather than fail on "duplicate column".
"""

import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0001"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "run_users", "reason"):
        op.add_column("run_users", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "run_users", "reason"):
        op.drop_column("run_users", "reason")
