"""Regressions for bugs the browser e2e layer surfaced (each one shipped to a real Plex screen)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import rowarr.server.services.run_service as run_service_mod
from rowarr.engine.delivery import DEFAULT_ROW_NAME, render_row_name
from rowarr.engine.models import MediaType, Pick
from rowarr.server.db.models import User
from rowarr.server.db.session import make_engine, make_session_factory, run_migrations
from rowarr.server.services.run_service import RunService
from rowarr.server.services.secrets import SecretBox
from rowarr.server.services.sse import EventBus
from rowarr.server.settings_store import SettingsStore
from tests.conftest import make_profile


def pick(seed_title: str | None) -> Pick:
    return Pick(
        tmdb_id=1, rating_key=1, title="Movie", rank=1, reason="r", media_type=MediaType.MOVIE, seed_title=seed_title
    )


class TestColdStartRowName:
    """A cold-start user has no seed — the row must not read 'Because you watched'."""

    def test_seeded_user_gets_the_dynamic_title(self):
        name = render_row_name("Because you watched {top_seed}", make_profile(), [pick("Fargo")])
        assert name == "Because you watched Fargo"

    def test_cold_start_user_falls_back_instead_of_dangling(self):
        assert render_row_name("Because you watched {top_seed}", make_profile(), [pick(None)]) == DEFAULT_ROW_NAME
        assert render_row_name("Because you watched {top_seed}", make_profile(), []) == DEFAULT_ROW_NAME

    def test_static_template_is_untouched(self):
        assert render_row_name("✨ Picked for You", make_profile(), [pick(None)]) == "✨ Picked for You"


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    factory = make_session_factory(engine)
    with factory() as session:
        session.add(User(plex_account_id=1, username="sarah", slug="sarah", enabled=True))
        session.commit()
    yield factory
    engine.dispose()


class TestPauseAll:
    """The Danger Zone switch was a no-op: the key wasn't storable and nothing read it."""

    def test_paused_all_stops_every_run_without_disabling_users(self, sessions, tmp_path):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        with sessions() as session:
            assert [p.slug for p in service.enabled_profiles(session)] == ["sarah"]
            SettingsStore(session, service._secrets).set("paused_all", True)
            assert service.enabled_profiles(session) == []
            # The user is still enabled — unpausing restores them, no re-enabling needed.
            assert session.query(User).filter_by(enabled=True).count() == 1
            SettingsStore(session, service._secrets).set("paused_all", False)
            assert [p.slug for p in service.enabled_profiles(session)] == ["sarah"]


class TestOllamaCurator:
    """Ollama takes a base URL and no key; the key was previously unstorable, 422ing setup."""

    def test_ollama_provider_is_built_with_its_url(self, sessions, tmp_path, monkeypatch):
        service = RunService(sessions, EventBus(), tmp_path, SecretBox(tmp_path))
        with sessions() as session:
            store = SettingsStore(session, service._secrets)
            store.set("plex.url", "http://pms:32400")
            store.set("plex.token", "tok")
            store.set("tmdb.apikey", "k")
            store.set("curator.provider", "ollama")
            store.set("curator.ollama_url", "http://ollama.local:11434")
        monkeypatch.setattr(run_service_mod, "PlexClient", lambda *a, **k: MagicMock(machine_id="m1"))
        monkeypatch.setattr(run_service_mod, "PlexTvClient", lambda *a, **k: MagicMock())
        monkeypatch.setattr(run_service_mod, "TmdbClient", lambda *a, **k: MagicMock())

        ctx = service.build_context(dry_run=True)

        assert ctx.curator.name == "ollama"
        assert ctx.curator._base_url == "http://ollama.local:11434"
