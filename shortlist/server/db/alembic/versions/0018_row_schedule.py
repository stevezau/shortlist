"""per-row run schedule

Adds ``schedule`` (a cron string, default "") to ``collections``: each row runs on its own cron, or
not at all — replacing the single global ``schedule.cron`` setting. Existing rows are backfilled with
whatever the global cron was (or the 03:30 default), so scheduled runs continue unchanged on upgrade;
clearing a row's schedule later ("") opts that row out. Idempotent like the migrations before it.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_DEFAULT_CRON = "30 3 * * *"


def upgrade() -> None:
    bind = op.get_bind()
    cols = {c["name"] for c in sa.inspect(bind).get_columns("collections")}
    if "schedule" not in cols:
        op.add_column("collections", sa.Column("schedule", sa.String(64), nullable=False, server_default=""))

    # Preserve current behaviour: seed every existing row with the old global cron so nothing stops
    # running. The settings value is JSON-encoded (SQLAlchemy JSON column); absent -> the 03:30 default.
    cron = _DEFAULT_CRON
    row = bind.execute(sa.text("SELECT value FROM settings WHERE key = 'schedule.cron'")).fetchone()
    if row is not None and row[0] is not None:
        try:
            parsed = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if isinstance(parsed, str) and parsed.strip():
                cron = parsed.strip()
        except (ValueError, TypeError):
            pass
    bind.execute(sa.text("UPDATE collections SET schedule = :cron WHERE schedule = ''"), {"cron": cron})


def downgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    with op.batch_alter_table("collections") as batch:
        if "schedule" in cols:
            batch.drop_column("schedule")
