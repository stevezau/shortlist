"""RunService.build_context branch matrix."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import shortlist.server.services.context_builder as context_builder_mod
from shortlist.engine.history import FallbackHistorySource, PlexHistorySource
from shortlist.engine.models import MediaType
from shortlist.server.db.models import PickRow, User
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.context_builder import ContextBuilder
from shortlist.server.services.run_service import RunService
from shortlist.server.services.secrets import SecretBox
from shortlist.server.services.sse import EventBus
from shortlist.server.settings_store import SettingsStore


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
        from shortlist.server.db.models import Run

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
