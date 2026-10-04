"""The dashboard report cache: served from memory within the TTL, dropped when its data changes."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from shortlist.server.api import report as report_api
from shortlist.server.db.models import PickRow, Run, User
from shortlist.server.services import jobs, report_cache, report_service, run_persistence
from shortlist.server.services.watch_stream import STREAM_CONNECTED_KEY, STREAM_DOWN_SINCE_KEY
from shortlist.server.settings_store import SettingsStore

pytestmark = pytest.mark.integration


@pytest.fixture
def computes(monkeypatch) -> list[str]:
    """The windows the report was actually COMPUTED for (a cache hit appends nothing)."""
    calls: list[str] = []
    real = report_service.effectiveness

    def spy(session, window, **kwargs):
        calls.append(window)
        return real(session, window, **kwargs)

    monkeypatch.setattr(report_service, "effectiveness", spy)
    return calls


class TestReportCache:
    def test_a_second_call_within_the_ttl_is_served_from_memory(self, client: TestClient, computes: list[str]):
        first = client.get("/api/report").json()
        second = client.get("/api/report").json()

        assert second == first
        assert computes == ["30"]

    def test_invalidating_forces_a_recompute(self, client: TestClient, computes: list[str]):
        client.get("/api/report")
        report_cache.invalidate_report_cache()
        client.get("/api/report")

        assert computes == ["30", "30"]

    def test_an_expired_entry_is_recomputed(self, client: TestClient, computes: list[str], monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(report_cache.time, "monotonic", lambda: now[0])
        client.get("/api/report")

        now[0] += report_cache.TTL_SECONDS - 1
        client.get("/api/report")
        assert computes == ["30"], "still fresh just inside the TTL"

        now[0] += 2
        client.get("/api/report")
        assert computes == ["30", "30"]

    def test_each_window_is_cached_on_its_own(self, client: TestClient, computes: list[str]):
        client.get("/api/report?window=7")
        client.get("/api/report?window=90")
        client.get("/api/report?window=7")
        client.get("/api/report?window=bogus")
        client.get("/api/report")

        assert computes == ["7", "90", "30"], "an unknown window shares the default window's entry"

    def test_next_watch_sync_is_read_per_request_not_cached(self, client: TestClient, monkeypatch):
        stamps = iter(["2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z"])
        monkeypatch.setattr(report_api, "iso_utc", lambda _t: next(stamps))
        scheduler = SimpleNamespace(get_job=lambda _id: SimpleNamespace(next_run_time=object()))
        monkeypatch.setattr(client.app.state, "scheduler", scheduler, raising=False)

        first = client.get("/api/report").json()["watch_sync"]["next"]
        second = client.get("/api/report").json()["watch_sync"]["next"]

        assert (first, second) == ("2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z")

    def test_the_cache_hands_out_copies(self):
        report_cache.store_report("30", {"watch_sync": {"next": None}})

        report_cache.get_cached_report("30")["watch_sync"]["next"] = "mutated"

        assert report_cache.get_cached_report("30") == {"watch_sync": {"next": None}}

    def test_clearing_deleted_rows_invalidates(self, client: TestClient, computes: list[str]):
        with client.app.state.sessions() as session:
            user = session.query(User).filter_by(slug="sarah").one()
            run = Run(trigger="manual", status="ok")
            session.add(run)
            session.flush()
            session.add(
                PickRow(
                    run_id=run.id,
                    user_id=user.id,
                    tmdb_id=1,
                    media_type="movie",
                    rating_key=1,
                    rank=1,
                    collection_slug="gone",
                    title="T",
                )
            )
            session.commit()
        client.get("/api/report")

        assert client.delete("/api/report/deleted-rows").json()["cleared"] == 1
        client.get("/api/report")

        assert computes == ["30", "30"]

    def test_clearing_run_history_invalidates(self, client: TestClient, computes: list[str]):
        client.get("/api/report")

        assert client.delete("/api/runs").status_code == 200
        client.get("/api/report")

        assert computes == ["30", "30"]

    def test_live_listener_status_is_read_per_request_while_the_rest_stays_cached(
        self, client: TestClient, computes: list[str]
    ):
        def live() -> tuple[str | None, str | None]:
            watch_sync = client.get("/api/report").json()["watch_sync"]
            return watch_sync["live_since"], watch_sync["live_down_since"]

        assert live() == (None, None)
        with client.app.state.sessions() as session:
            store = SettingsStore(session)
            store.set(STREAM_CONNECTED_KEY, "2026-10-05T01:00:00Z")
            store.set(STREAM_DOWN_SINCE_KEY, "2026-10-05T02:00:00Z")
            session.commit()

        assert live() == ("2026-10-05T01:00:00Z", "2026-10-05T02:00:00Z")
        assert computes == ["30"], "the report itself was served from the cache"

    def test_a_store_after_an_interleaved_invalidate_is_discarded(self):
        generation = report_cache.current_generation()
        report_cache.invalidate_report_cache()

        report_cache.store_report("30", {"stale": True}, generation)

        assert report_cache.get_cached_report("30") is None

    def test_a_store_with_the_current_generation_is_kept(self):
        report_cache.store_report("30", {"fresh": True}, report_cache.current_generation())

        assert report_cache.get_cached_report("30") == {"fresh": True}


class TestReconcileInvalidates:
    def test_a_credit_drops_the_cache_before_the_sse_goes_out(self, monkeypatch):
        monkeypatch.setattr(run_persistence, "reconcile_from_events", lambda _sessions: 2)
        report_cache.store_report("30", {"x": 1})
        seen: list[dict | None] = []
        bus = MagicMock()
        bus.publish.side_effect = lambda *_a: seen.append(report_cache.get_cached_report("30"))

        jobs._watch_reconcile(SimpleNamespace(sessions=None, bus=bus), {})

        assert seen == [None], "a listener refetching on the event must not be served the stale report"
        assert bus.publish.call_args.args == ("sync.finished", {"kind": "credited", "ok": True, "count": 2})

    def test_nothing_credited_keeps_the_cache(self, monkeypatch):
        monkeypatch.setattr(run_persistence, "reconcile_from_events", lambda _sessions: 0)
        report_cache.store_report("30", {"x": 1})

        jobs._watch_reconcile(SimpleNamespace(sessions=None, bus=MagicMock()), {})

        assert report_cache.get_cached_report("30") == {"x": 1}
