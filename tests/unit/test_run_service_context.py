"""RunService.build_context branch matrix + the server-side privacy gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import rowarr.server.services.run_service as run_service_mod
from rowarr.engine.history import FallbackHistorySource, PlexHistorySource
from rowarr.server.db.models import PickRow, PrivacyCheck, Server, User
from rowarr.server.db.session import make_engine, make_session_factory, run_migrations
from rowarr.server.services.run_service import RunService
from rowarr.server.services.secrets import SecretBox
from rowarr.server.services.sse import EventBus
from rowarr.server.settings_store import SettingsStore


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    yield factory
    engine.dispose()


@pytest.fixture
def service(sessions, tmp_path):
    return RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))


@pytest.fixture
def configured(sessions, tmp_path, monkeypatch):
    """Configure plex+tmdb settings and stub the vendor client constructors (the boundary)."""
    box = SecretBox(tmp_path)
    with sessions() as session:
        store = SettingsStore(session, box)
        store.set("plex.url", "http://pms:32400")
        store.set("plex.token", "tok")
        store.set("tmdb.apikey", "k")
    plex_client = MagicMock()
    plex_client.machine_id = "m1"
    monkeypatch.setattr(run_service_mod, "PlexClient", lambda url, token: plex_client)
    monkeypatch.setattr(run_service_mod, "PlexTvClient", lambda *a, **k: MagicMock())
    monkeypatch.setattr(run_service_mod, "TmdbClient", lambda *a, **k: MagicMock())
    return box


class TestBuildContext:
    def test_unconfigured_raises_plainly(self, service):
        with pytest.raises(RuntimeError, match="not configured"):
            service.build_context(dry_run=True)

    def test_no_tautulli_uses_plex_history(self, service, configured):
        ctx = service.build_context(dry_run=True)
        assert isinstance(ctx.history_source, PlexHistorySource)
        assert ctx.curator.name == "none"
        assert ctx.config.dry_run is True

    def test_tautulli_configured_uses_per_user_fallback(self, service, sessions, configured):
        with sessions() as session:
            store = SettingsStore(session, configured)
            store.set("tautulli.url", "http://taut:8181")
            store.set("tautulli.apikey", "tk")
        ctx = service.build_context(dry_run=False)
        assert isinstance(ctx.history_source, FallbackHistorySource)

    def test_recent_picks_window_respects_staleness_config(self, service, sessions, configured):
        from rowarr.server.db.models import Run

        with sessions() as session:
            store = SettingsStore(session, configured)
            store.set("staleness_runs", 2)
            store.set("row.size", 3)  # window = 6
            session.add(User(plex_account_id=1, username="sarah", slug="sarah", enabled=True))
            run = Run(trigger="manual", status="ok", dry_run=False, stats={})
            session.add(run)
            session.commit()
            user_id = session.query(User).one().id
            for i in range(10):
                session.add(PickRow(run_id=run.id, user_id=user_id, tmdb_id=100 + i, rating_key=i, rank=1))
            session.commit()
        ctx = service.build_context(dry_run=True)
        assert ctx.recent_picks["sarah"] == {104, 105, 106, 107, 108, 109}  # newest 6 only


class TestServerPrivacyGate:
    """Matrix: no check / failing / stale / old PMS / pass."""

    def _add_server(self, sessions, version="1.43.3.10793"):
        with sessions() as session:
            session.add(
                Server(
                    machine_id="m1",
                    url="u",
                    token_enc="x",
                    version=version,
                    owner_account_id=1,
                    plex_pass=True,
                    capabilities={},
                )
            )
            session.commit()

    def _add_check(self, sessions, *, passed=True, days_old=0):
        with sessions() as session:
            session.add(
                PrivacyCheck(tier="T1", passed=passed, ran_at=datetime.now(UTC) - timedelta(days=days_old), detail={})
            )
            session.commit()

    def test_no_check_on_record_refuses(self, service, sessions):
        self._add_server(sessions)
        assert "no Privacy Check" in service._privacy_gate_error()

    def test_failing_check_refuses(self, service, sessions):
        self._add_server(sessions)
        self._add_check(sessions, passed=False)
        assert "FAILED" in service._privacy_gate_error()

    def test_stale_check_refuses(self, service, sessions):
        self._add_server(sessions)
        self._add_check(sessions, days_old=8)
        assert "last passed 8 days ago" in service._privacy_gate_error()

    def test_a_fresh_t1_cannot_carry_a_stale_t2_along(self, service, sessions):
        """Staleness is measured from the OLDEST tier, not the newest check."""
        self._add_server(sessions)
        with sessions() as session:
            session.add(PrivacyCheck(tier="T2", passed=True, ran_at=datetime.now(UTC) - timedelta(days=60), detail={}))
            session.add(PrivacyCheck(tier="T1", passed=True, ran_at=datetime.now(UTC), detail={}))
            session.commit()
        error = service._privacy_gate_error()
        assert error is not None and "T2" in error

    def test_old_pms_refuses_even_with_fresh_pass(self, service, sessions):
        self._add_server(sessions, version="1.42.0.9000")
        self._add_check(sessions)
        assert "upgrade Plex" in service._privacy_gate_error()

    def test_fresh_pass_and_new_pms_opens_the_gate(self, service, sessions):
        self._add_server(sessions)
        self._add_check(sessions)
        assert service._privacy_gate_error() is None

    def test_gated_run_is_marked_error_without_touching_engine(self, service, sessions, monkeypatch):
        import asyncio

        called = MagicMock()
        monkeypatch.setattr(run_service_mod, "engine_run", called)

        async def scenario():
            run_id = await service.start_run(trigger="schedule", dry_run=False)
            for _ in range(100):
                with sessions() as session:
                    from rowarr.server.db.models import Run

                    run = session.get(Run, run_id)
                    if run.status == "error":
                        return run.stats
                await asyncio.sleep(0.02)
            raise AssertionError("gate did not trip")

        stats = asyncio.run(scenario())
        assert "privacy gate" in stats["error"]
        called.assert_not_called()
