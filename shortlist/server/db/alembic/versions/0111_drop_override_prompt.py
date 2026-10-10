"""Drop the dead `collection_user_overrides.prompt` column.

Migration 0036 emptied it when the curate feature went, and nothing has read or written it since.
(`collections.prompt` is a different, live column: the row's AI web search instructions.)
Downgrade re-adds it NOT NULL with an empty-object default so an older binary finds its schema.
"""

import sqlalchemy as sa
from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

_TABLE = "collection_user_overrides"


def _has_prompt() -> bool:
    return "prompt" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    if _has_prompt():
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_column("prompt")


def downgrade() -> None:
    if not _has_prompt():
        with op.batch_alter_table(_TABLE) as batch:
            batch.add_column(sa.Column("prompt", sa.JSON(), nullable=False, server_default="{}"))
