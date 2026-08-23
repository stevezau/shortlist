"""Clear `picks.max_percent` on SERIES — an episode's progress was never the show's.

`session_progress` resolves an episode session onto the SHOW's pick (a pick for a series stores the
show's rating key, while playback reports the episode). It then kept `viewOffset / duration`, which is
how far through THAT EPISODE they got. So one full episode of a sixty-episode series arrived as
`max_percent = 100`.

The report read that as an abandonment near the end: "stops at 100%", filed under the 75%+ bucket. The
dashboard would have stated as fact that people give up on a show just before finishing it, when what
they did was watch episode one and stop — and given this codebase's own measurement that 87% of
credited show picks are unfinished, that is the majority of series engagement being described
backwards.

`watch_events.session_progress` now returns None for a show, so nothing new is written. But
`_stamp_percent` deliberately never walks a percentage backwards (retention deletes old sessions, so
the max a session can report shrinks over time, and a fresh short session must not overwrite a real
earlier one). That guard means the already-wrong values would have survived for ever.

Observed on the maintainer's server before this ran: both of the two picks carrying a percentage were
series — 99% and 100% — and both would have rendered as near-complete abandonments.

Films are untouched: for a film the offset IS the title's progress, and those values are correct.

Revision ID: 0077
Revises: 0076
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE picks SET max_percent = NULL WHERE media_type = 'show'"))


def downgrade() -> None:
    """Deliberately empty.

    The cleared values were wrong — an episode's progress recorded as a series' — so there is nothing
    to restore, and inventing a number to put back would be worse than the NULL that honestly means
    "we do not know how far through the series they are".
    """
