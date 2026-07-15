"""The shared HTTP retry/backoff layer for the service clients."""

from __future__ import annotations

import httpx
import pytest

from shortlist.engine.clients import http_retry


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Record backoff durations without actually waiting, so the retry tests run instantly."""
    waited: list[float] = []
    monkeypatch.setattr(http_retry.time, "sleep", waited.append)
    return waited


def _responder(monkeypatch, sequence):
    """Drive http_retry off a scripted sequence of results — each item is a Response or an Exception."""
    calls: list[str] = []

    def fake_request(method, url, **kwargs):
        calls.append(method)
        item = sequence[min(len(calls) - 1, len(sequence) - 1)]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(httpx, "request", fake_request)
    return calls


class TestGet:
    def test_retries_a_read_timeout_then_succeeds(self, monkeypatch, _no_real_sleep):
        calls = _responder(monkeypatch, [httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"), httpx.Response(200)])
        r = http_retry.get("http://svc/x", attempts=3)
        assert r.status_code == 200
        assert len(calls) == 3 and len(_no_real_sleep) == 2  # two backoffs before the win

    def test_gives_up_after_attempts_and_reraises(self, monkeypatch):
        _responder(monkeypatch, [httpx.ConnectTimeout("down")])
        with pytest.raises(httpx.ConnectTimeout):
            http_retry.get("http://svc/x", attempts=3)

    def test_retries_429_and_honours_retry_after(self, monkeypatch, _no_real_sleep):
        _responder(monkeypatch, [httpx.Response(429, headers={"Retry-After": "2"}), httpx.Response(200)])
        r = http_retry.get("http://svc/x", attempts=3)
        assert r.status_code == 200
        assert _no_real_sleep == [2.0]  # the server's Retry-After, not the computed backoff

    def test_retries_a_500(self, monkeypatch, _no_real_sleep):
        calls = _responder(monkeypatch, [httpx.Response(503), httpx.Response(200)])
        assert http_retry.get("http://svc/x", attempts=3).status_code == 200
        assert len(calls) == 2

    def test_does_not_retry_a_404(self, monkeypatch):
        calls = _responder(monkeypatch, [httpx.Response(404), httpx.Response(200)])
        r = http_retry.get("http://svc/x", attempts=3)
        assert r.status_code == 404 and len(calls) == 1  # a 4xx (not 429) is a real answer, not transient


class TestWrite:
    def test_does_not_retry_a_read_timeout(self, monkeypatch):
        # A read timeout on a POST/PUT may mean the mutation already applied — retrying could double it.
        _responder(monkeypatch, [httpx.ReadTimeout("maybe applied")])
        with pytest.raises(httpx.ReadTimeout):
            http_retry.request("POST", "http://svc/x", attempts=3)

    def test_retries_a_connect_error(self, monkeypatch, _no_real_sleep):
        # A connect error proves the request never reached the server, so re-sending is safe.
        calls = _responder(monkeypatch, [httpx.ConnectError("never sent"), httpx.Response(201)])
        assert http_retry.request("POST", "http://svc/x", attempts=3).status_code == 201
        assert len(calls) == 2

    def test_retries_a_429_but_not_a_500(self, monkeypatch, _no_real_sleep):
        calls_429 = _responder(monkeypatch, [httpx.Response(429), httpx.Response(200)])
        assert http_retry.request("PUT", "http://svc/x", attempts=3).status_code == 200
        assert len(calls_429) == 2
        # A 5xx on a write is NOT retried (the mutation may have applied server-side).
        calls_500 = _responder(monkeypatch, [httpx.Response(500), httpx.Response(200)])
        assert http_retry.request("PUT", "http://svc/x", attempts=3).status_code == 500
        assert len(calls_500) == 1
