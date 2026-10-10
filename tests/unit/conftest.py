"""Fixtures shared by the unit tests. A module that needs a seeded database defines its own `sessions`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tests.db_helpers import create_schema, disposing_engine


@pytest.fixture
def sessions() -> Iterator[sessionmaker]:
    """A session factory over an empty in-memory database with the full schema."""
    with disposing_engine(create_engine("sqlite://")) as engine:
        create_schema(engine)
        yield sessionmaker(engine)


@pytest.fixture
def threaded_sessions() -> Iterator[sessionmaker]:
    """`sessions`, but one shared connection, for code that touches the database from a worker thread.

    SQLite's default pooling hands a new thread its OWN connection, which for `sqlite://` means its own
    empty in-memory database: without `StaticPool` the writes land somewhere nothing can read.
    """
    with disposing_engine(
        create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    ) as engine:
        create_schema(engine)
        yield sessionmaker(engine)
