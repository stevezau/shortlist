"""Record a managed user's Plex restriction PROFILE, not just that they are restricted

`/api/users` — the endpoint the roster sync reads — reports `restricted="1"` for every Plex Home
managed account and offers nothing to tell them apart. So Shortlist treated them all the same and
skipped their share-filter writes, which meant a managed user with NO age restriction never got the
`label!=` excludes that hide other people's rows. They could see everything (issue #20).

The distinction lives on a different endpoint, `/api/home/users`, as `restrictionProfile`:
"little_kid" | "older_kid" | "teen", or absent when none is set. It is the field that decides whether
a filter write is even possible — Plex's own documentation: "For Managed users, the restriction
profile must be set to None if you wish to edit Rating and Label restrictions on the library types."
(support.plex.tv/articles/204232573-restricting-the-shares/)

Live-confirmed against a real server, 2026-07-29: a `little_kid` account returns HTTP 422 on the
write AND sees zero collections of any kind, so skipping it is both necessary and harmless. An
account with no profile is an ordinary user that has simply never been given its excludes.

Empty-string default, backfilled on the next user sync. Until then every managed account reads as
"no profile", so the code ATTEMPTS the write and plex.tv gets the final say — a 422 is already
handled and never blocks promotion for anyone else (issue #14). Failing open is safe here precisely
because the refusal path already exists.
"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "restriction_profile" not in columns:
        op.add_column(
            "users",
            sa.Column("restriction_profile", sa.String(32), server_default="", nullable=False),
        )


def downgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "restriction_profile" in columns:
        op.drop_column("users", "restriction_profile")
