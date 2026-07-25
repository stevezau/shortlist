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
_GITHUB_REPO = "stevezau/rowarr"
_RELEASES_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"

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
            tag = r.json().get("tag_name", "")
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
    """Parse a PEP 440-ish version into a comparable tuple. Pre-release suffixes sort below release."""
    m = re.match(r"(\d+(?:\.\d+)*)(.*)", v)
    if not m:
        return (0,)
    nums = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2)
    # No suffix = release (sorts higher), any suffix (a/b/rc/beta/dev) = pre-release
    return nums + ((1,) if not suffix else (0, suffix))


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
