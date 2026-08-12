"""Give a shared row a run record, and attribute a skipped person to the rows they were skipped for

Two additions, both for the same gap: a run page could not say what a run had actually DONE.

* ``run_shared_rows`` — a shared row is built once for the whole server and belongs to no user, so it
  could never have a ``run_users`` row. It had no record at all: ``persist_report`` files reports by
  user slug, a shared row's is ``shared_<slug>``, and the lookup miss ``continue``d — discarding its
  trace, breakdown, token spend and picks. On a live server, run #37 built a shared row of 40 picks
  and the page showed 46 skipped people and nothing else.
* ``run_users.rows_considered`` — ``reason`` is one sentence about the PERSON ("None of this person's
  rows were due to rebuild in this run"), which cannot be attributed to a row. Without it a
  rows-first view has nowhere to put a skipped user, and on a run where nothing was due that is
  everybody.

Both are additive, and neither backfills. Past runs keep exactly what they recorded: no shared-row
rows, and ``{}`` for ``rows_considered``. That is a real distinction the UI has to honour — "not
recorded" is not "nothing was considered", and rendering the second would put a confident lie on
every historical run.

``run_shared_rows.run_id`` cascades with the run, deliberately unlike ``run_users``, whose RESTRICT
protects an irreplaceable account record (migration 0055). There is no account on this key.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # Guarded individually: a half-applied upgrade (interrupted between the table and the column)
    # must be able to finish rather than fail on the half it already created.
    if not _has_table(bind, "run_shared_rows"):
        op.create_table(
            "run_shared_rows",
            sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("collection_slug", sa.String(length=255), primary_key=True),
            sa.Column("row_title", sa.String(length=512), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("llm_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("llm_tokens_by_step", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("exa_searches", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("diff", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("breakdown", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("trace", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("picks", sa.JSON(), nullable=False, server_default="[]"),
        )
    if "rows_considered" not in _columns(bind, "run_users"):
        with op.batch_alter_table("run_users") as batch:
            batch.add_column(sa.Column("rows_considered", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    bind = op.get_bind()
    if "rows_considered" in _columns(bind, "run_users"):
        with op.batch_alter_table("run_users") as batch:
            batch.drop_column("rows_considered")
    if _has_table(bind, "run_shared_rows"):
        op.drop_table("run_shared_rows")
