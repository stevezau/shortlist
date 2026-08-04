"""Per-row cold-start behaviour: build the popular-titles fallback, or build nothing

Someone with too little watch history has always got a row of the server's highest-rated titles.
That is a reasonable default and a poor one for some rows: a `{top_seed}` row ("Because you
watched X") has no seed to name itself after, so it silently degrades to the bare default title
(issue #66). Owners also asked for the row simply not to exist until there is taste behind it.

NULL keeps the existing behaviour — every row already in the database inherits the global
`recommendations.cold_start`, which itself defaults to "popular". Upgrading removes nobody's row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    if "cold_start" not in columns:
        op.add_column("collections", sa.Column("cold_start", sa.String(length=16), nullable=True))


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("collections")}
    if "cold_start" in columns:
        op.drop_column("collections", "cold_start")
