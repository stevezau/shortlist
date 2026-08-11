"""Retire the "auto" web-search backend, pinning every install to the backend it was actually using

`llm_web.search_provider` had four values: native, exa, searxng and auto — and auto was the default,
so it is what almost every install stores. Auto meant "the AI provider's own search UNIONED with
whichever external is configured", which is a real behaviour but one the word "auto" does not
describe. Owners could not tell what it was doing, which is the whole reason it is going.

Each install is pinned to a single explicit backend, chosen to change as little as possible:

* a configured SearXNG wins (it was auto's own tie-break, and it is the free one);
* else a configured Exa key;
* else `native`, which is also the new default for fresh installs.

Preferring a configured EXTERNAL over `native` is deliberate. Setting up an Exa key or a SearXNG
instance is an act of intent, and under auto that backend WAS running — mapping to `native` would
silently stop using a service the owner had gone out of their way to configure (and, for Exa, still
pays for). A native-capable provider keeps its own search available either way; it just becomes a
choice rather than a silent addition.

**A server that had BOTH does lose the combined search.** Auto ran the provider's own tool and the
external one together, and their results barely overlap, so the candidate pool for those servers
narrows. That is the accepted cost of the option going away (owner decision); such a server can widen
it again by raising the row's seed count, or by switching backend if the other one suits it better.

Rows re-pick on their own refresh night, so this reaches a row when it next rebuilds rather than
rewriting anything on Plex at upgrade.
"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

_KEY = "llm_web.search_provider"


def _setting(conn, key: str) -> str:
    """A settings value as plain text, or "" when unset.

    Values are stored JSON-wrapped (`{"v": ...}`); a non-string or unparseable row reads as unset,
    which for the two credential keys here simply means "not configured".
    """
    row = conn.execute(sa.text("SELECT value FROM settings WHERE key = :k"), {"k": key}).scalar()
    if not row:
        return ""
    try:
        value = json.loads(row).get("v")
    except (ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""


def upgrade() -> None:
    conn = op.get_bind()
    if _setting(conn, _KEY) not in ("auto", ""):
        return  # already pinned to an explicit backend — nothing to decide
    # An unset row means the install was running on the old default, which was auto.
    if _setting(conn, "searxng.url").strip():
        chosen = "searxng"
    elif _setting(conn, "exa.apikey").strip():
        chosen = "exa"
    else:
        chosen = "native"

    if chosen == "native":
        # Same as the new default, so DELETE rather than pin: a migration that stashes a value the
        # owner never chose makes the default mean two different things on two servers. Removing a
        # literal "auto" row matters either way — left behind it is a value no validator accepts.
        conn.execute(sa.text("DELETE FROM settings WHERE key = :k"), {"k": _KEY})
        return
    # `updated_at` is NOT NULL with no server-side default (the ORM fills it in normally), so it has
    # to be written here — without it this INSERT raises on every real upgrade.
    conn.execute(
        sa.text(
            "INSERT INTO settings (key, value, updated_at) VALUES (:k, :v, :now) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
        ),
        {"k": _KEY, "v": json.dumps({"v": chosen}), "now": datetime.now(UTC).replace(tzinfo=None)},
    )


def downgrade() -> None:
    """Undo only what `upgrade` could have WRITTEN, never a choice it left alone.

    Removing the row unconditionally destroyed values this migration never touched: an install that
    had explicitly chosen `native` while holding an Exa key came back with no row, fell to 0062's
    `auto` default, and silently resumed Exa searches (and billing) on its next run.

    So only the two values upgrade can write are cleared, which returns those installs to 0062's
    `auto`. `native` is deliberately NOT cleared — it was a legal value on 0062 too, so an explicit
    one there is the owner's, not ours.
    """
    op.get_bind().execute(
        sa.text("DELETE FROM settings WHERE key = :k AND value IN (:exa, :searxng)"),
        {"k": _KEY, "exa": json.dumps({"v": "exa"}), "searxng": json.dumps({"v": "searxng"})},
    )
