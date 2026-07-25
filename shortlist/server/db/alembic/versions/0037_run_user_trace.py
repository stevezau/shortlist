"""Record a per-user pipeline trace on each run

Answering "why did this person get these picks?" — or "what did the AI actually search for?" — meant
reading the container logs or the ranking code. The run already stored the final picks and per-row
breakdown, but not the reasoning that produced them: the seeds derived from history, what each
candidate source queried and returned, and the exact web-search / RAG prompts.

`trace` is a JSON blob mirroring `breakdown` — one per (run, user), pruned with the run by the same
retention. Existing rows default to `{}` (no trace recorded), which the UI renders as "not available
for this run" rather than an error.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("run_users")}
    if "trace" not in columns:
        op.add_column("run_users", sa.Column("trace", sa.JSON(), nullable=False, server_default="{}"))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("run_users")}
    if "trace" in columns:
        op.drop_column("run_users", "trace")
