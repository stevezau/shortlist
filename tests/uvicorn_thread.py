"""One way for a test to run an ASGI app on a loopback port in a daemon thread."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn


class UvicornThread(threading.Thread):
    """An ASGI app served from a daemon thread on a socket this object already owns.

    The socket is bound in ``__init__`` and handed to uvicorn, so the port is known (and reserved) before
    the server starts: a caller can put ``url`` into the app's own settings first.

    Args:
        app: The ASGI application.
        port: A specific loopback port to bind; 0 picks a free one.
        sock: An already-bound socket to serve on instead (``port`` is then ignored).
        log_level: uvicorn's log level.
        access_log: Whether uvicorn logs each request.
    """

    def __init__(
        self,
        app,
        port: int = 0,
        *,
        sock: socket.socket | None = None,
        log_level: str = "warning",
        access_log: bool = True,
    ) -> None:
        super().__init__(daemon=True)
        if sock is None:
            sock = socket.socket()
            sock.bind(("127.0.0.1", port))
        self._sock = sock
        self.port = int(sock.getsockname()[1])
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_level=log_level, access_log=access_log))

    def run(self) -> None:
        self.server.run(sockets=[self._sock])

    def start(self, timeout_s: float = 15) -> UvicornThread:  # type: ignore[override]
        """Start serving and block until uvicorn reports it is accepting connections."""
        super().start()
        deadline = time.monotonic() + timeout_s
        while not self.server.started:
            if not self.is_alive() or time.monotonic() > deadline:
                raise RuntimeError(f"uvicorn on port {self.port} did not start within {timeout_s}s")
            time.sleep(0.01)
        return self

    def wait_until_up(self, path: str, timeout_s: float = 20) -> None:
        """Block until ``GET path`` answers; for a server whose readiness is a real endpoint."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                httpx.get(f"{self.url}{path}", timeout=1)
                return
            except httpx.HTTPError:
                time.sleep(0.05)
        raise AssertionError(f"loopback server on port {self.port} did not become ready")

    def stop(self) -> None:
        self.server.should_exit = True
        self.join(timeout=10)
        self._sock.close()
