"""History mix: how many long-time favourites and older watches the AI web search looks up (#152).

`favourite_count` and `older_count` sit beside `recent_count` on a row (`collections`) and on one person's
tweaks to a row (`collection_user_overrides`). NULL, the value every existing row gets, means "inherit the
row's own, then the `recommendations.*` setting", whose default of 0 searches neither - so nothing changes
until someone sets a number.
"""

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

_TABLES = ("collections", "collection_user_overrides")
_COLUMNS = ("favourite_count", "older_count")


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    for table in _TABLES:
        present = _columns(table)
        for name in _COLUMNS:
            if name not in present:
                op.add_column(table, sa.Column(name, sa.Integer(), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        present = _columns(table)
        for name in _COLUMNS:
            if name in present:
                op.drop_column(table, name)
