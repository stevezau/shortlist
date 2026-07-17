"""The unauthenticated PIN endpoint is rate-limited so it can't be spammed to hammer plex.tv:
a per-IP cap for the honest-proxy case, plus a global ceiling that holds even if X-Forwarded-For
(and thus the per-IP key) is spoofed."""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from shortlist.server import auth


def _request(ip: str) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host=ip))


class TestPinRateLimit:
    def setup_method(self):
        auth._PIN_HITS.clear()
        auth._PIN_ALL.clear()

    def teardown_method(self):
        auth._PIN_HITS.clear()
        auth._PIN_ALL.clear()

    def test_allows_up_to_the_per_ip_cap_then_refuses(self):
        req = _request("9.9.9.9")
        for _ in range(auth._PIN_MAX_PER_WINDOW):
            auth._rate_limit_pin(req)  # the first N in the window are fine
        with pytest.raises(HTTPException) as exc:
            auth._rate_limit_pin(req)  # the (N+1)th trips the per-IP limiter
        assert exc.value.status_code == 429

    def test_limit_is_per_ip(self):
        for _ in range(auth._PIN_MAX_PER_WINDOW):
            auth._rate_limit_pin(_request("1.1.1.1"))
        auth._rate_limit_pin(_request("2.2.2.2"))  # a different IP is unaffected

    def test_an_ip_recovers_after_its_window_elapses(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock["now"])
        req = _request("3.3.3.3")
        for _ in range(auth._PIN_MAX_PER_WINDOW):
            auth._rate_limit_pin(req)
        with pytest.raises(HTTPException):
            auth._rate_limit_pin(req)
        clock["now"] += auth._PIN_WINDOW_S + 1  # window elapses
        auth._rate_limit_pin(req)  # allowed again

    def test_global_ceiling_holds_when_the_per_ip_key_is_spoofed(self):
        # Every request from a fresh IP (as header-rotation would produce) still counts globally.
        for i in range(auth._PIN_MAX_GLOBAL):
            auth._rate_limit_pin(_request(f"10.0.0.{i}"))
        with pytest.raises(HTTPException) as exc:
            auth._rate_limit_pin(_request("10.0.9.9"))
        assert exc.value.status_code == 429

    def test_memory_cleanup_drops_stale_ips_in_place(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock["now"])
        same_dict = auth._PIN_HITS
        # Seed >4096 stale IPs whose windows have long since expired.
        for i in range(5000):
            auth._PIN_HITS[f"stale-{i}"] = deque([clock["now"]])
        clock["now"] += auth._PIN_WINDOW_S + 1
        auth._rate_limit_pin(_request("live"))
        assert auth._PIN_HITS is same_dict  # never reassigned — still the module dict
        assert len(auth._PIN_HITS) < 5000  # stale entries pruned
        assert "live" in auth._PIN_HITS  # the active IP survives


class TestPollRateLimit:
    """poll_pin is the login handshake (can't require auth) and proxies to plex.tv on every call,
    so a GLOBAL-only ceiling bounds total amplification without a per-IP cap that would break the
    legit client's ~1.5s polling."""

    def setup_method(self):
        auth._POLL_ALL.clear()

    def teardown_method(self):
        auth._POLL_ALL.clear()

    def test_global_cap_bounds_poll_amplification(self):
        for _ in range(auth._POLL_MAX_GLOBAL):
            auth._rate_limit_poll()  # the first N in the window are fine
        with pytest.raises(HTTPException) as exc:
            auth._rate_limit_poll()  # the (N+1)th trips the global ceiling
        assert exc.value.status_code == 429

    def test_poll_budget_recovers_after_the_window(self, monkeypatch):
        clock = {"now": 1000.0}
        monkeypatch.setattr(auth.time, "monotonic", lambda: clock["now"])
        for _ in range(auth._POLL_MAX_GLOBAL):
            auth._rate_limit_poll()
        with pytest.raises(HTTPException):
            auth._rate_limit_poll()
        clock["now"] += auth._PIN_WINDOW_S + 1  # window elapses
        auth._rate_limit_poll()  # allowed again
