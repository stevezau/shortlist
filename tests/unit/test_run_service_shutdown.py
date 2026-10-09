"""Run and watch workers finish before their owning service releases the database."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services import jobs
from shortlist.server.services.run_service import RunService
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from tests.db_helpers import disposing_engine


@pytest.fixture
def service(tmp_path: Path) -> Iterator[RunService]:
    run_migrations(tmp_path)
    with disposing_engine(make_engine(tmp_path)) as engine:
        yield RunService(make_session_factory(engine), EventBus(), tmp_path, SecretBox(tmp_path))


@pytest.mark.parametrize("cancel_shutdown", [False, True])
def test_cancelled_run_keeps_its_worker_and_writer_lock_until_shutdown_finishes(
    service: RunService, monkeypatch: pytest.MonkeyPatch, cancel_shutdown: bool
) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    async def scenario() -> None:
        async def blocked_run(run_id, dry_run, user_ids, collection_ids, loop) -> None:
            def worker() -> None:
                started.set()
                try:
                    if not release.wait(10):
                        raise TimeoutError("test did not release its run worker")
                finally:
                    finished.set()

            async with jobs.plex_writer_lock():
                await asyncio.to_thread(worker)

        async def forbidden_sync() -> None:
            pytest.fail("shutdown admitted a new watch sync")

        monkeypatch.setattr(service, "_run_locked", blocked_run)
        monkeypatch.setattr(service, "_sync_watched", forbidden_sync)
        run_id = await service.start_run(trigger="manual", dry_run=True)
        (caller,) = service._tasks
        shutdown_task: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            caller.cancel()
            await asyncio.sleep(0)

            assert service._cancels[run_id].is_set()
            assert not caller.done()
            assert jobs.plex_writer_lock().locked()
            assert not finished.is_set()

            shutdown_task = asyncio.create_task(service.shutdown())
            await asyncio.sleep(0)
            if cancel_shutdown:
                shutdown_task.cancel()
                await asyncio.sleep(0)

            assert not shutdown_task.done()

            def forbidden_session():
                pytest.fail("shutdown admitted a new database session")

            monkeypatch.setattr(service, "_sessions", forbidden_session)
            with pytest.raises(RuntimeError, match="shutting down"):
                await service.start_run(trigger="manual", dry_run=True)
            with pytest.raises(RuntimeError, match="shutting down"):
                await service.dispatch_queued_assistant_run(run_id)
            await service.sync_watched()
            assert service._tasks == {caller}

            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(caller), timeout=5)
            if cancel_shutdown:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=5)
            else:
                await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=5)

            assert finished.is_set()
            assert not jobs.plex_writer_lock().locked()
            assert not service._tasks
        finally:
            release.set()
            await asyncio.wait_for(service.shutdown(), timeout=10)
            await asyncio.gather(
                caller, *([shutdown_task] if shutdown_task is not None else []), return_exceptions=True
            )

    asyncio.run(scenario())


def test_cancelled_scheduled_sync_remains_owned_until_shutdown_finishes(
    service: RunService, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    async def scenario() -> None:
        async def blocked_sync() -> None:
            def worker() -> None:
                started.set()
                try:
                    if not release.wait(10):
                        raise TimeoutError("test did not release its watch worker")
                finally:
                    finished.set()

            await asyncio.to_thread(worker)

        monkeypatch.setattr(service, "_sync_watched", blocked_sync)
        caller = asyncio.create_task(service.sync_watched())
        shutdown_task: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert len(service._tasks) == 1
            assert not finished.is_set()

            shutdown_task = asyncio.create_task(service.shutdown())
            await asyncio.sleep(0)
            assert not shutdown_task.done()
            release.set()
            await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=5)

            assert finished.is_set()
            assert not service._tasks
        finally:
            release.set()
            await asyncio.wait_for(service.shutdown(), timeout=10)
            await asyncio.gather(
                caller, *([shutdown_task] if shutdown_task is not None else []), return_exceptions=True
            )

    asyncio.run(scenario())
