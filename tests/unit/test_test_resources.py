"""The harness catches leaked databases and bounds collectible test metadata."""

from pathlib import Path

import pytest

from tests.unit.test_test_scratch import _assert_exit, _run_pytest

pytestmark = pytest.mark.real_migrations

_PLUGIN = 'pytest_plugins = ["tests.resources"]\n'


@pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="requires Linux file descriptors")
@pytest.mark.parametrize("resource", ["engine", "connection"])
def test_unclosed_database_fails_at_owning_test_teardown(tmp_path: Path, resource: str) -> None:
    setup = (
        "resource = create_engine(f\"sqlite:///{tmp_path / 'leak.sqlite'}\")\n"
        '    with resource.connect() as connection:\n        connection.exec_driver_sql("SELECT 1")\n'
        "    atexit.register(resource.dispose)\n"
        if resource == "engine"
        else 'resource = sqlite3.connect(tmp_path / "leak.sqlite")\n    atexit.register(resource.close)\n'
    )
    result = _run_pytest(
        tmp_path / "project",
        tmp_path / "scratch",
        conftest=_PLUGIN,
        source="import atexit\nimport sqlite3\nfrom sqlalchemy import create_engine\n\n"
        + "def test_leak(tmp_path):\n    "
        + setup,
    )

    _assert_exit(result, pytest.ExitCode.TESTS_FAILED)
    assert "Database handles remain open" in result.stdout
    assert "dispose()" in result.stdout
    assert "leak.sqlite" in result.stdout


def test_closed_database_and_wider_fixture_scope_are_preserved(tmp_path: Path) -> None:
    result = _run_pytest(
        tmp_path / "project",
        tmp_path / "scratch",
        conftest=_PLUGIN,
        source="""
        import pytest
        from sqlalchemy import create_engine

        @pytest.fixture(scope="module")
        def shared(tmp_path_factory):
            path = tmp_path_factory.mktemp("module-db") / "shared.db"
            engine = create_engine(f"sqlite:///{path}")
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE shared (value INTEGER)")
                connection.exec_driver_sql("INSERT INTO shared VALUES (42)")
            try:
                yield engine
            finally:
                engine.dispose()

        @pytest.mark.parametrize("case", range(2))
        def test_isolation(shared, tmp_path, case):
            with shared.connect() as connection:
                assert connection.exec_driver_sql("SELECT value FROM shared").scalar() == 42
            engine = create_engine(f"sqlite:///{tmp_path / 'isolated.db'}")
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql("CREATE TABLE isolated (value INTEGER)")
            finally:
                engine.dispose()
        """,
    )

    _assert_exit(result, pytest.ExitCode.OK)
    assert "2 passed" in result.stdout


def test_unreachable_cycles_are_released_during_a_long_run(tmp_path: Path) -> None:
    result = _run_pytest(
        tmp_path / "project",
        tmp_path / "scratch",
        conftest=_PLUGIN,
        source="""
        import gc
        import weakref
        import pytest

        references = []

        class Cycle:
            def __init__(self):
                self.reference = self

        @pytest.fixture(scope="module", autouse=True)
        def manual_collection():
            gc.disable()
            try:
                yield
            finally:
                gc.enable()

        @pytest.mark.parametrize("case", range(60))
        def test_cycle(case):
            cycle = Cycle()
            references.append(weakref.ref(cycle))
            if case >= 50:
                assert references[0]() is None
        """,
    )

    _assert_exit(result, pytest.ExitCode.OK)
    assert "60 passed" in result.stdout
