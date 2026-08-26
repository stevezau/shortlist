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

from loguru import logger
from sqlalchemy.orm import Session

from shortlist.server.db.models import Event

# The SAME three words the notification severities use, because both reach the UI and a reader
# cannot tell which vocabulary a given string came from. Writers used to say "warn" here while every
# reader matched "warning", so a level filter silently dropped the warnings it was asked for.
Level = Literal["info", "warning", "error"]
LEVELS: frozenset[str] = frozenset(("info", "warning", "error"))


# How much of a User-Agent to keep. Enough to tell a desktop browser from a phone, and no more —
# these rows are immutable and the support bundle exports them, so a full UA string is a long, noisy,
# fingerprintable value in an artifact people paste into public issues.
_MAX_CLIENT_CHARS = 80


def actor_of(auth: dict | None, request) -> dict:
    """Who made this change, in a form safe to store forever in an immutable audit row.

    `settings.change` recorded WHAT changed and WHEN but never WHO, so a value that moved without
    anyone owning up to it was simply unanswerable — observed on a live server (2026-08-26): a
    request threshold changed by 0.2 between two runs and no record could say what did it. Rule 10
    exists so "what changed on whose share at 03:31" is always answerable; this is the same principle
    one level up, for the settings that decide what the runs then do.

    Deliberately NOT the client IP. It would narrow "which device" further, but these rows are
    exported by the support bundle, and a LAN address is exactly the environment-specific detail this
    project keeps out of anything shareable. `via` plus a short UA answers the real question — an API
    token versus a browser, and which kind of browser.

    Args:
        auth: What ``require_owner`` returned — the browser session, or ``{"via": "api_token", ...}``.
        request: For the User-Agent header only.

    Never raises: an audit row that cannot be written is worse than one missing a field, and this is
    called on the write path of every settings change.
    """
    try:
        session = auth if isinstance(auth, dict) else {}
        ua = str(request.headers.get("user-agent") or "").strip()
        actor: dict[str, object] = {"via": session.get("via", "browser")}
        if session.get("account_id") is not None:
            actor["account_id"] = session["account_id"]
        if ua:
            actor["client"] = ua[:_MAX_CLIENT_CHARS]
        return actor
    except Exception as e:  # an audit field must never break the write it describes
        # Say so. A feature built because "there was no record" must not fail by leaving no record of
        # why. Type only, never the message — rule 9.
        logger.warning("could not identify the actor for an audit row ({})", type(e).__name__)
        return {"via": "unknown"}


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
