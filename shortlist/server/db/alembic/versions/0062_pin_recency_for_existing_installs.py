"""Raise the "Recent releases" default to 0.5 for NEW installs, pin 0.0 for existing ones

0061 shipped `recommendations.recency` defaulting to 0.0 — age-blind ranking, exactly as it was
before the setting existed. That is the *status quo* default, not a neutral one: a library holds
decades of catalog against a trickle of new releases, and TMDB's similar-lists lean to established
titles, so a ranker with no opinion about age returns whatever the pool's own skew gives it. The
symptom that prompted the feature (rows full of 1990s and 2000s titles) came from a ranker that was
never biased toward old titles — only blind to them.

So the shipped default moves to 0.5 ("leans towards recent releases", the phrasing the UI already
uses for that value). But `SettingsStore.get` falls back to `DEFAULTS` whenever no row exists, and
no install has a row for a key this new — so raising it alone would re-rank every row on every
server already running, the first night after a point release nobody read the notes for.

This writes an explicit 0.0 for servers ALREADY IN USE, so they keep exactly the rows they have and
the change is opt-in there via the slider. New installs have no row and pick up 0.5.

"Already in use" is `setup.completed`, not merely "the database exists": a fresh install runs every
migration too, so an unconditional write would pin the new default out of existence. An install that
never finished the wizard has built no rows and has no behaviour to preserve, so it counts as fresh.

Idempotent, and never overwrites a choice: a row that is already present (someone on :dev who moved
the slider) is left exactly as it is. No downgrade — an explicit 0.0 is a legitimate value that
0061's schema holds perfectly well, and removing it would silently change behaviour again.
"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

_KEY = "recommendations.recency"


def _stored(bind, key: str):
    """A setting's real value, unwrapping the ``{"v": ...}`` envelope every write uses.

    `SettingsStore` stores `{"v": <value>}` as JSON and treats any other shape as unset. Reading or
    writing the bare value here would be silently wrong in both directions: this migration would
    misread `setup.completed` (whose stored text is `{"v": true}`, not `true`) and would write a pin
    that `_unwrap` rejects — logging "unreadable value", falling back to the raised default, and
    re-ranking the very servers this exists to protect, while appearing to have worked.
    """
    raw = bind.execute(sa.text("SELECT value FROM settings WHERE key = :k"), {"k": key}).scalar()
    if raw is None:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return parsed.get("v") if isinstance(parsed, dict) else None


def upgrade() -> None:
    bind = op.get_bind()
    if "settings" not in sa.inspect(bind).get_table_names():
        return  # nothing to preserve on a database without the table yet

    present = bind.execute(sa.text("SELECT 1 FROM settings WHERE key = :k"), {"k": _KEY}).scalar()
    if present:
        return  # an explicit choice (the slider, or an earlier run of this migration) — never touch it

    if not _stored(bind, "setup.completed"):
        return  # fresh, or never finished the wizard: no built rows, so no behaviour to preserve

    # `updated_at` is NOT NULL with a PYTHON-side default (`default=utcnow`), which SQLAlchemy applies
    # on ORM inserts only — raw SQL in a migration bypasses it and hits the constraint.
    op.execute(
        sa.text("INSERT INTO settings (key, value, updated_at) VALUES (:k, :v, :t)").bindparams(
            k=_KEY, v=json.dumps({"v": 0.0}), t=datetime.now(UTC)
        )
    )


def downgrade() -> None:
    """Deliberately a no-op — see the module docstring. Removing the pin would let an existing
    server fall back to the raised default, which is the exact surprise this migration prevents."""
