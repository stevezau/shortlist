"""RunService.build_context branch matrix + the server-side privacy gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import rowarr.server.services.context_builder as context_builder_mod
import rowarr.server.services.run_service as run_service_mod
from rowarr.engine.history import FallbackHistorySource, PlexHistorySource
from rowarr.engine.models import MediaType, RunReport
from rowarr.server.db.models import PickRow, PrivacyCheck, Server, User
from rowarr.server.db.session import make_engine, make_session_factory, run_migrations
from rowarr.server.services.context_builder import ContextBuilder
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
    monkeypatch.setattr(context_builder_mod, "PlexClient", lambda url, token: plex_client)
    monkeypatch.setattr(context_builder_mod, "PlexTvClient", lambda *a, **k: MagicMock())
    monkeypatch.setattr(context_builder_mod, "TmdbClient", lambda *a, **k: MagicMock())
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

    def test_ollama_provider_is_built_with_its_url(self, service, sessions, configured):
        """Ollama takes a base URL and no key; the key was previously unstorable, 422ing setup."""
        with sessions() as session:
            store = SettingsStore(session, configured)
            store.set("curator.provider", "ollama")
            store.set("curator.ollama_url", "http://ollama.local:11434")
        ctx = service.build_context(dry_run=True)
        assert ctx.curator.name == "ollama"
        assert ctx.curator._base_url == "http://ollama.local:11434"

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
                session.add(
                    PickRow(
                        run_id=run.id,
                        user_id=user_id,
                        tmdb_id=100 + i,
                        media_type="show" if i % 2 else "movie",
                        rating_key=i,
                        rank=1,
                    )
                )
            session.commit()
        ctx = service.build_context(dry_run=True)
        # Keyed on (id, type): a recently-picked movie must not suppress the show sharing its id.
        assert ctx.recent_picks["sarah"] == {
            (104, MediaType.MOVIE),
            (105, MediaType.SHOW),
            (106, MediaType.MOVIE),
            (107, MediaType.SHOW),
            (108, MediaType.MOVIE),
            (109, MediaType.SHOW),
        }  # newest 6 only


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

    def test_a_gated_run_builds_nothing_but_still_runs_the_remedy(self, service, sessions, monkeypatch):
        """The gate refuses to BUILD rows — but it must not refuse the things that make the server
        more private, or it deadlocks itself: a missing exclude fails the Privacy Check, the failed
        check closes the gate, and a closed gate that blocked the sync would stop the only thing
        that writes the exclude. The check could never pass again.

        The remedy is `engine_run(ctx, [])`: with no users there is nothing to deliver and nothing
        to promote, so all that happens is the unhidable-row sweep and the share-filter merges.
        """
        import asyncio

        called = MagicMock(return_value=RunReport(started_at=datetime.now(UTC)))
        monkeypatch.setattr(run_service_mod, "engine_run", called)
        contexts: list[dict] = []
        monkeypatch.setattr(service, "build_context", lambda **kw: (contexts.append(kw), MagicMock())[1])

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
        # The engine ran, with NO users: sweep + share-filter merges, no delivery, no promotion.
        called.assert_called_once()
        assert called.call_args.args[1] == [], "a gated run must not build rows for anyone"
        # ...and for REAL. A remedy built as a dry run would log every write and make none, so the
        # excludes would never land, the check would never pass, and the gate would stay shut.
        assert contexts == [{"dry_run": False}]

    def test_a_remedy_that_blows_up_still_finishes_the_run(self, service, sessions, monkeypatch):
        """The gate refusal is the headline; a failing remedy is the footnote. It must never leave
        the run stuck in `running` — a run that never finishes is one nobody can act on."""
        import asyncio

        monkeypatch.setattr(service, "build_context", lambda **kw: MagicMock())
        monkeypatch.setattr(run_service_mod, "engine_run", MagicMock(side_effect=RuntimeError("PMS timed out")))

        async def scenario():
            run_id = await service.start_run(trigger="schedule", dry_run=False)
            for _ in range(100):
                with sessions() as session:
                    from rowarr.server.db.models import Run

                    run = session.get(Run, run_id)
                    if run.status == "error":
                        return run.stats
                await asyncio.sleep(0.02)
            raise AssertionError("the run never finished")

        stats = asyncio.run(scenario())

        assert "privacy gate" in stats["error"], "the gate refusal is still what the owner must fix"
        assert "PMS timed out" in stats["remedy_error"]


class TestBuildRequests:
    """The adapter turns request.* settings into a RequestConfig — off, whole, and half-configured."""

    def _store(self, sessions, tmp_path, values: dict):
        box = SecretBox(tmp_path)
        with sessions() as session:
            store = SettingsStore(session, box)
            for key, value in values.items():
                store.set(key, value)
        # A fresh store over a new session, so secret reads go through decrypt like production.
        session = sessions()
        return SettingsStore(session, box)

    def test_off_by_default_returns_none(self, sessions, tmp_path):
        store = self._store(sessions, tmp_path, {})
        assert ContextBuilder._build_requests(store) is None

    def test_enabled_with_both_apps_builds_both_targets(self, sessions, tmp_path):
        store = self._store(
            sessions,
            tmp_path,
            {
                "requests.enabled": True,
                "requests.radarr.url": "http://radarr:7878",
                "requests.radarr.apikey": "rk",
                "requests.radarr.quality_profile_id": 4,
                "requests.radarr.root_folder": "/movies",
                "requests.sonarr.url": "http://sonarr:8989",
                "requests.sonarr.apikey": "sk",
                "requests.sonarr.quality_profile_id": 7,
                "requests.sonarr.root_folder": "/tv",
                "requests.min_rating": 7.5,
                "requests.min_votes": 250,
                "requests.max_per_run": 3,
            },
        )
        cfg = ContextBuilder._build_requests(store)
        assert cfg is not None and cfg.enabled
        assert cfg.radarr.url == "http://radarr:7878" and cfg.radarr.api_key == "rk"
        assert cfg.radarr.quality_profile_id == 4 and cfg.radarr.root_folder == "/movies"
        assert cfg.sonarr.api_key == "sk" and cfg.sonarr.quality_profile_id == 7
        assert (cfg.min_rating, cfg.min_votes, cfg.max_per_run) == (7.5, 250, 3)

    def test_half_configured_app_is_left_as_none(self, sessions, tmp_path):
        # Radarr has a URL but no key -> its target is None (movies skipped), Sonarr is whole.
        store = self._store(
            sessions,
            tmp_path,
            {
                "requests.enabled": True,
                "requests.radarr.url": "http://radarr:7878",
                "requests.sonarr.url": "http://sonarr:8989",
                "requests.sonarr.apikey": "sk",
            },
        )
        cfg = ContextBuilder._build_requests(store)
        assert cfg.radarr is None  # no key -> not built, rather than erroring mid-run
        assert cfg.sonarr is not None
