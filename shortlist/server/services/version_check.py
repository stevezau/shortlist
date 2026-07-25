"""Version check against GitHub Releases — cached, non-blocking, graceful on failure."""

from __future__ import annotations

import os
import re
import threading
import time

import httpx
from loguru import logger

import shortlist

_CACHE_TTL = 3600  # 1 hour
_GITHUB_REPO = "stevezau/shortlist"
_RELEASES_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases"

_lock = threading.Lock()
_cached_latest: str | None = None
_cached_at: float = 0


def current_version() -> str:
    return shortlist.__version__


def _install_type() -> str:
    """Detect how the app was installed: docker (release), dev_docker, or source."""
    git_sha = os.environ.get("GIT_SHA", "")
    git_branch = os.environ.get("GIT_BRANCH", "")
    if git_sha and git_branch == "dev":
        return "dev_docker"
    if git_sha:
        return "docker"
    return "source"


def _fetch_latest() -> str | None:
    """Fetch the latest release tag from GitHub. Returns None on any failure."""
    try:
        r = httpx.get(
            _RELEASES_URL, timeout=10, follow_redirects=True, headers={"Accept": "application/vnd.github.v3+json"}
        )
        if r.status_code == 200:
            releases = r.json()
            if releases:
                tag = releases[0].get("tag_name", "")
                return tag.lstrip("v")
    except Exception as e:
        logger.debug("version check failed: {}", e)
    return None


def latest_version() -> str | None:
    """Latest release version (cached 1h). Returns None if unknown."""
    global _cached_latest, _cached_at
    now = time.monotonic()
    if _cached_latest is not None and (now - _cached_at) < _CACHE_TTL:
        return _cached_latest
    with _lock:
        if _cached_latest is not None and (now - _cached_at) < _CACHE_TTL:
            return _cached_latest
        result = _fetch_latest()
        if result:
            _cached_latest = result
            _cached_at = now
        return _cached_latest


def _parse_version(v: str) -> tuple:
    """Parse a PEP 440-ish version into a comparable tuple. Pre-release suffixes sort below release.

    Handles both PEP 440 (0.1.0b5) and GitHub-style (0.1.0-beta.5) pre-release tags.
    """
    m = re.match(r"(\d+(?:\.\d+)*)(.*)", v)
    if not m:
        return (0,)
    nums = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2)
    if not suffix:
        return (*nums, 1, 0)  # release sorts above any pre-release
    # Normalize: "b5", "-beta.5", "-beta5" all become pre-release number 5
    pre_match = re.search(r"(\d+)", suffix)
    pre_num = int(pre_match.group(1)) if pre_match else 0
    return (*nums, 0, pre_num)


def update_available() -> bool:
    """True if a newer release exists on GitHub."""
    latest = latest_version()
    if not latest:
        return False
    return _parse_version(latest) > _parse_version(current_version())


def version_info() -> dict:
    """Full version info for the API."""
    latest = latest_version()
    current = current_version()
    install = _install_type()
    has_update = _parse_version(latest) > _parse_version(current) if latest else False
    return {
        "current_version": current,
        "latest_version": latest,
        "update_available": has_update,
        "install_type": install,
    }
