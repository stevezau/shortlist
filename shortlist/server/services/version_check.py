"""Version check against GitHub Releases — cached, non-blocking, graceful on failure."""

from __future__ import annotations

import os
import threading
import time

import httpx
from loguru import logger
from packaging.version import InvalidVersion, Version

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
        r = httpx.get(_RELEASES_URL, timeout=10, headers={"Accept": "application/vnd.github.v3+json"})
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


def update_available() -> bool:
    """True if a newer release exists on GitHub."""
    latest = latest_version()
    if not latest:
        return False
    try:
        return Version(latest) > Version(current_version())
    except InvalidVersion:
        return False


def version_info() -> dict:
    """Full version info for the API."""
    latest = latest_version()
    current = current_version()
    install = _install_type()
    try:
        has_update = Version(latest) > Version(current) if latest else False
    except InvalidVersion:
        has_update = False
    return {
        "current_version": current,
        "latest_version": latest,
        "update_available": has_update,
        "install_type": install,
    }
