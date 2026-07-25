"""Clear the dead curation-recipe settings and the cut llm_library source

The LLM no longer ranks a fixed candidate pool (the `curate` step) — code does the diversification
and genre-template reasons instead — and the `llm_library` source (LLM-picked-from-your-library) was
cut with it. Both leave stored values behind that nothing reads anymore:

  * settings `curator.prompt_tone` / `curator.prompt_guidance` / `curator.prompt_template` — the
    global curation recipe. Gone.
  * `llm_library` inside the settings `candidates.sources` list and inside every row's
    `collections.candidate_sources` array. The source no longer exists in `KNOWN_SOURCES`, so a
    leftover would fail the settings validator on the next save and be silently dropped by the
    engine — clear it now so the value on disk matches what the app accepts.
  * the per-row / per-person curation recipe stored as JSON in `collections.prompt`,
    `collection_user_overrides.prompt`, and the `prompt_*` keys inside `users.prefs`. All dead.

DATA-ONLY. The `prompt` COLUMNS on `collections` and `collection_user_overrides` are left in place:
dropping a column on SQLite forces a full table rebuild, and `collections` carries inbound foreign
keys (audience, overrides, poster assets, picks) — not worth the risk for two now-empty JSON blobs.
A later migration can remove the columns if they ever matter. `curator.provider` / `curator.model` /
`curator.api_key` / the base URLs are UNTOUCHED — the provider still does web-search title discovery.

Idempotent: re-running finds nothing to change. No downgrade — the deleted recipe values cannot be
reconstructed, and leaving them absent is harmless (the app treats a missing setting as its default).
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

_DEAD_SETTINGS = ("curator.prompt_tone", "curator.prompt_guidance", "curator.prompt_template")
_DEAD_PREFS = ("prompt_tone", "prompt_guidance", "prompt_template")
_CUT_SOURCE = "llm_library"


def _get(bind, key: str):
    """A setting's value, unwrapped from the `{"v": ...}` envelope `SettingsStore` writes."""
    raw = bind.execute(sa.text("select value from settings where key = :k"), {"k": key}).scalar()
    if raw is None:
        return None
    value = json.loads(raw) if isinstance(raw, str) else raw
    return value.get("v") if isinstance(value, dict) and "v" in value else value


def _set(bind, key: str, value) -> None:
    bind.execute(
        sa.text("update settings set value = :v, updated_at = CURRENT_TIMESTAMP where key = :k"),
        {"k": key, "v": json.dumps({"v": value})},
    )


def _loads(raw) -> object:
    """A JSON column value as Python. The column may hand back a decoded object or a raw string
    depending on the driver/dialect, so handle both."""
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


def upgrade() -> None:
    bind = op.get_bind()

    # 1) The global curation recipe settings are gone entirely.
    bind.execute(
        sa.text("delete from settings where key in :keys").bindparams(sa.bindparam("keys", expanding=True)),
        {"keys": list(_DEAD_SETTINGS)},
    )

    # 2) Drop the cut source from the settings candidate list (an envelope-wrapped JSON array).
    sources = _get(bind, "candidates.sources")
    if isinstance(sources, list) and _CUT_SOURCE in sources:
        _set(bind, "candidates.sources", [s for s in sources if s != _CUT_SOURCE])

    # 3) Drop the cut source from every row's own candidate list (a raw JSON column).
    for cid, raw in bind.execute(sa.text("select id, candidate_sources from collections")).all():
        value = _loads(raw)
        if isinstance(value, list) and _CUT_SOURCE in value:
            cleaned = [s for s in value if s != _CUT_SOURCE]
            bind.execute(
                sa.text("update collections set candidate_sources = :v where id = :id"),
                {"id": cid, "v": json.dumps(cleaned)},
            )

    # 4) Empty the dead per-row / per-person curation recipes (raw JSON columns, {} == "no recipe").
    for table in ("collections", "collection_user_overrides"):
        bind.execute(sa.text(f"update {table} set prompt = '{{}}' where prompt is not null and prompt != '{{}}'"))

    # 5) Strip the dead prompt_* keys out of each user's prefs blob, leaving the rest byte-for-byte.
    for uid, raw in bind.execute(sa.text("select id, prefs from users")).all():
        prefs = _loads(raw)
        if isinstance(prefs, dict) and any(k in prefs for k in _DEAD_PREFS):
            cleaned = {k: v for k, v in prefs.items() if k not in _DEAD_PREFS}
            bind.execute(sa.text("update users set prefs = :v where id = :id"), {"id": uid, "v": json.dumps(cleaned)})


def downgrade() -> None:
    """No-op: the cleared recipe values cannot be reconstructed, and their absence is the app's
    default state (a missing setting reads as its default; an empty prompt means 'no recipe')."""
