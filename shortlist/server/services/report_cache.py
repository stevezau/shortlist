"""A short-lived, in-process cache of the dashboard report.

The report costs seconds on a large picks table and gates the dashboard, but its inputs change only
when something below calls :func:`invalidate_report_cache`: a run ending, whether it finished,
failed or was cancelled while queued (``run_service``), a full watch sync completing
(``watch_sync``), the live-playback reconcile job crediting picks (``jobs._watch_reconcile``),
clearing deleted rows (``api/report``), clearing run history (``api/runs.clear_runs``) and retention
pruning (``jobs._maintenance_prune``). The TTL is the backstop for changes nobody hooks. Per process
and never persisted: a restart starts cold.

The live-listener status (``watch_sync.live_since``/``live_down_since``) and the next watch-sync time
are NOT cached: the caller merges them onto each served copy.

A generation counter keeps a slow compute from overwriting an invalidation: callers take
:func:`current_generation` before computing and pass it to :func:`store_report`, which discards the
result if any invalidation happened in between.
"""

from __future__ import annotations

import copy
import threading
import time

TTL_SECONDS = 120.0

_lock = threading.Lock()
_entries: dict[str, tuple[float, dict]] = {}
_generation = 0


def current_generation() -> int:
    """The invalidation count; capture it before computing a report to hand to :func:`store_report`."""
    with _lock:
        return _generation


def get_cached_report(window: str) -> dict | None:
    """A copy of the cached report for `window`, or None when absent or older than the TTL."""
    with _lock:
        entry = _entries.get(window)
        if entry is None or time.monotonic() - entry[0] >= TTL_SECONDS:
            return None
        return copy.deepcopy(entry[1])


def store_report(window: str, report: dict, generation: int | None = None) -> None:
    """Remember a copy of `report` for `window`, so a caller mutating its own dict cannot poison it.

    Args:
        window: The report window the result is for.
        report: The computed report.
        generation: The :func:`current_generation` captured before computing. When given, the result is
            discarded if an invalidation happened since: it was computed from data that has changed.
    """
    with _lock:
        if generation is not None and generation != _generation:
            return
        _entries[window] = (time.monotonic(), copy.deepcopy(report))


def invalidate_report_cache() -> None:
    """Drop every cached report. Call whenever the data the report reads has changed."""
    global _generation
    with _lock:
        _generation += 1
        _entries.clear()
