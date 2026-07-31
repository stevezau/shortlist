"""The update check must be right about "newer", and silent about everything else."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shortlist.server import version_check


@pytest.fixture(autouse=True)
def _reset_cache():
    version_check._cache.update(at=None, value=None)
    yield
    version_check._cache.update(at=None, value=None)


def _stub_latest(monkeypatch, value):
    monkeypatch.setattr(version_check, "_fetch_latest", lambda: value)


def test_reports_a_strictly_newer_release(monkeypatch):
    _stub_latest(monkeypatch, {"tag": "v0.2.0", "url": "https://example/rel"})
    result = version_check.check_for_update("0.1.0.dev0")
    assert result == {"latest": "0.2.0", "url": "https://example/rel"}


def test_silent_when_current_is_up_to_date(monkeypatch):
    _stub_latest(monkeypatch, {"tag": "v0.2.0", "url": "u"})
    assert version_check.check_for_update("0.2.0") is None  # equal
    version_check._cache.update(at=None, value=None)
    _stub_latest(monkeypatch, {"tag": "v0.1.0", "url": "u"})
    assert version_check.check_for_update("0.2.0") is None  # older release than running


def test_swallows_a_failed_fetch(monkeypatch):
    def boom():
        raise RuntimeError("github down")

    # _fetch_latest itself catches; simulate the caught result (None) and assert no raise, no update.
    monkeypatch.setattr(version_check, "_fetch_latest", lambda: None)
    assert version_check.check_for_update("0.1.0") is None
    # And the real _fetch_latest never propagates a network error.
    monkeypatch.setattr(version_check.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(boom()))
    assert version_check._fetch_latest() is None


def test_caches_between_calls(monkeypatch):
    calls = {"n": 0}

    def counting():
        calls["n"] += 1
        return {"tag": "v9.9.9", "url": "u"}

    monkeypatch.setattr(version_check, "_fetch_latest", counting)
    version_check.check_for_update("0.1.0")
    version_check.check_for_update("0.1.0")
    assert calls["n"] == 1  # second call served from cache, not a second fetch


def test_a_bad_tag_is_ignored(monkeypatch):
    _stub_latest(monkeypatch, {"tag": "not-a-version", "url": "u"})
    version_check._cache.update(at=datetime.now(UTC), value={"tag": "not-a-version", "url": "u"})
    assert version_check.check_for_update("0.1.0") is None


class TestVersionInfo:
    """The About panel and the notification bell read the SAME check.

    There used to be a second implementation under `services/`, with its own URL (`/releases`, whose
    first entry can be a pre-release), its own cache and its own comparison — and on this very build
    the two disagreed: the bell said "up to date" while About offered an update.
    """

    def test_it_reports_the_running_build(self, monkeypatch):
        import shortlist

        _stub_latest(monkeypatch, None)
        info = version_check.version_info()
        assert info["current_version"] == shortlist.__version__
        assert info["latest_version"] is None  # GitHub unreachable / no releases
        assert info["update_available"] is False

    def test_update_available_agrees_with_the_bell(self, monkeypatch):
        """One answer, not two comparisons: whatever `check_for_update` says, this must say."""
        _stub_latest(monkeypatch, {"tag": "v9.9.9", "url": "https://example/rel"})
        info = version_check.version_info()
        assert info["latest_version"] == "9.9.9"
        assert info["update_available"] is (version_check.check_for_update(info["current_version"]) is not None)
        assert info["update_available"] is True

    def test_the_newest_release_is_reported_even_when_it_is_not_newer(self, monkeypatch):
        """About shows what the latest release IS; only `update_available` judges it."""
        _stub_latest(monkeypatch, {"tag": "v0.0.1", "url": "u"})
        info = version_check.version_info()
        assert info["latest_version"] == "0.0.1"
        assert info["update_available"] is False

    def test_install_type_comes_from_the_image_build_args(self, monkeypatch):
        monkeypatch.delenv("GIT_SHA", raising=False)
        monkeypatch.delenv("GIT_BRANCH", raising=False)
        assert version_check._install_type() == "source"
        monkeypatch.setenv("GIT_SHA", "abc123")
        assert version_check._install_type() == "docker"
        monkeypatch.setenv("GIT_BRANCH", "dev")
        assert version_check._install_type() == "dev_docker"
