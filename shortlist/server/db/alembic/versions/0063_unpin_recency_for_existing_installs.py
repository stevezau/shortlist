"""Give existing installs the raised "Recent releases" default too — undo 0062's pin

0062 wrote an explicit `recommendations.recency` of 0.0 for every install already in use, so raising
the shipped default to 0.5 could not re-rank a running server. The owner chose the opposite
(2026-08-11): age-blind ranking is the behaviour the setting exists to correct, so every server
should get the corrected default and opt OUT with the slider rather than opt in.

This removes that pin. A server upgrading straight through both migrations writes it and then drops
it, ending exactly where a fresh install starts. 0062 is deliberately left in the chain rather than
deleted — a database that already stamped it could not migrate if the revision disappeared.

WHAT THIS CHANGES ON A RUNNING SERVER: every row's CONTENTS shift towards newer titles on its next
refresh night (staggered by each row's freshness, not all at once). Nothing about privacy, share
filters or delivery changes, and sliding Settings -> Finding titles -> Recent releases back to 0
restores the old ranking exactly.

Only a value of exactly 0.0 is removed, and only when the row is one 0062 could have written. That
cannot be told apart from a `:dev` user who deliberately chose 0 in the ~1h window between the two
releases, so such a choice is reset to the default here; the window is small, the setting is one
slider away, and the alternative is leaving genuinely pinned servers stuck for ever. A non-zero
stored value is never touched.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

_KEY = "recommendations.recency"


def upgrade() -> None:
    bind = op.get_bind()
    if "settings" not in sa.inspect(bind).get_table_names():
        return

    raw = bind.execute(sa.text("SELECT value FROM settings WHERE key = :k"), {"k": _KEY}).scalar()
    if raw is None:
        return
    try:
        # `{"v": ...}` is the envelope every SettingsStore write uses; anything else already reads
        # as unset, so leaving it alone changes nothing.
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return
    if isinstance(parsed, dict) and parsed.get("v") == 0.0:
        op.execute(sa.text("DELETE FROM settings WHERE key = :k").bindparams(k=_KEY))


def downgrade() -> None:
    """No-op. Re-writing the pin would hand back age-blind ranking to a server that has since been
    running on the corrected default, which is a behaviour change no downgrade should make silently."""
