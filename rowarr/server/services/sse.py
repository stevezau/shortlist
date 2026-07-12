"""In-process SSE event bus: one publisher, N subscriber queues (one EventSource per page)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from loguru import logger


class EventBus:
    def __init__(self, max_queue: int = 256):
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._max_queue = max_queue

    def publish(self, event: str, data: dict) -> None:
        frame = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                logger.warning("SSE subscriber queue full — dropping client")
                self._subscribers.discard(queue)

    async def stream(self) -> AsyncIterator[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(queue)
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=25)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self._subscribers.discard(queue)
