"""A short-lived, in-process cache of the dashboard report.

The report costs seconds on a large picks table and gates the dashboard, but its inputs change only
when a run finishes, the watch sync completes, or history is cleared. Those call
:func:`invalidate_report_cache`; the TTL is the backstop for changes nobody hooks (live playback
credits, retention pruning). Per process and never persisted: a restart starts cold.
"""

from __future__ import annotations

import copy
import threading
import time

TTL_SECONDS = 120.0

_lock = threading.Lock()
_entries: dict[str, tuple[float, dict]] = {}


def get_cached_report(window: str) -> dict | None:
    """A copy of the cached report for `window`, or None when absent or older than the TTL."""
    with _lock:
        entry = _entries.get(window)
        if entry is None or time.monotonic() - entry[0] >= TTL_SECONDS:
            return None
        return copy.deepcopy(entry[1])


def store_report(window: str, report: dict) -> None:
    """Remember a copy of `report` for `window`, so a caller mutating its own dict cannot poison it."""
    with _lock:
        _entries[window] = (time.monotonic(), copy.deepcopy(report))


def invalidate_report_cache() -> None:
    """Drop every cached report. Call whenever the data the report reads has changed."""
    with _lock:
        _entries.clear()
