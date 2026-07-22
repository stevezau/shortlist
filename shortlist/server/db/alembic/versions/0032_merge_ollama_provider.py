"""Fold the Ollama curator into the one local/OpenAI-compatible provider — SUPERSEDED BY 0034

Kept as a no-op rather than deleted: it is already stamped on instances running `dev`, and removing
it would strand them at a revision alembic can no longer resolve.

It never worked. `settings.value` holds `{"v": <value>}` (`SettingsStore.set`), and this migration
compared the whole envelope to the bare string `"ollama"` — which never matched, so it returned early
on every real database. Its writer had the mirror-image bug (it stored the value UNWRAPPED), so
"fixing" the read alone would have made `row.value["v"]` raise for every setting the app reads.
Neutered here so that half-fix can never fire; 0034 does the migration correctly against the
envelope, and runs on the instances 0032 silently skipped.
"""

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """No-op — see the module docstring. 0034 carries the Ollama config over."""


def downgrade() -> None:
    """No-op — 0034 owns the reverse direction too."""
