"""Explicit lifetimes for database engines owned by individual tests or examples."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine


@contextmanager
def disposing_engine(engine: Engine) -> Iterator[Engine]:
    """Dispose an engine after its sessions have closed, including failed setup.

    Args:
        engine: The engine owned by the surrounding test, fixture, or property example.

    Yields:
        The same engine, with disposal guaranteed on leaving the context.
    """
    try:
        yield engine
    finally:
        engine.dispose()
