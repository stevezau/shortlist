"""scripts/check_requirements_lock.py: the image installs requirements.lock, so it must satisfy pyproject.toml.

Dependabot's pip PRs bump pyproject.toml alone. A check that compared package names only passed authlib pinned
at 1.9.1 while the image shipped the 1.8.0 still in the lock (v1.12.2 release review).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_requirements_lock.py"


def _load():
    """Import the checker by path — `scripts/` is not a package, deliberately."""
    spec = importlib.util.spec_from_file_location("check_requirements_lock", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lock_check = _load()


def _project(dependencies: list[str], **extras: list[str]) -> dict:
    return {"project": {"dependencies": dependencies, "optional-dependencies": extras}}


def test_the_repository_s_lock_satisfies_its_pyproject():
    assert lock_check.main() == 0


def test_a_pin_bumped_in_pyproject_but_not_the_lock_fails():
    problems = lock_check.lock_problems(_project(["Authlib==1.9.1"]), "authlib==1.8.0\n")

    assert problems == ["authlib: the lock has 1.8.0, which does not satisfy ==1.9.1"]


def test_a_lock_below_a_raised_minimum_fails():
    problems = lock_check.lock_problems(_project(["httpx>=0.28"]), "httpx==0.27.2\n")

    assert problems == ["httpx: the lock has 0.27.2, which does not satisfy >=0.28"]


def test_a_dependency_missing_from_the_lock_fails():
    problems = lock_check.lock_problems(_project(["loguru>=0.7"]), "httpx==0.28.1\n")

    assert problems == ["loguru: missing from the lock"]


def test_a_shipped_extra_is_checked_and_the_dev_extra_is_not():
    project = _project([], anthropic=["anthropic>=0.40"], dev=["pytest>=9"])

    problems = lock_check.lock_problems(project, "anthropic==0.39.0\n")

    assert problems == ["anthropic: the lock has 0.39.0, which does not satisfy >=0.40"]


def test_names_match_however_they_are_spelled():
    lock = "google-genai==1.2.0\n    # via shortlist\npython-multipart==0.0.20\n"

    assert lock_check.lock_problems(_project(["Google_GenAI>=1.0", "python.multipart>=0.0.9"]), lock) == []


def test_a_requirement_with_extras_is_matched_by_its_name():
    assert lock_check.lock_problems(_project(["uvicorn[standard]>=0.30"]), "uvicorn==0.34.0\n") == []


def test_a_requirement_for_another_platform_is_not_required():
    """The lock is resolved for the image (Linux, Python 3.12), so it holds nothing for other platforms."""
    project = _project(['pywin32>=306; sys_platform == "win32"'])

    assert lock_check.lock_problems(project, "httpx==0.28.1\n") == []
