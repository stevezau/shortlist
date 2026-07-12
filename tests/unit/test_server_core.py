"""Server building blocks: migrations, settings store, secrets, SSE bus."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

from rowarr.server.db.models import Setting, User
from rowarr.server.db.session import make_engine, make_session_factory, run_migrations
from rowarr.server.services.secrets import SecretBox
from rowarr.server.services.sse import EventBus
from rowarr.server.settings_store import SettingsStore


@pytest.fixture
def db_sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    yield make_session_factory(engine)
    engine.dispose()


class TestMigrations:
    def test_migrations_create_all_v1_tables(self, tmp_path: Path, db_sessions):
        from sqlalchemy import inspect

        engine = make_engine(tmp_path)
        tables = set(inspect(engine).get_table_names())
        assert {
            "settings",
            "server",
            "users",
            "runs",
            "run_users",
            "picks",
            "restriction_snapshots",
            "privacy_checks",
            "caches",
            "events",
        } <= tables
        engine.dispose()

    def test_models_round_trip(self, db_sessions):
        with db_sessions() as session:
            session.add(User(plex_account_id=555000100, username="sarah", slug="sarah"))
            session.commit()
        with db_sessions() as session:
            user = session.query(User).one()
            assert user.enabled is False
            assert user.prefs == {}


class TestSecretBox:
    def test_round_trip_and_key_permissions(self, tmp_path: Path):
        box = SecretBox(tmp_path)
        token = box.encrypt("plex-token-value")
        assert token != "plex-token-value"
        assert box.decrypt(token) == "plex-token-value"
        mode = stat.S_IMODE((tmp_path / "secret.key").stat().st_mode)
        assert mode == 0o600
        # A second box with the same key file decrypts values from the first.
        assert SecretBox(tmp_path).decrypt(token) == "plex-token-value"


class TestSettingsStore:
    def test_defaults_and_set_get(self, db_sessions):
        with db_sessions() as session:
            store = SettingsStore(session)
            assert store.get("row.size") == 15
            store.set("row.size", 20)
            assert store.get("row.size") == 20

    def test_secrets_encrypted_at_rest_and_redacted_in_public_view(self, tmp_path: Path, db_sessions):
        box = SecretBox(tmp_path)
        with db_sessions() as session:
            store = SettingsStore(session, box)
            store.set("plex.token", "super-secret-token")
            raw = session.get(Setting, "plex.token").value["v"]
            assert "super-secret-token" not in raw  # Fernet ciphertext only
            assert store.get("plex.token") == "super-secret-token"
            assert store.all_public()["plex.token"] == "•••••"

    def test_env_seeding_happens_exactly_once(self, tmp_path: Path, db_sessions):
        box = SecretBox(tmp_path)
        with db_sessions() as session:
            store = SettingsStore(session, box)
            store.seed_from_env({"PLEX_URL": "http://pms:32400", "PLEX_TOKEN": "tok"})
            assert store.get("plex.url") == "http://pms:32400"
            assert store.get("plex.token") == "tok"
            # Second boot with different env values: DB wins, env ignored.
            store.seed_from_env({"PLEX_URL": "http://other:32400"})
            assert store.get("plex.url") == "http://pms:32400"


class TestEventBus:
    def test_publish_reaches_subscriber_as_sse_frame(self):
        async def scenario():
            bus = EventBus()
            stream = bus.stream()
            hello = await stream.__anext__()
            assert hello.startswith("event: hello")
            bus.publish("run.finished", {"run_id": 7})
            frame = await asyncio.wait_for(stream.__anext__(), timeout=1)
            assert "event: run.finished" in frame
            assert '"run_id": 7' in frame
            await stream.aclose()

        asyncio.run(scenario())
