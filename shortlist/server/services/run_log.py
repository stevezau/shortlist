"""Run-log buffering — a run's narration, live in memory and durable in `run_log_lines`.

The engine's progress hook fires thousands of times over a run. `RunLogBuffer` keeps a short
in-memory tail for the most recent runs (what the SSE stream and a mid-run page reload read) and
batches the durable INSERTs behind it, so narration never puts a write on the hot path of a stage
transition. Reads fall back to the DB rows for any run this process didn't handle.
"""

from __future__ import annotations

import threading
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.orm import Session, sessionmaker

from shortlist.server.db.models import RunLogLine, iso_utc


def _parse_ts(value: str | None) -> datetime:
    """The progress hook stamps `iso_utc()` strings; turn one back into a datetime for the DB.
    A malformed or missing stamp falls back to now rather than dropping the line."""
    if not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.now(UTC)


class RunLogBuffer:
    """The live tail + durable writer for run narration, keyed by run id."""

    #: Flush the narration in batches. A chatty run emits thousands of lines; one INSERT each would
    #: put a write on the hot path of every stage transition for no benefit — nothing reads the
    #: durable copy while the run is live (the SSE tail serves that).
    _LOG_FLUSH_EVERY = 50

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory
        # run_id -> the run's stage activity log, in memory as the LIVE tail: the SSE stream and a
        # reload during the run read from here. The durable copy lands in `run_log_lines` (see
        # `start`), which is what any later read uses.
        self._run_logs: OrderedDict[int, deque[dict]] = OrderedDict()
        self._run_log_runs = 10  # keep the in-memory tail for this many most-recent runs
        self._log_seq: dict[int, int] = {}  # run_id -> next seq
        self._log_buffer: dict[int, list[dict]] = {}  # run_id -> lines not yet flushed
        self._log_lock = threading.Lock()

    def start(self, run_id: int) -> Callable[[dict], None]:
        """Start (or reset) a run's activity buffer and return an append sink for the progress hook.

        The sink stamps a monotonic `seq` (a timestamp is not unique enough to dedupe on — several
        lines land in the same millisecond), appends to the live tail, and buffers for the DB.
        """
        log: deque[dict] = deque(maxlen=2000)
        # Under the same lock every other reader and writer of these dicts takes. Serialized by the
        # run lock today (one run at a time), so this is latent rather than live — but "latent" is
        # what the eviction below makes it: it DROPS another run's buffer, which `flush` may
        # be reading from a worker thread at that moment.
        with self._log_lock:
            self._run_logs[run_id] = log
            self._run_logs.move_to_end(run_id)
            while len(self._run_logs) > self._run_log_runs:
                evicted, _ = self._run_logs.popitem(last=False)
                self._log_seq.pop(evicted, None)
                self._log_buffer.pop(evicted, None)
            self._log_seq[run_id] = 0
            self._log_buffer[run_id] = []

        def sink(entry: dict) -> None:
            with self._log_lock:
                seq = self._log_seq.get(run_id, 0)
                self._log_seq[run_id] = seq + 1
                entry["seq"] = seq
                log.append(entry)
                buffered = self._log_buffer.setdefault(run_id, [])
                buffered.append(entry)
                due = len(buffered) >= self._LOG_FLUSH_EVERY
            if due:
                self.flush(run_id)

        return sink

    def flush(self, run_id: int) -> None:
        """Write buffered narration to `run_log_lines`. Best-effort: losing a log line must never
        fail a run that has already written to Plex."""
        with self._log_lock:
            pending = self._log_buffer.get(run_id) or []
            self._log_buffer[run_id] = []
        if not pending:
            return
        try:
            with self._sessions() as session:
                session.add_all(
                    [
                        RunLogLine(
                            run_id=run_id,
                            seq=entry["seq"],
                            ts=_parse_ts(entry.get("ts")),
                            user_slug=entry.get("user") or "",
                            stage=entry.get("stage") or "",
                            counts=entry.get("counts") or {},
                            reason=(entry.get("reason") or "")[:1024],
                            level=entry.get("level") or "info",
                        )
                        for entry in pending
                    ]
                )
                session.commit()
        except Exception:
            logger.exception("could not persist {} run-log line(s) for run {}", len(pending), run_id)

    def lines(self, run_id: int, after_seq: int | None = None) -> list[dict]:
        """A run's activity log, newest-last.

        Serves the live in-memory tail while the run is in flight, and falls back to the durable
        `run_log_lines` rows for any run this process didn't handle — which, before those rows
        existed, was every run after a restart.
        """
        live = list(self._run_logs.get(run_id, ()))
        if live:
            # The in-memory tail is small and already materialised, so trimming it in Python costs
            # nothing. The durable path below cannot afford that — the poller asks every second.
            return [e for e in live if after_seq is None or e.get("seq", -1) > after_seq]

        with self._sessions() as session:
            query = session.query(RunLogLine).filter(RunLogLine.run_id == run_id)
            if after_seq is not None:
                query = query.filter(RunLogLine.seq > after_seq)
            rows = query.order_by(RunLogLine.seq).all()
        return [
            {
                "seq": row.seq,
                "ts": iso_utc(row.ts),
                "run_id": run_id,
                "user": row.user_slug,
                "stage": row.stage,
                "counts": row.counts or {},
                **({"reason": row.reason} if row.reason else {}),
            }
            for row in rows
        ]
