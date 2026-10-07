"""Database ownership closes the actual pooled SQLite connections on every exit."""

import sqlite3
from contextlib import nullcontext

import pytest
from sqlalchemy import create_engine

from tests.db_helpers import disposing_engine


@pytest.mark.parametrize("fail", [False, True])
def test_disposing_engine_closes_pooled_connection_even_after_an_error(fail: bool) -> None:
    expected = pytest.raises(RuntimeError, match="failed test setup") if fail else nullcontext()
    with expected, disposing_engine(create_engine("sqlite://")) as engine:
        with engine.connect() as connection:
            raw_connection = connection.connection.driver_connection
            assert connection.exec_driver_sql("SELECT 1").scalar() == 1
        if fail:
            raise RuntimeError("failed test setup")

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        raw_connection.execute("SELECT 1")
