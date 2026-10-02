"""In-process SSE event bus: one publisher, N subscriber queues (one EventSource per page)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
from collections.abc import AsyncIterator, Callable
from types import FrameType
from typing import Any

from loguru import logger

#: The signals `docker stop` and Ctrl+C send, which uvicorn handles with `signal.signal`.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class EventBus:
    """Fans published frames out to every open `stream()`.

    Not thread-safe: `publish` and `close` run on the event loop's thread. Other threads hand them
    over with `loop.call_soon_threadsafe`.
    """

    def __init__(self, max_queue: int = 256):
        self._subscribers: set[asyncio.Queue[str | None]] = set()
        # Every stream still open, including one `publish` stopped feeding when its queue overflowed.
        self._open_streams: set[asyncio.Queue[str | None]] = set()
        self._max_queue = max_queue
        self._closed = False

    def publish(self, event: str, data: dict) -> None:
        frame = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full — dropping client")
                self._subscribers.discard(queue)

    def close(self) -> None:
        """End every open stream once it has sent what was already published, and any opened later at once.

        A stopping uvicorn waits for every open connection before it runs the app's shutdown, and a
        stream ends only when this says so. Idempotent. Call it on the event loop's thread, like `publish`.
        """
        if self._closed:
            return
        self._closed = True
        self._subscribers.clear()
        for queue in self._open_streams:
            # Only a stream with nothing queued can be parked in `get()`; the rest see `_closed` once drained.
            if queue.empty():
                queue.put_nowait(None)

    async def stream(self) -> AsyncIterator[str]:
        if self._closed:
            return
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        self._open_streams.add(queue)
        try:
            yield "event: hello\ndata: {}\n\n"
            while not (self._closed and queue.empty()):
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=25)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if frame is None:
                    return
                yield frame
        finally:
            self._subscribers.discard(queue)
            self._open_streams.discard(queue)


def close_on_stop_signals(bus: EventBus, loop: asyncio.AbstractEventLoop) -> list[signal.Signals]:
    """Chain `bus.close()` in front of the stop-signal handlers already installed (uvicorn's).

    uvicorn's handler only sets a flag; its shutdown then waits for open connections before running
    the app's, so an open event stream held the stop until `--timeout-graceful-shutdown` cancelled it
    (logged as an ERROR and a traceback). uvicorn installs its handlers with `signal.signal` before the
    lifespan starts and restores their predecessors after the lifespan shuts down, which replaces
    these wrappers too — nothing has to undo them.

    Skipped, silently, off the main thread (`signal.signal` raises there — Starlette's TestClient) and
    for a signal with no Python handler to chain; the graceful-shutdown timeout still covers those.

    Args:
        bus: The bus whose streams a stop signal ends.
        loop: The loop the bus runs on.

    Returns:
        The signals now chained.
    """
    chained: list[signal.Signals] = []
    for sig in STOP_SIGNALS:
        previous = signal.getsignal(sig)
        if not callable(previous):
            continue
        try:
            signal.signal(sig, _closing_handler(bus, loop, previous))
        except ValueError:
            return chained
        chained.append(sig)
    return chained


SignalHandler = Callable[[int, FrameType | None], Any]


def _closing_handler(bus: EventBus, loop: asyncio.AbstractEventLoop, previous: SignalHandler) -> SignalHandler:
    # A signal handler runs on the main thread between bytecodes, possibly inside the loop itself: it
    # only hands the close to the loop, and does nothing that takes a lock or does I/O.
    def handle(signum: int, frame: FrameType | None) -> None:
        try:
            with contextlib.suppress(RuntimeError):  # a closed loop has no stream left to end
                loop.call_soon_threadsafe(bus.close)
        finally:
            previous(signum, frame)

    return handle
