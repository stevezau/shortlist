"""Let a row cycle between several recent watches instead of sitting on the newest one

A `{top_seed}` ("Because you watched X") row has only ever been built from a person's single most
recent watch, so it names that watch until they finish something newer. Reported on issue #57 as
looking stuck: the row over-prescribes on one title, and someone who watches little for a fortnight
sees the same name for a fortnight.

`collections.seed_window` is how many of their most recent watches the row may be built from. One of
them is chosen per run — a CYCLE keyed on the run's day plus a stable per-(row, user) phase, not a
random pick, because random repeats and a repeat is indistinguishable from the bug this relieves.
It stays a single-seed row; only WHICH seed moves. Raising `max_seeds` instead would blend several
watches into one row and dilute the claim its title makes, which is the opposite of the ask.

NOT NULL with a server default of 1 — every row that exists keeps taking the most recent watch and
behaving exactly as it does today, and the feature is opt-in per row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Guarded like every other add-column migration here, so replaying it against a database that was
    # healed or created from the models directly is a no-op rather than a duplicate-column error.
    if "seed_window" not in _columns("collections"):
        op.add_column("collections", sa.Column("seed_window", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    if "seed_window" in _columns("collections"):
        op.drop_column("collections", "seed_window")
