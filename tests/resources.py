"""Check database cleanup and release unreachable metadata during long test runs."""

from __future__ import annotations

import gc
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

_COMPLETED_TESTS = pytest.StashKey[int]()
_DATABASE_SUFFIXES = (".db", ".sqlite", ".sqlite3")


def _open_databases(directory: Path) -> set[str]:
    descriptors = Path("/proc/self/fd")
    if not descriptors.is_dir():
        return set()
    root = directory.resolve()
    remaining = set()
    for descriptor in descriptors.iterdir():
        try:
            target = os.readlink(descriptor).removesuffix(" (deleted)")
        except FileNotFoundError:
            continue  # Another thread may have closed the descriptor since the listing.
        path = Path(target)
        database = target.removesuffix("-wal").removesuffix("-shm").removesuffix("-journal")
        if path.is_relative_to(root) and database.endswith(_DATABASE_SUFFIXES):
            remaining.add(target)
    return remaining


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, Any, Any]:
    directory = getattr(item, "funcargs", {}).get("tmp_path")
    result = yield
    # Run after every fixture finalizer, before GC can hide an omitted close/dispose.
    if isinstance(directory, Path) and (remaining := _open_databases(directory)):
        pytest.fail(
            "Database handles remain open after test teardown. Close SQLite connections and "
            "use engine.dispose() in a finally block (tests.db_helpers.disposing_engine). "
            "SQLite's transaction context manager does not close its connection.\n" + "\n".join(sorted(remaining)),
            pytrace=False,
        )
    return result


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item) -> Generator[None, Any, Any]:
    result = yield
    completed = item.config.stash.get(_COMPLETED_TESTS, 0) + 1
    item.config.stash[_COMPLETED_TESTS] = completed
    # Alembic/SQLAlchemy metadata forms large unreachable cycles even after connections close.
    # In long runs these accumulated for thousands of tests under Python 3.14's automatic GC.
    # Protocol completion is after pytest releases funcargs; live wider-scope fixtures survive GC.
    if completed % 50 == 0:
        gc.collect()
    return result
