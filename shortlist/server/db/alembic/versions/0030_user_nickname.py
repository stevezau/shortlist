"""users.nickname + users.friendly_name — what to call someone in a row title

Plex usernames are often an email or a handle nobody uses ("mrjohnpoz"), and `{user}` put that
straight onto a Home screen. `nickname` is the owner's override; `friendly_name` mirrors Tautulli's,
refreshed on each sync. Display order is nickname → friendly_name → plex username.

Deliberately NOT the slug: labels are built from the slug, and moving those would strand the
`label!=shortlist_<slug>` exclusions that keep each row private.

Numbered from 0029 upward — 0002-0028 are reserved for the squashed pre-release revisions, and
anything inside that range is re-stamped backward and replayed on every boot.
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_COLUMNS = ("nickname", "friendly_name")


def _existing(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("users")}


def upgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    for column in _COLUMNS:
        if column not in have:
            op.add_column("users", sa.Column(column, sa.String(length=255), nullable=False, server_default=""))


def downgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    for column in _COLUMNS:
        if column in have:
            op.drop_column("users", column)
