"""Exercise scratch safeguards before pytest allocates databases or removes files."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.real_migrations

_REPO_ROOT = Path(__file__).resolve().parents[2]
_NEEDS_SHM = pytest.mark.skipif(
    sys.platform != "linux" or not Path("/dev/shm").is_dir(), reason="requires Linux shared memory"
)
_SAMPLE = """
from pathlib import Path

def test_scratch(tmp_path):
    (tmp_path / "payload").write_text("test-owned data")
    Path("allocated.txt").write_text(str(tmp_path))
"""


def _run_pytest(
    project: Path,
    scratch: Path | None,
    *,
    source: str = _SAMPLE,
    args: tuple[str, ...] = (),
    conftest: str = "",
    filename: str = "test_sample.py",
) -> subprocess.CompletedProcess[str]:
    project.mkdir(exist_ok=True)
    (project / "pytest.ini").write_text(
        "[pytest]\ntmp_path_retention_count = 2\ntmp_path_retention_policy = failed\n"
        "markers = real_migrations: requires an empty database directory\n"
    )
    (project / filename).write_text(textwrap.dedent(source))
    (project / "conftest.py").write_text(conftest)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env.update(
        PYTHONPATH=str(_REPO_ROOT),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    if scratch is not None:
        env["PYTEST_DEBUG_TEMPROOT"] = str(scratch)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.scratch", "-c", "pytest.ini", "-q", *args, filename],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_exit(result: subprocess.CompletedProcess[str], code: int) -> None:
    assert result.returncode == code, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "linux", reason="Linux filesystem check")
@pytest.mark.parametrize("filesystem", ["tmpfs", "ramfs", "devtmpfs", "hugetlbfs"])
def test_memory_filesystems_are_rejected(monkeypatch: pytest.MonkeyPatch, filesystem: str) -> None:
    from tests import scratch

    monkeypatch.setattr(scratch, "_filesystem_type", lambda path: filesystem)
    with pytest.raises(pytest.UsageError, match="RAM-backed"):
        scratch._require_disk(Path("/var/tmp/disk-looking-path"))


@_NEEDS_SHM
@pytest.mark.parametrize("override", ["environment", "basetemp", "symlink"])
def test_ram_scratch_override_fails_before_touching_existing_files(tmp_path: Path, override: str) -> None:
    with tempfile.TemporaryDirectory(prefix="shortlist-scratch-guard-", dir="/dev/shm") as directory:
        ram_root = Path(directory)
        sentinel = ram_root / "active-test-file"
        sentinel.write_text("preserve me")
        scratch = ram_root
        args: tuple[str, ...] = ()
        if override == "basetemp":
            scratch = tmp_path / "disk-scratch"
            args = (f"--basetemp={ram_root}",)
        elif override == "symlink":
            scratch = tmp_path / "disk-looking-alias"
            scratch.symlink_to(ram_root, target_is_directory=True)

        result = _run_pytest(tmp_path / "project", scratch, args=args)

        _assert_exit(result, pytest.ExitCode.USAGE_ERROR)
        assert "RAM-backed" in result.stderr
        assert "/var/tmp" in result.stderr
        assert sentinel.read_text() == "preserve me"
        assert list(ram_root.iterdir()) == [sentinel]
        assert not (tmp_path / "project" / "allocated.txt").exists()


def test_explicit_disk_basetemp_cannot_bypass_retention(tmp_path: Path) -> None:
    basetemp = tmp_path / "explicit-base"
    basetemp.mkdir()
    sentinel = basetemp / "active-test-file"
    sentinel.write_text("preserve me")

    result = _run_pytest(tmp_path / "project", tmp_path / "scratch", args=(f"--basetemp={basetemp}",))

    _assert_exit(result, pytest.ExitCode.USAGE_ERROR)
    assert "PYTEST_DEBUG_TEMPROOT" in result.stderr
    assert "retention" in result.stderr
    assert sentinel.read_text() == "preserve me"


@pytest.mark.parametrize(
    "override",
    [
        "tmp_path_retention_count=3",
        "tmp_path_retention_count=0",
        "tmp_path_retention_count=-1",
        "tmp_path_retention_count=invalid",
        "tmp_path_retention_policy=all",
    ],
)
def test_unbounded_or_unnecessary_retention_is_rejected(tmp_path: Path, override: str) -> None:
    result = _run_pytest(tmp_path / "project", tmp_path / "scratch", args=("-o", override))

    _assert_exit(result, pytest.ExitCode.USAGE_ERROR)
    assert "retention" in result.stderr
    assert not (tmp_path / "project" / "allocated.txt").exists()


def test_successful_disk_run_removes_its_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    project = tmp_path / "project"

    result = _run_pytest(project, scratch)

    _assert_exit(result, pytest.ExitCode.OK)
    allocated = Path((project / "allocated.txt").read_text())
    assert allocated.is_relative_to(scratch)
    assert not allocated.exists()
    assert not list(scratch.rglob("payload"))


def test_default_scratch_root_is_stable_under_var_tmp(tmp_path: Path) -> None:
    project = tmp_path / "project"
    roots = []
    for _ in range(2):
        result = _run_pytest(
            project,
            None,
            source="""
            import os
            from pathlib import Path

            def test_default():
                Path("scratch-root.txt").write_text(os.environ["PYTEST_DEBUG_TEMPROOT"])
            """,
        )
        _assert_exit(result, pytest.ExitCode.OK)
        roots.append(Path((project / "scratch-root.txt").read_text()))

    assert roots[0] == roots[1]
    assert roots[0].is_relative_to(Path("/var/tmp"))


def test_failed_runs_keep_only_two_completed_directories(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    allocated = []

    for _ in range(4):
        result = _run_pytest(project, scratch, source=_SAMPLE + "    assert False\n")
        _assert_exit(result, pytest.ExitCode.TESTS_FAILED)
        allocated.append(Path((project / "allocated.txt").read_text()))

    assert [path.exists() for path in allocated] == [False, False, True, True]
    assert len(list(scratch.rglob("payload"))) == 2


def test_retention_preserves_a_locked_active_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    result = _run_pytest(project, scratch, source=_SAMPLE + "    assert False\n")
    _assert_exit(result, pytest.ExitCode.TESTS_FAILED)
    active = Path((project / "allocated.txt").read_text())
    lock = active.parent / ".lock"
    lock.write_text(str(os.getpid()))

    try:
        for _ in range(3):
            result = _run_pytest(project, scratch, source=_SAMPLE + "    assert False\n")
            _assert_exit(result, pytest.ExitCode.TESTS_FAILED)

        assert (active / "payload").read_text() == "test-owned data"
        assert lock.read_text() == str(os.getpid())
    finally:
        lock.unlink(missing_ok=True)


def test_pure_tests_do_not_create_a_database_or_scratch(tmp_path: Path) -> None:
    result = _run_pytest(
        tmp_path / "project",
        tmp_path / "scratch",
        conftest='pytest_plugins = ["tests.conftest"]\n',
        source="""
        def test_pure(request):
            assert request.config._tmp_path_factory._basetemp is None
        """,
    )

    _assert_exit(result, pytest.ExitCode.OK)
    assert not list((tmp_path / "scratch").rglob("shortlist.db"))


@pytest.mark.parametrize("filename", ["test_sample.py", "test_migrations_sample.py"])
def test_migration_optouts_do_not_build_a_schema_template(tmp_path: Path, filename: str) -> None:
    result = _run_pytest(
        tmp_path / "project",
        tmp_path / "scratch",
        conftest='pytest_plugins = ["tests.conftest"]\n',
        filename=filename,
        source="import pytest\n\n"
        + ("@pytest.mark.real_migrations\n" if filename == "test_sample.py" else "")
        + "def test_empty(tmp_path, request):\n"
        + "    assert not list(tmp_path.iterdir())\n"
        + '    assert not list(request.config._tmp_path_factory.getbasetemp().glob("schema-template*"))\n',
    )

    _assert_exit(result, pytest.ExitCode.OK)


def test_database_copies_remain_isolated(tmp_path: Path) -> None:
    result = _run_pytest(
        tmp_path / "project",
        tmp_path / "scratch",
        conftest='pytest_plugins = ["tests.conftest"]\n',
        source="""
        import sqlite3
        from contextlib import closing
        import pytest

        @pytest.mark.parametrize("case", range(2))
        def test_database_copy(tmp_path, case):
            database = tmp_path / "shortlist.db"
            assert database.exists()
            with closing(sqlite3.connect(database)) as connection, connection:
                assert connection.execute("SELECT version_num FROM alembic_version").fetchone()
                connection.execute("CREATE TABLE isolation_probe (value INTEGER)")
                connection.execute("INSERT INTO isolation_probe VALUES (?)", (case,))
        """,
    )

    _assert_exit(result, pytest.ExitCode.OK)
    assert "2 passed" in result.stdout
