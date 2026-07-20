"""Loguru configuration for the server."""

import logging
import sys

from loguru import logger

# The levels the UI/API accept, quietest → loudest. TRACE adds full AI prompts + responses;
# DEBUG adds per-source candidate counts, cache hits, throttle waits, and per-row/per-user timing.
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR")

# Remember the file sink between reconfigurations so a live level change (settings PUT) can rebuild
# the stderr sink without losing — or duplicating — the rotating debug file.
_log_file: str | None = None

# The stdlib→loguru bridge is installed once (not per reconfigure) so a live level change can't
# stack duplicate root handlers.
_stdlib_bridged = False


class _InterceptHandler(logging.Handler):
    """Route stdlib ``logging`` records into loguru.

    APScheduler (job misfires, "maximum number of running instances reached"), plexapi, and
    SQLAlchemy log via the stdlib root logger — none of which reaches loguru's rotating file
    otherwise. When the 03:30 run doesn't fire, the APScheduler misfire line is exactly what the
    operator needs, and it would never touch ``/config/logs`` without this bridge.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk back past the stdlib logging frames so loguru attributes the real caller.
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _bridge_stdlib_logging() -> None:
    """Install the stdlib→loguru bridge once (idempotent)."""
    global _stdlib_bridged
    if _stdlib_bridged:
        return
    logging.basicConfig(handlers=[_InterceptHandler()], level=logging.INFO, force=True)
    # httpx logs a line per request at INFO; our own http_retry already narrates calls at DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # asyncio warns "socket.send() raised exception" every time an SSE/HTTP client disconnects
    # mid-stream — harmless, but noisy now that the bridge surfaces it. Keep real asyncio errors.
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    _stdlib_bridged = True


def normalize_level(level: str | None) -> str:
    """Coerce any input to a valid loguru level name, defaulting to INFO."""
    candidate = (level or "").strip().upper()
    return candidate if candidate in LOG_LEVELS else "INFO"


def configure_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """(Re)configure loguru sinks. Idempotent — safe to call again on a live level change.

    Args:
        level: Minimum level for the stderr sink (console / `docker logs`).
        log_file: Optional rotating file sink path (the server writes under /config/logs/).
            The file sink always captures DEBUG so the on-disk log stays useful even when the
            console is quiet. Passing None keeps the file sink from the previous call.
    """
    global _log_file
    if log_file is not None:
        _log_file = log_file
    logger.remove()
    logger.add(sys.stderr, level=normalize_level(level), backtrace=False, diagnose=False)
    if _log_file:
        logger.add(_log_file, level="DEBUG", rotation="10 MB", retention=10, backtrace=False, diagnose=False)
    _bridge_stdlib_logging()
