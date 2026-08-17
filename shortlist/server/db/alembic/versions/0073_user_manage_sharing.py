"""Add users.manage_sharing — may Shortlist edit this person's Plex sharing settings?

Every account that can see the server gets `label!=shortlist_<other>` merged into its share filter,
whether or not Shortlist builds it a row. That is deliberate (a row is visible to anyone whose filter
does not exclude it), but it is wrong for one shape of account: one whose owner has set their OWN Plex
restrictions in a way our excludes conflict with. Reported in discussion #92 — a managed account with
an "allow only" label list, where the reporter wants Shortlist out of the Restrictions tab entirely.

Deliberately NOT a new value of `enabled`. Disabling someone means "no row for them" and still hides
everyone else's rows FROM them (`privacy.hide_shared_from_disabled` goes further and hides the public
shared rows too, and its UI copy promises exactly that). Folding "leave their sharing alone" into the
same switch would silently turn the strongest hiding state on the server into the weakest, for every
user anybody disabled months ago for an unrelated reason.

Defaults to 1: every existing account keeps being managed, so the migration changes no behaviour on
any live server until somebody turns it off for a named person.

Revision ID: 0073
Revises: 0072
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("users")}


def upgrade() -> None:
    bind = op.get_bind()
    if "manage_sharing" not in _columns(bind):
        op.add_column("users", sa.Column("manage_sharing", sa.Boolean(), server_default="1", nullable=False))


def downgrade() -> None:
    bind = op.get_bind()
    if "manage_sharing" in _columns(bind):
        op.drop_column("users", "manage_sharing")
