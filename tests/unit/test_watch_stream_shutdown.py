"""Stopping the listener waits for its private worker before database disposal."""

import asyncio
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from shortlist.server.services.watch_stream import WatchStream

pytestmark = pytest.mark.real_migrations


@pytest.mark.parametrize("cancel_shutdown", [False, True])
def test_shutdown_joins_cancelled_database_work_without_blocking_the_loop(
    tmp_path: Path, cancel_shutdown: bool
) -> None:
    stream = WatchStream(MagicMock(), MagicMock())
    release = threading.Event()
    connections: list[sqlite3.Connection] = []
    workers: list[threading.Thread] = []

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def write() -> None:
            workers.append(threading.current_thread())
            with closing(sqlite3.connect(tmp_path / "listener.sqlite", check_same_thread=False)) as connection:
                connections.append(connection)
                connection.execute("CREATE TABLE marker (value INTEGER)")
                connection.execute("INSERT INTO marker VALUES (42)")
                loop.call_soon_threadsafe(started.set)
                assert release.wait(5), "test did not release the listener worker"
                connection.commit()

        work = asyncio.create_task(stream._in_pool(write))
        shutdown = None
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            work.cancel()
            with pytest.raises(asyncio.CancelledError):
                await work

            shutdown = asyncio.create_task(stream.shutdown())
            await asyncio.sleep(0)
            assert not shutdown.done()
            rejected = MagicMock()
            with pytest.raises(RuntimeError, match="shutting down"):
                await stream._in_pool(rejected)
            rejected.assert_not_called()

            if cancel_shutdown:
                for _ in range(2):
                    shutdown.cancel()
                    await asyncio.sleep(0)
                    assert not shutdown.done()
            release.set()
            if cancel_shutdown:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(shutdown, timeout=2)
            else:
                await asyncio.wait_for(shutdown, timeout=2)

            assert not workers[0].is_alive()
            await stream.shutdown()
        finally:
            release.set()
            await asyncio.gather(work, return_exceptions=True)
            if shutdown is not None:
                await asyncio.gather(shutdown, return_exceptions=True)
            await stream.shutdown()

    asyncio.run(scenario())

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with closing(sqlite3.connect(tmp_path / "listener.sqlite")) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == (42,)


def test_shutdown_is_safe_before_the_listener_starts() -> None:
    stream = WatchStream(MagicMock(), MagicMock())

    async def scenario() -> None:
        await stream.shutdown()
        await stream.shutdown()
        with pytest.raises(RuntimeError, match="shutting down"):
            await stream._in_pool(lambda: None)

    asyncio.run(scenario())
