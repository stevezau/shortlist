#!/usr/bin/env python3
"""Fail when requirements.lock does not satisfy pyproject.toml's shipped dependencies.

The image installs the lock and then the project with `--no-deps`, so a version pyproject asks for reaches
users only once it is in the lock. Dependabot's pip PRs bump pyproject.toml alone, and a check that compared
names only let authlib sit at 1.9.1 in pyproject while the image shipped 1.8.0 (v1.12.2 release review); CI's
own tests ran 1.9.1, because `pip install -e .[dev]` upgrades past the lock.

Deliberately checks that each locked version SATISFIES its requirement, not that the lock equals a fresh
resolution: re-resolving would report drift every time any upstream package publishes.

    python scripts/check_requirements_lock.py     # CI lint job; regenerate the lock with the command in CLAUDE.md
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
# The extras the Dockerfile bundles into the image; `dev` is intentionally not locked.
SHIPPED_EXTRAS = ("anthropic", "openai", "google", "posters")
# What the lock is resolved for: the image's interpreter and OS (the `uv pip compile` flags in CLAUDE.md).
IMAGE_ENVIRONMENT = {"python_version": "3.12", "python_full_version": "3.12.0", "sys_platform": "linux"}
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)")


def lock_problems(pyproject: dict, lock_text: str) -> list[str]:
    """Each shipped requirement the lock is missing or holds at a version that does not satisfy it."""
    project = pyproject["project"]
    declared = list(project["dependencies"])
    for extra in SHIPPED_EXTRAS:
        declared += project.get("optional-dependencies", {}).get(extra, [])
    locked = {
        canonicalize_name(match[1]): Version(match[2]) for line in lock_text.splitlines() if (match := _PIN.match(line))
    }
    problems = []
    for requirement in map(Requirement, declared):
        if requirement.marker is not None and not requirement.marker.evaluate(IMAGE_ENVIRONMENT):
            continue
        name = canonicalize_name(requirement.name)
        version = locked.get(name)
        if version is None:
            problems.append(f"{name}: missing from the lock")
        elif not requirement.specifier.contains(version, prereleases=True):
            problems.append(f"{name}: the lock has {version}, which does not satisfy {requirement.specifier}")
    return problems


def main() -> int:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    problems = lock_problems(pyproject, (REPO_ROOT / "requirements.lock").read_text())
    if problems:
        print("requirements.lock is stale:", *problems, sep="\n  ")
        print("Regenerate it with the `uv pip compile` command in .claude/CLAUDE.md")
        return 1
    print("requirements.lock satisfies every shipped pyproject dependency")
    return 0


if __name__ == "__main__":
    sys.exit(main())
