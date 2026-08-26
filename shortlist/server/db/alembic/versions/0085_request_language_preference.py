"""Language preference for requests: per-row overrides, and the language of a queued title.

The request pool is by definition what the library LACKS, and an English-heavy library has already
absorbed the popular English titles — so what is left missing skews non-English before any floor is
applied, and the rating floor then favours it further (TMDB's audience rates anime and K-drama
generously). The global choice lives in `settings` and needs no migration. This adds the two things
that do:

* `collections.req_language_mode` / `req_preferred_languages` / `req_min_rating_other` — the per-row
  overrides, beside the `req_*` floors that row already has. A kids row can be English-only while an
  anime row stays on "any".
* `request_candidates.language` — so the inbox can show WHICH language a held-back title is in. A
  reason that names the rule ("below the bar for other languages") without naming the fact is not an
  explanation.

All NULLable/defaulted with no backfill: NULL means "inherit the global" on the row columns, exactly
as every `req_*` column beside them already does, and the global itself defaults to "any" — today's
behaviour — until somebody picks another mode. `request_candidates.language` takes "" (unknown), which
is what a pre-existing queued row genuinely is: nothing recorded it at the time.

`req_preferred_languages` is JSON, not a delimited string, so an empty LIST stays distinct from NULL —
[] is a row that cleared its languages, NULL is a row that inherits the owner's. Collapsing those two
would silently change what a row requests.

Revision ID: 0085
Revises: 0084
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

_ROW_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("req_language_mode", sa.String(length=16)),
    ("req_preferred_languages", sa.JSON()),
    ("req_min_rating_other", sa.Float()),
)


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    existing = _columns(bind, "collections")
    for name, type_ in _ROW_COLUMNS:
        if name not in existing:
            op.add_column("collections", sa.Column(name, type_, nullable=True))

    if "language" not in _columns(bind, "request_candidates"):
        op.add_column(
            "request_candidates",
            sa.Column("language", sa.String(length=16), nullable=False, server_default=""),
        )


def downgrade() -> None:
    bind = op.get_bind()

    existing = _columns(bind, "collections")
    for name, _type in _ROW_COLUMNS:
        if name in existing:
            op.drop_column("collections", name)

    if "language" in _columns(bind, "request_candidates"):
        op.drop_column("request_candidates", "language")
