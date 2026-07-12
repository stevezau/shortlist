"""Loguru configuration shared by the CLI and (later) the server."""

import sys

from loguru import logger


def configure_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure loguru sinks.

    Args:
        level: Minimum level for the stderr sink.
        log_file: Optional rotating file sink path (used by the CLI under /config/logs/).
    """
    logger.remove()
    logger.add(sys.stderr, level=level, backtrace=False, diagnose=False)
    if log_file:
        logger.add(log_file, level="DEBUG", rotation="10 MB", retention=10, backtrace=False, diagnose=False)
