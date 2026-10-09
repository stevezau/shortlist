"""Explicit lifetimes for database engines owned by individual tests or examples."""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine

_schema_template: sqlite3.Connection | None = None
_schema_template_lock = threading.Lock()


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


def create_schema(engine: Engine) -> None:
    """`Base.metadata.create_all(engine)`, for a fresh, empty database — by copying a schema built once.

    Building the schema costs ~50 ms; copying it with SQLite's backup API costs ~0.1 ms. Property tests
    build one per example, so the difference was minutes per run. The copy REPLACES whatever the
    database held, which is why it is only for a database that was just created.

    Args:
        engine: A newly created SQLite engine whose database is still empty.
    """
    global _schema_template
    with _schema_template_lock:
        if _schema_template is None:
            from shortlist.server.db.models import Base

            with disposing_engine(create_engine("sqlite://")) as builder:
                Base.metadata.create_all(builder)
                template = sqlite3.connect(":memory:", check_same_thread=False)
                with builder.connect() as conn:
                    conn.connection.driver_connection.backup(template)
            _schema_template = template
        with engine.connect() as conn:
            _schema_template.backup(conn.connection.driver_connection)
