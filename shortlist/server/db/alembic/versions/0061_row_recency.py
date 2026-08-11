"""Per-row "Recent releases" weight: how much a title's release date counts when ranking it

Ranking had no age term at all — a title's score was seed frequency x rating x seed weight x
affinity — so a well-rated, highly-similar 1996 film beat a 2024 one every time and rows filled
with catalog titles. This column is the per-row strength of the release-date weight; the global
default it falls back to is `recommendations.recency`.

NULL is behaviour-preserving in two directions, which is why the column is nullable rather than
defaulting to 0.0. Every row already in the database reads NULL and follows the global (itself
shipped at 0.0, so nothing re-orders on upgrade), AND a row can still pin an explicit 0.0 to mean
"ignore release date on THIS row" on a server whose global is high — a Hidden Gems row beside a New
& Notable one. Collapsing the two states into a non-null 0.0 default would make raising the global
do nothing for every pre-existing row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    if "recency" not in columns:
        op.add_column("collections", sa.Column("recency", sa.Float(), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    if "recency" in columns:
        op.drop_column("collections", "recency")
