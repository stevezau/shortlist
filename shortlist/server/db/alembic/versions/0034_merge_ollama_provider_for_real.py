"""Actually fold the Ollama curator into the local/OpenAI-compatible one — 0032 never did

0032 was written against the wrong storage shape and so was a no-op on every real database.
`settings.value` is not the bare value: `SettingsStore.set` wraps it (`{"v": <value>}`) and
`get` unwraps it (`settings_store.py`). 0032 compared the whole envelope to the string
`"ollama"`, which never matched, and returned early every time. Worse, its writer would have
stored an UNWRAPPED string, so the next `row.value["v"]` in the app would have raised
`TypeError: string indices must be integers` for every setting read — fixing only the reader
would have turned a silent no-op into an outage.

Symptom this leaves behind on an instance configured for Ollama before the merge: the engine
keeps curating (the `"ollama"` aliases in `make_curator`/`curator_kwargs` still resolve), but the
Settings UI no longer has a card with that id, so the provider reads as blank/unrecognised and the
Server URL field — now bound to `curator.openai_base_url` — is empty while the real URL sits in
`curator.ollama_url`.

Reads both shapes because 0032 is stamped in the wild and, on a database seeded some other way,
may have written a bare string before this ran.
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def _get(bind, key: str):
    """The stored value, unwrapped from the `{"v": ...}` envelope `SettingsStore` writes."""
    raw = bind.execute(sa.text("select value from settings where key = :k"), {"k": key}).scalar()
    if raw is None:
        return None
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value.get("v") if isinstance(value, dict) and "v" in value else value


def _set(bind, key: str, value) -> None:
    bind.execute(
        sa.text(
            "insert into settings (key, value, updated_at) values (:k, :v, CURRENT_TIMESTAMP) "
            "on conflict(key) do update set value = :v, updated_at = CURRENT_TIMESTAMP"
        ),
        {"k": key, "v": json.dumps({"v": value})},
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _get(bind, "curator.provider") != "ollama":
        return
    url = (_get(bind, "curator.ollama_url") or "").strip().rstrip("/")
    if url and not url.endswith(("/v1", "/api/v1")):
        # The native Ollama API sat at the root; the OpenAI-compatible one it is moving to is at /v1.
        url = f"{url}/v1"
    _set(bind, "curator.provider", "openai_compatible")
    if url:
        _set(bind, "curator.openai_base_url", url)


def downgrade() -> None:
    """Best-effort: only an instance whose URL still looks like Ollama's default port goes back."""
    bind = op.get_bind()
    if _get(bind, "curator.provider") != "openai_compatible":
        return
    if ":11434" in (_get(bind, "curator.openai_base_url") or ""):
        _set(bind, "curator.provider", "ollama")
