"""Background job workers release their database before application shutdown disposes it."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from starlette.datastructures import State

from shortlist.server.main import create_app
from shortlist.server.services import jobs


def test_shutdown_waits_for_background_database_work_before_disposing_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    disposed_while_running: list[bool] = []
    original_shutdown = jobs.shutdown_background

    async def scenario() -> None:
        shutdown_entered = asyncio.Event()

        async def observe_shutdown(state: State) -> None:
            shutdown_entered.set()
            await original_shutdown(state)

        async def blocked_drain(state: State, reason: str) -> None:
            def worker() -> None:
                try:
                    with state.sessions() as session:
                        session.execute(text("SELECT 1"))
                        started.set()
                        if not release.wait(10):
                            raise TimeoutError("test did not release its database worker")
                finally:
                    finished.set()

            await asyncio.to_thread(worker)

        monkeypatch.setattr(jobs, "shutdown_background", observe_shutdown)
        monkeypatch.setattr(jobs, "drain_now", blocked_drain)
        app = create_app(config_dir=tmp_path)
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        shutdown_task: asyncio.Task[None] | None = None
        try:
            engine = app.state.sessions.kw["bind"]
            original_dispose = engine.dispose

            def observe_dispose() -> None:
                disposed_while_running.append(not finished.is_set())
                original_dispose()

            monkeypatch.setattr(engine, "dispose", observe_dispose)
            jobs.drain_in_background(app.state, "test-owned background database work")
            assert await asyncio.to_thread(started.wait, 5)

            shutdown_task = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await asyncio.wait_for(shutdown_entered.wait(), timeout=5)

            assert not shutdown_task.done()
            assert disposed_while_running == []
            release.set()
            await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=5)

            assert finished.is_set()
            assert disposed_while_running == [False]
        finally:
            release.set()
            if shutdown_task is None:
                await lifespan.__aexit__(None, None, None)
            else:
                await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=10)

    asyncio.run(scenario())


def test_shutdown_waits_only_for_its_own_app_and_leaves_the_other_worker_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = SimpleNamespace(started=threading.Event(), release=threading.Event(), finished=threading.Event())
    second = SimpleNamespace(started=threading.Event(), release=threading.Event(), finished=threading.Event())

    async def scenario() -> None:
        async def blocked_drain(state: SimpleNamespace, reason: str) -> None:
            def worker() -> None:
                state.started.set()
                try:
                    if not state.release.wait(10):
                        raise TimeoutError("test did not release its worker")
                finally:
                    state.finished.set()

            await asyncio.to_thread(worker)

        monkeypatch.setattr(jobs, "drain_now", blocked_drain)
        jobs.drain_in_background(first, "first app")
        jobs.drain_in_background(second, "second app")
        try:
            assert await asyncio.to_thread(first.started.wait, 5)
            assert await asyncio.to_thread(second.started.wait, 5)
            first.release.set()

            await asyncio.wait_for(jobs.shutdown_background(first), timeout=5)

            assert first.finished.is_set()
            assert not second.finished.is_set()
        finally:
            first.release.set()
            second.release.set()
            await asyncio.wait_for(
                asyncio.gather(jobs.shutdown_background(first), jobs.shutdown_background(second)), timeout=10
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_shutdown", [False, True])
def test_cancelled_caller_keeps_worker_and_writer_lock_until_shutdown_finishes(
    monkeypatch: pytest.MonkeyPatch, cancel_shutdown: bool
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    state = SimpleNamespace(sessions=object())
    dispatches: list[str | None] = []

    async def scenario() -> None:
        monkeypatch.setattr(jobs, "_DRAIN_LOCK", asyncio.Lock())

        async def blocked_drain(state: SimpleNamespace, sessions: object, only_kind: str | None = None) -> int:
            dispatches.append(only_kind)

            def worker() -> None:
                started.set()
                try:
                    if not release.wait(10):
                        raise TimeoutError("test did not release its writer")
                finally:
                    finished.set()

            async with jobs.plex_writer_lock():
                await asyncio.to_thread(worker)
            return 1

        monkeypatch.setattr(jobs, "_drain", blocked_drain)
        caller = asyncio.create_task(jobs.run_pending(state))
        shutdown_task: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(started.wait, 5)
            with pytest.raises(RuntimeError, match="active jobs"):
                jobs.start_background(state)

            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller

            assert jobs.plex_writer_lock().locked()
            assert jobs._DRAIN_LOCK.locked()
            assert not finished.is_set()
            assert await jobs.run_pending(state) == 0

            shutdown_task = asyncio.create_task(jobs.shutdown_background(state))
            await asyncio.sleep(0)  # Let shutdown close admission before testing all entry points.
            if cancel_shutdown:
                shutdown_task.cancel()
                await asyncio.sleep(0)

            assert not shutdown_task.done()
            assert jobs.plex_writer_lock().locked()
            assert await jobs.run_pending(state) == 0
            assert await jobs.drain_kind(state, "watch.reconcile") == 0
            jobs.drain_in_background(state, "must remain queued until restart")
            assert dispatches == [None]

            release.set()
            if cancel_shutdown:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=5)
            else:
                await asyncio.wait_for(asyncio.shield(shutdown_task), timeout=5)
            assert finished.is_set()
            assert not jobs.plex_writer_lock().locked()
            assert not jobs._DRAIN_LOCK.locked()

            jobs.start_background(state)
            assert await jobs.run_pending(state) == 1
            assert dispatches == [None, None]
        finally:
            release.set()
            await asyncio.wait_for(jobs.shutdown_background(state), timeout=10)
            pending = [caller] if shutdown_task is None else [caller, shutdown_task]
            await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(scenario())
