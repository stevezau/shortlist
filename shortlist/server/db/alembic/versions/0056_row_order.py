"""Per-row pick ordering, and the two pick fields it sorts on

A row's picks have only ever been delivered in ranked order. Plex itself cannot help here: a regular
collection sorts by release date, alphabetically, or by the custom order we write — `sortUpdate`
accepts nothing else — and the "Randomly" sort Plex offers in its library view belongs to smart
collections, which Shortlist's rows cannot be (a smart collection matches a FILTER, so expressing
"these twenty titles" would mean labelling the library items themselves, and the share filters read
those same labels to hide rows — one person's exclude would then hide those films from another
person's whole library).

So every order is applied by us and delivered as the custom order Plex already honours. Verified
against a real PMS: the Home hub for a collection serves `/library/collections/<rk>/children`, and
that endpoint returns exactly the custom order written to it.

`collections.pick_order` is NOT NULL with a server default of "best" — the existing ranking — so every
row that exists keeps behaving exactly as it does today.

`picks.rating` / `picks.year` are nullable because they are unknowable for already-delivered rows.
A carried-forward pick is rebuilt from this table, so without them a row ordered by rating or year
would sort every surviving pick as unrated and undated. Existing rows sort those picks last, keeping
their relative order, until the row next rebuilds and fills the values in.
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Guarded like every other add-column migration here: `0001_initial` is the squashed baseline and
    # is kept in step with the models, so a DB healed from a pre-release revision replays this against
    # a schema that already has these columns.
    if "pick_order" not in _columns("collections"):
        op.add_column("collections", sa.Column("pick_order", sa.String(16), nullable=False, server_default="best"))
    pick_columns = _columns("picks")
    if "rating" not in pick_columns:
        op.add_column("picks", sa.Column("rating", sa.Float(), nullable=True))
    if "year" not in pick_columns:
        op.add_column("picks", sa.Column("year", sa.Integer(), nullable=True))


def downgrade() -> None:
    pick_columns = _columns("picks")
    if "year" in pick_columns:
        op.drop_column("picks", "year")
    if "rating" in pick_columns:
        op.drop_column("picks", "rating")
    if "pick_order" in _columns("collections"):
        op.drop_column("collections", "pick_order")
