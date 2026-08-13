"""Remove the Agregarr connection, and the credentials any install stored for it

Shortlist used to write its shelf order back into a co-managing agregarr so agregarr's own 30-minute
sync would stop undoing it. That integration is gone (owner decision 2026-08-13): it existed to keep
one third-party tool from fighting us over hub positions, and maintaining a client against a private,
unversioned API to do it was not worth the carry.

Nothing about a run changes for an install that never connected one. For an install that DID: the
Recommended shelf goes back to being contested — agregarr reorders it on its clock, Shortlist puts
its rows back on the next run — which is exactly the state the "Something else is reordering your
shelf" notification exists to report, and it will now fire on those servers. The alternative is
already in the UI: turn off "Let Shortlist order the Recommended shelf" and let the other tool own
the order outright. Rows are still built, delivered and kept private either way; only their position
on the shelf is affected.

Both settings rows are DELETED rather than left orphaned. `agregarr.apikey` is a credential held
Fernet-encrypted at rest, and retiring the feature that reads it must not leave someone else's API
key sitting in the database with no screen left to remove it from (plex-safety rule 9). `agregarr.url`
goes with it because a half-configured connection is not a state anything can still act on.

Historical `shelf.agregarr` and `run.agregarr_order` event rows are deliberately left alone. They are
the audit record of writes that really happened — "what changed on the shelf at 03:31" has to stay
answerable after the code that answered it is gone (rule 10), and rewriting history to match the
current feature set is the opposite of an audit.
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None

_KEYS = ("agregarr.url", "agregarr.apikey")


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM settings WHERE key IN (:url, :apikey)"),
        {"url": _KEYS[0], "apikey": _KEYS[1]},
    )


def downgrade() -> None:
    """A no-op, on purpose.

    Downgrading restores the code that reads these keys, but it cannot restore the values: the API
    key was encrypted with `/config/secret.key` and this migration destroyed the only copy. Writing
    empty rows back would be worse than nothing — `settings_store` treats blank as "not connected",
    which is what an absent row already means, so the connection would read as un-set up either way.
    Whoever downgrades re-enters the key on the Connections screen.
    """
