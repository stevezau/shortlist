"""Loguru configuration shared by the CLI and the server."""

import sys

from loguru import logger

# The levels the UI/CLI/API accept, quietest → loudest. TRACE adds full AI prompts + responses;
# DEBUG adds per-source candidate counts, cache hits, throttle waits, and per-row/per-user timing.
LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR")

# Remember the file sink between reconfigurations so a live level change (settings PUT) can rebuild
# the stderr sink without losing — or duplicating — the rotating debug file.
_log_file: str | None = None


def normalize_level(level: str | None) -> str:
    """Coerce any input to a valid loguru level name, defaulting to INFO."""
    candidate = (level or "").strip().upper()
    return candidate if candidate in LOG_LEVELS else "INFO"


def configure_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """(Re)configure loguru sinks. Idempotent — safe to call again on a live level change.

    Args:
        level: Minimum level for the stderr sink (console / `docker logs`).
        log_file: Optional rotating file sink path (used by the CLI and server under /config/logs/).
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
