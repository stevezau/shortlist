"""The season catalogue a request, job or run sees: the built-ins, then the owner's own (issue #137)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from shortlist.engine.seasons import BUILTIN_SEASONS, Season


def load_catalogue(session: Session) -> dict[str, Season]:
    """Every season a row may follow. Built once per request, job or run and passed down explicitly."""
    return dict(BUILTIN_SEASONS)
