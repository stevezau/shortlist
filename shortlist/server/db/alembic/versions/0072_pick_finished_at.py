"""Add picks.finished_at — did they finish it, or just start it?

`picks.watched_at` is stamped from Plex's binary watched flag. For a MOVIE that flag is honest: it
means played. For a SERIES it flips on the first finished episode, so one episode of a 60-episode
show has always scored exactly like a whole film.

Measured on a real 47-user server on 2026-08-16, over the 158 show picks credited as watched:

    finished (all episodes)      21   13%
    6+ episodes, unfinished      28
    3-5 episodes                 46
    2 episodes                   32
    1 episode                    31   20%

So 87% of TV "hits" were people who had not finished the series, and the dashboard ranked TV rows
against movie rows on that basis.

THE THRESHOLD IS OURS, AND HAS TO BE. Plex publishes no show-level watched flag — a `<Directory>`
carries only `viewedLeafCount` and `leafCount` (recorded in `tests/fixtures/pms_watched_shows.xml.txt`,
which observed `2/176` returned as "watched"). Every yes/no about a series is somebody's threshold over
those two numbers. This picks the strictest and least arguable one: ALL episodes watched, which is the
wording `watch-history.tsx` already shows per title and what the Plex UI itself ticks.

NOT the engine's bar. `rows._watched_titles` treats a show as already-seen at
`min(80%, max(3, 15%))` episodes — 3 episodes of a 12-episode series. That answers "engaged enough
not to recommend it again?", which is a different question from "did they finish it?", and the two
thresholds stay separate on purpose.

BACKFILL. Movies get `finished_at = watched_at`: for a movie the two are the same event, so this is
exact rather than an approximation. Series are left NULL and fill in going forward — we know which
shows are past the bar today, but not WHEN they crossed it, and inventing that date would put watches
in the wrong week of the trend chart for ever. `reconcile_watched` scans for `finished_at IS NULL`
regardless of `watched_at`, so an already-credited series is picked up the first night it completes.

Revision ID: 0072
Revises: 0071
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns("picks")}


def _indexes(bind) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes("picks")}


def upgrade() -> None:
    bind = op.get_bind()
    if "finished_at" not in _columns(bind):
        op.add_column("picks", sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
    if "ix_picks_finished_at" not in _indexes(bind):
        op.create_index("ix_picks_finished_at", "picks", ["finished_at"])

    # Movies only. A movie's watched flag IS completion, so copying it is exact; see the docstring
    # for why series are deliberately not backfilled.
    op.execute(
        sa.text(
            "UPDATE picks SET finished_at = watched_at "
            "WHERE media_type = 'movie' AND watched_at IS NOT NULL AND finished_at IS NULL"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if "ix_picks_finished_at" in _indexes(bind):
        op.drop_index("ix_picks_finished_at", table_name="picks")
    if "finished_at" in _columns(bind):
        op.drop_column("picks", "finished_at")
