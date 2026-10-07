"""Keep backend test scratch on disk and under pytest's locked, bounded retention."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import pytest


def _filesystem_type(path: Path) -> str:
    # Resolve aliases before matching mounts, including a not-yet-created final directory.
    resolved = path.resolve()
    mount_type = ""
    mount_depth = -1
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        mount, filesystem = line.split(" - ", 1)
        mountpoint = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), mount.split()[4]))
        if resolved.is_relative_to(mountpoint) and len(mountpoint.parts) >= mount_depth:
            mount_depth = len(mountpoint.parts)
            mount_type = filesystem.split()[0]
    if not mount_type:
        raise OSError(f"No filesystem mount found for {resolved}")
    return mount_type


def _require_disk(path: Path) -> None:
    if sys.platform != "linux":
        return
    try:
        filesystem = _filesystem_type(path)
    except (OSError, ValueError) as exc:
        raise pytest.UsageError(f"Cannot verify backend scratch filesystem for {path}: {exc}") from exc
    if filesystem in {"tmpfs", "ramfs", "devtmpfs", "hugetlbfs"}:
        raise pytest.UsageError(
            f"Backend test scratch {path} is RAM-backed ({filesystem}). "
            "Use PYTEST_DEBUG_TEMPROOT=/var/tmp and omit --basetemp."
        )


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Validate overrides before pytest's tmpdir plugin can create or erase scratch."""
    basetemp = config.option.basetemp
    if basetemp is not None:
        _require_disk(Path(basetemp))
        # xdist supplies each worker a directory inside the controller's managed run.
        if not hasattr(config, "workerinput"):
            raise pytest.UsageError(
                "Shortlist backend tests disallow --basetemp because it bypasses bounded retention "
                "and can erase an active run. Use PYTEST_DEBUG_TEMPROOT=/var/tmp (or another "
                "disk-backed parent); pytest manages the run directories."
            )

    count = config.getini("tmp_path_retention_count")
    policy = config.getini("tmp_path_retention_policy")
    if str(count) not in {"1", "2"} or policy != "failed":
        raise pytest.UsageError(
            "Backend scratch requires tmp_path_retention_count=1 or 2 and "
            "tmp_path_retention_policy=failed, to preserve active locks and bound completed runs."
        )

    if hasattr(config, "workerinput"):
        return

    parent = Path(os.environ.get("PYTEST_DEBUG_TEMPROOT", "/var/tmp")).expanduser().resolve()
    worktree = str(Path(__file__).resolve().parents[1])
    identity = hashlib.sha256(worktree.encode()).hexdigest()[:12]
    root = parent / f"shortlist-pytest-{os.getuid()}-{identity}"
    _require_disk(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A stable namespace prevents this suite's retention from pruning another project's runs.
    environment = pytest.MonkeyPatch()
    environment.setenv("PYTEST_DEBUG_TEMPROOT", str(root))
    config.add_cleanup(environment.undo)
