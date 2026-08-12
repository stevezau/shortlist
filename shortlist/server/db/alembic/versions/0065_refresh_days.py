"""Say the cadence in days instead of encoding it as a 0..1 "freshness" fraction

`recommendations.freshness` and `collections.freshness` held a 0..1 number that a curve in
`engine/rows.py` stretched onto 1..14 days: `round(1 + (1 - f) * 13)`, with 0.0 meaning "never refresh
once built". Three things were wrong with that, none of them the behaviour:

* The stored value described nothing. 0.55 meant "every 7 days", and no one could tell without running
  the curve. The UI already conceded this — it rendered "Refreshes about every 7 days" as helper text
  under a percent slider, translating the number back for the reader.
* The far end of the scale (14) was a constant duplicated in TypeScript, where it appeared as a bare
  `13` inline. Nothing pinned the two together, so the helper text could quote a cadence the engine
  was not running.
* Nothing slower than a fortnight could be expressed at all, because the fraction had to land inside
  the curve's range. A monthly row was simply not sayable.

So the stored number IS the cadence now: 0 = never once built, 1 = nightly, N = every N days.

BEHAVIOUR-PRESERVING. Every stored value is converted through the very curve the engine applied to it
last night, so each row keeps the cadence it already had — a server on the 0.5 default stays on 8
days, and one on 0.55 stays on 7. The global default moves 0.5 -> 8 for the same reason: 8 is what
0.5 has always meant, so an install that never set the value sees no change either.

The conversion is many-to-one (0.55 and 0.58 both meant 7 days), so the downgrade returns a
representative fraction rather than the exact original: up to 13 days it reproduces the same day
count, which is the only thing the engine ever read. Past that it cannot — the old scheme had no way
to say "every 30 days" — so it clamps to the slowest cadence 0064 CAN express (a fortnight) rather
than to 0.0, which in the old scheme meant "never rebuild" and would have frozen the row outright.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

# The curve this replaces, verbatim, so the conversion agrees with what the engine did last night.
_MAX_REFRESH_PERIOD_DAYS = 14


def _days_from_fraction(freshness: float) -> int:
    """The day count `engine.rows._refresh_period_days` produced for this freshness (0 = never)."""
    f = max(0.0, min(1.0, freshness))
    if f <= 0.0:
        return 0  # the caller's special case: frozen, never refreshed once built
    if f >= 1.0:
        return 1
    return max(1, round(1 + (1 - f) * (_MAX_REFRESH_PERIOD_DAYS - 1)))


# The slowest cadence the OLD scheme could express. Any fraction at or below this maps to a 14-day
# period; 0.0 does not — it is the old special case "never refresh once built".
_SLOWEST_EXPRESSIBLE_FRACTION = 0.03


def _fraction_from_days(days: int) -> float:
    """The freshness fraction that maps back to ``days`` — the downgrade's best inverse.

    Clamped to ``_SLOWEST_EXPRESSIBLE_FRACTION`` rather than to 0.0, because the two mean opposite
    things. The old scheme could not express anything slower than a fortnight — that limit is half of
    why this migration exists — so a 30-day cadence has no faithful pre-0065 value. The nearest one is
    "as slow as the old scheme goes" (14 days). Letting the arithmetic fall through to 0.0 instead
    would land on the old scheme's "NEVER refresh once built", silently freezing every row that used
    the new range. It did: `1 - (days-1)/13` is already 0.0 at days=14 and negative beyond it, so
    monthly and quarterly rows both downgraded to frozen.

    So a downgrade is behaviour-preserving only up to 13 days, and lossy-but-safe past it — it slows
    a row to a fortnight rather than stopping it dead. The upgrade is exact in both directions.
    """
    if days <= 0:
        return 0.0  # frozen in both schemes
    if days <= 1:
        return 1.0
    span = _MAX_REFRESH_PERIOD_DAYS - 1
    return max(_SLOWEST_EXPRESSIBLE_FRACTION, min(1.0, round(1 - (days - 1) / span, 4)))


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("collections")}


def _convert_setting(bind, to_days: bool) -> None:
    """Rewrite the global setting in place. Settings values are JSON blobs shaped ``{"v": ...}``."""
    import json

    old_key = "recommendations.freshness" if to_days else "recommendations.refresh_days"
    new_key = "recommendations.refresh_days" if to_days else "recommendations.freshness"
    row = bind.execute(sa.text("SELECT value FROM settings WHERE key = :k"), {"k": old_key}).fetchone()
    if row is None:
        return  # never set: the new default already means what the old one did
    try:
        stored = json.loads(row[0]).get("v")
        converted = _days_from_fraction(float(stored)) if to_days else _fraction_from_days(int(stored))
    except (TypeError, ValueError, AttributeError, json.JSONDecodeError):
        return  # unparseable: leave it and let the default stand rather than write a guess
    bind.execute(sa.text("DELETE FROM settings WHERE key = :k"), {"k": old_key})
    # `updated_at` is NOT NULL with no server-side default — the ORM fills it in, raw SQL must not
    # rely on that. Without it this INSERT raises on every database that has the old key set, which
    # is every real upgrade (verified against a copy of the production database, 2026-08-12). Same
    # trap 0063 hit; see the comment there.
    # Naive UTC, matching 0063 and every ORM write to this column: SQLAlchemy's SQLite DATETIME
    # drops the offset, so an aware value written through a raw bind stores a DIFFERENTLY SHAPED
    # string than the ORM's, and the first caller to compare the two would hit aware-vs-naive.
    bind.execute(
        sa.text("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (:k, :v, :now)"),
        {"k": new_key, "v": json.dumps({"v": converted}), "now": datetime.now(UTC).replace(tzinfo=None)},
    )


def upgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    if "refresh_days" not in columns:
        op.add_column("collections", sa.Column("refresh_days", sa.Integer(), nullable=True))
    if "freshness" in columns:
        rows = bind.execute(sa.text("SELECT id, freshness FROM collections WHERE freshness IS NOT NULL")).fetchall()
        for row_id, freshness in rows:
            bind.execute(
                sa.text("UPDATE collections SET refresh_days = :d WHERE id = :i"),
                {"d": _days_from_fraction(float(freshness)), "i": row_id},
            )
        op.drop_column("collections", "freshness")
    _convert_setting(bind, to_days=True)


def downgrade() -> None:
    bind = op.get_bind()
    columns = _columns(bind)
    if "freshness" not in columns:
        op.add_column("collections", sa.Column("freshness", sa.Float(), nullable=True))
    if "refresh_days" in columns:
        rows = bind.execute(
            sa.text("SELECT id, refresh_days FROM collections WHERE refresh_days IS NOT NULL")
        ).fetchall()
        for row_id, days in rows:
            bind.execute(
                sa.text("UPDATE collections SET freshness = :f WHERE id = :i"),
                {"f": _fraction_from_days(int(days)), "i": row_id},
            )
        op.drop_column("collections", "refresh_days")
    _convert_setting(bind, to_days=False)
