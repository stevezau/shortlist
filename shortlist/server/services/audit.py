"""One way to write an audit Event (plex-safety rule 10).

Every write to Plex or plex.tv — real or dry-run — leaves a structured `events` row carrying its
diff, so "what changed on whose share at 03:31" is answerable from the UI. That was previously
hand-rolled in five places, which is why some writers stamped a `"at"` field into the message and
others didn't: the same question was answered from a different field depending on who wrote the row.

There is no timestamp in the message. `Event.ts` is the column the API sorts and filters on, and a
second copy inside the JSON could only ever drift from it.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from shortlist.server.db.models import Event

# The SAME three words the notification severities use, because both reach the UI and a reader
# cannot tell which vocabulary a given string came from. Writers used to say "warn" here while every
# reader matched "warning", so a level filter silently dropped the warnings it was asked for.
Level = Literal["info", "warning", "error"]
LEVELS: frozenset[str] = frozenset(("info", "warning", "error"))


def add_audit(session: Session, scope: str, level: Level, **message) -> None:
    """Add one audit Event to an OPEN session — the caller owns the commit.

    Use this from code that is already inside a transaction it wants the event to share.
    """
    if level not in LEVELS:
        raise ValueError(f"audit level must be one of {sorted(LEVELS)}, got {level!r}")
    session.add(Event(scope=scope, level=level, message=dict(message)))


def write_audit(state, scope: str, level: str, **message) -> None:
    """Write one audit Event in its own session and commit it.

    Use this from code with no transaction of its own (job handlers, the on-demand reconciles): the
    audit is the record that the write was ATTEMPTED, so it must not be lost with whatever the caller
    does next.
    """
    with state.sessions() as session:
        add_audit(session, scope, level, **message)
        session.commit()
