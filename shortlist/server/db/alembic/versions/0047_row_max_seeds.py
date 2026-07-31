"""Per-row seed budget: how many watched titles a row is built from

`max_seeds` was an engine constant (30) with no way to change it — not per row, not even globally.
Every row on the server was therefore derived from the same ~30 most recent watches, blended.

That is wrong for any row that names one title. A row whose template is `{top_seed}` renders
"Because you watched Chernobyl" and then fills itself from thirty other things the person watched,
so the title claims something the contents do not honour (issue #57). One seed makes the claim true.

NULL keeps the existing behaviour — every row already in the database is unchanged, and the engine
falls back to EngineConfig.max_seeds exactly as before.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    if "max_seeds" not in columns:
        op.add_column("collections", sa.Column("max_seeds", sa.Integer(), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    if "max_seeds" in columns:
        op.drop_column("collections", "max_seeds")
