"""The payload shapes carried by `GET /api/events`, so the SPA can generate them like everything else.

The stream itself is `text/event-stream`: FastAPI has no response model to hang on it, and nothing
here ever validates a real payload — these models exist purely so the shapes land in
``components.schemas``. They are attached to the SSE route's OpenAPI ``responses`` (see
``api/events.py``), which is the one hook FastAPI offers for registering a schema no handler returns.

That "documentation only" property is also why the Literals below can be exact where a *response*
model's have to be conservative: a value outside the set here describes the schema slightly wrong,
whereas on a real response model it would fail serialization and 500 the endpoint.

Each model names the event it documents. Keep them in step with the publishers:
``services/run_service.py``, ``services/user_sync.py``, ``services/watch_sync.py`` and
``api/system.py``'s uninstall handler.
"""

from __future__ import annotations

from typing import Literal

from shortlist.server.api.schemas import PassthroughModel

#: Which of the two Tools-page syncs an event belongs to. `watched` refreshes every user's watch
#: status; `users` re-reads the plex.tv roster.
SyncKind = Literal["watched", "users"]


class RunFinishedEvent(PassthroughModel):
    """Event ``run.finished`` — a run reached a terminal state.

    `aborted` is a cancel that still completed its privacy merge and promotion, so it is an outcome
    rather than a failure; the UI distinguishes the three.
    """

    run_id: int
    status: Literal["ok", "error", "aborted"]
    error: str | None = None  # the reason, when the failure belongs to no single person


class RunProgressEvent(PassthroughModel):
    """Event ``run.progress`` — a run entered a non-terminal state. `cancelling` is published the
    moment /cancel is accepted; the run keeps going until the person it is on finishes."""

    run_id: int
    status: Literal["running", "cancelling"]


class UninstallProgressEvent(PassthroughModel):
    """Event ``uninstall.progress`` — one live step of a REAL uninstall (the dry-run preview is
    instant and streams nothing)."""

    label: str  # the line for the live log, e.g. "✓ sarah restored"
    #: Only the share-filter restore loop counts; the collection and row steps send the label alone.
    done: int | None = None
    total: int | None = None


class SyncProgressEvent(PassthroughModel):
    """Event ``sync.progress`` — a Tools-page sync moved.

    The watched sync is one determinate `done`/`total` loop over users. The users sync has two
    phases: an indeterminate `fetch` (one opaque plex.tv + Tautulli round-trip), then a determinate
    `save` bar over the roster upsert.
    """

    kind: SyncKind
    phase: Literal["fetch", "save"] | None = None  # users sync only
    done: int | None = None
    total: int | None = None


class SyncFinishedEvent(PassthroughModel):
    """Event ``sync.finished`` — a Tools-page sync ended.

    The two syncs report different things because they did different work, so most fields belong to
    one of them: `count` to the watched sync, `added`/`updated`/`total` to the users sync.
    """

    kind: SyncKind
    ok: bool
    count: int | None = None  # watched sync: how many users were refreshed
    added: int | None = None
    updated: int | None = None
    total: int | None = None
    #: Watched sync only, and only the exception CLASS name — never a message, which can carry a
    #: tokened URL (plex-safety rule 9).
    error: str | None = None
