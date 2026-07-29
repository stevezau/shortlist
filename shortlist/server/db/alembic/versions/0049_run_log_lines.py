"""Keep a run's activity feed

The feed lived only in a bounded in-memory deque for the last 10 runs and was wiped on restart, so
opening the log of any older run showed nothing at all. Persisting it makes a run's narration
answerable after the fact — which is the whole point of having one.

Deliberately its own table rather than `events`: that one is the audit trail ("what changed on whose
share at 03:31", plex-safety rule 10), with different retention and a scope-indexed query shape.
This is high-volume narration, only ever read as one run's chronological feed.

`ondelete=CASCADE` on run_id so retention pruning a run takes its narration with it — and, crucially,
NOTHING else: `picks` (the impact ledger) and `deliveries` (what actually exists on Plex) are keyed
by slug precisely so they survive a run's deletion.
"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "run_log_lines" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "run_log_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_slug", sa.String(length=255), nullable=True),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=True),
        sa.Column("reason", sa.String(length=1024), nullable=True),
        sa.Column("level", sa.String(length=8), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_log_line_seq"),
    )
    op.create_index(op.f("ix_run_log_lines_run_id"), "run_log_lines", ["run_id"], unique=False)


def downgrade() -> None:
    if "run_log_lines" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index(op.f("ix_run_log_lines_run_id"), table_name="run_log_lines")
    op.drop_table("run_log_lines")
