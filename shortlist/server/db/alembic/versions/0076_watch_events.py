"""Watch events, watch sessions, and the two columns that make partial watches reportable.

Shortlist has only ever read ONE watch signal: `/library/sections/{key}/all?unwatched=0`, per user,
which is Plex's binary watched flag. That signal cannot answer the two questions this schema exists
for.

WHEN did they press play? `lastViewedAt` is the LATEST view of a title, so a rewatch overwrites the
original date and the first watch is gone. Crediting a pick against "was this in their row at the
time" needs a real event time, not a high-water mark.

Did they START it? A film's `viewCount` does not move until roughly 90% played, so someone who
watched twenty minutes and gave up is invisible. That is not a missing nicety: it is the difference
between "nobody opened this pick" and "the row got them to press play and the title lost them", and
only one of those is a fact about the recommendation.

Two sources fill these tables, both probed against a real server (PMS 1.43.3.10793) before this was
written; see `.claude/docs/watch-signals-design.md`:

* `watch_events` — Plex's own history log (`/status/sessions/history/all`), which is server-side,
  reaches back to 2020 on the maintainer's box, carries `accountID` and an exact `viewedAt`, and
  survives Shortlist being down. It records COMPLETIONS only.
* `watch_sessions` — the PMS notification websocket, the only place a partial play exists. A session
  is a span, not a point, which is why it is not folded into `watch_events`: conflating "we watched
  them watch it" with "Plex says it completed" would muddy both.

`picks.max_percent` denormalises the furthest a person got, so the report does not join sessions on
every read. `run_shared_rows.audience` snapshots who could SEE a shared row when it was delivered —
`collection_audience` is current state with no history, so without this, adding someone to a subset
row today would retroactively credit their older watches.

Nothing here is backfilled. Every column is nullable or defaulted, and both tables start empty: the
history feed fills `watch_events` from Plex's log on first run, and sessions only exist going
forward. No existing number moves when this migration runs.

Revision ID: 0076
Revises: 0075
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    if "watch_events" not in _tables(bind):
        op.create_table(
            "watch_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            # The plex.tv account id, NOT our users.id: an event can arrive for an account we do not
            # (yet) have a user row for, and dropping it at read time would silently lose history the
            # moment someone is invited. Joined to `users.plex_account_id` at query time.
            sa.Column("plex_account_id", sa.Integer(), nullable=False, index=True),
            # The movie or the EPISODE. A pick for a series stores the SHOW's key, so `show_rating_key`
            # below is what actually matches — measured on 30 days of real history, 46 of 78 matches
            # were only reachable through it.
            sa.Column("rating_key", sa.Integer(), nullable=False, index=True),
            sa.Column("show_rating_key", sa.Integer(), nullable=True, index=True),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("source", sa.String(16), nullable=False, server_default="history"),
            # Plex's own id for the row. UNIQUE because the log repeats itself — the same item for the
            # same account seconds apart, and twice at one second on one device (both observed live).
            # Dedupe therefore costs nothing and needs no window heuristic.
            sa.Column("history_key", sa.String(128), nullable=True, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )

    if "watch_sessions" not in _tables(bind):
        op.create_table(
            "watch_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("plex_account_id", sa.Integer(), nullable=False, index=True),
            # Plex's session key. Unique only while the session is LIVE and reused afterwards, so it
            # is not a key here — `ended_at IS NULL` is what identifies the open one.
            sa.Column("session_key", sa.String(32), nullable=False, index=True),
            sa.Column("rating_key", sa.Integer(), nullable=False, index=True),
            sa.Column("show_rating_key", sa.Integer(), nullable=True, index=True),
            sa.Column("media_type", sa.String(16), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("max_offset_ms", sa.Integer(), nullable=False, server_default="0"),
            # Runtime, read once per title and cached. Nullable because a session can start before we
            # have resolved it, and a percentage of an unknown runtime is worse than no percentage.
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            # stopped | timeout | replaced — how we decided it was over. `timeout` is the honest label
            # for a client that vanished, which is why it is recorded rather than assumed to be a stop.
            sa.Column("end_reason", sa.String(16), nullable=True),
        )

    if "max_percent" not in _columns(bind, "picks"):
        op.add_column("picks", sa.Column("max_percent", sa.Integer(), nullable=True))

    if "audience" not in _columns(bind, "run_shared_rows"):
        op.add_column("run_shared_rows", sa.Column("audience", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if "audience" in _columns(bind, "run_shared_rows"):
        op.drop_column("run_shared_rows", "audience")
    if "max_percent" in _columns(bind, "picks"):
        op.drop_column("picks", "max_percent")
    for table in ("watch_sessions", "watch_events"):
        if table in _tables(bind):
            op.drop_table(table)
