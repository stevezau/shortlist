"""Round-trip coverage for the file-backed snapshot store the fake-Plex integration tests use."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from shortlist.engine.models import FilterSnapshot
from tests.fakes.file_stores import FileSnapshotStore


class TestFileSnapshotStore:
    def test_round_trip(self, tmp_path: Path):
        store = FileSnapshotStore(tmp_path / "snaps")
        snapshot = FilterSnapshot(
            plex_account_id=100,
            username="sarah",
            taken_at=datetime(2026, 7, 12, tzinfo=UTC),
            filters={"filterMovies": "contentRating!=R"},
        )
        store.save(snapshot)
        loaded = store.get(100)
        assert loaded == snapshot
        assert store.get(999) is None
        assert [s.username for s in store.all()] == ["sarah"]


class TestPlaintextSecretsAreHealed:
    """`tmdb.apikey` was the one API key missing from SECRET_KEYS — plaintext at rest, and returned
    UNREDACTED by `all_public()`, so it reached anything with a session and anyone handed a /config
    backup (plex-safety rule 9).

    Adding it to the set is not enough on its own: `get()` would then Fernet-decrypt the existing
    plaintext and raise, breaking TMDB and so every recommendation on every existing install. The heal
    runs at boot.
    """

    def _store(self, tmp_path):
        from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
        from shortlist.server.services.secrets import SecretBox

        run_migrations(tmp_path)
        sessions = make_session_factory(make_engine(tmp_path))
        return sessions, SecretBox(tmp_path)

    def test_a_plaintext_secret_is_encrypted_and_still_readable(self, tmp_path):
        from shortlist.server.db.models import Setting
        from shortlist.server.settings_store import SettingsStore

        sessions, box = self._store(tmp_path)
        with sessions() as session:  # write it the way a pre-fix install has it: raw
            session.add(Setting(key="tmdb.apikey", value={"v": "plain-tmdb-key"}))
            session.commit()

        with sessions() as session:
            healed = SettingsStore(session, box).encrypt_plaintext_secrets()

        assert healed == ["tmdb.apikey"]
        with sessions() as session:
            raw = session.get(Setting, "tmdb.apikey").value["v"]
            assert raw != "plain-tmdb-key", "still in the clear"
            # …and the app can still read it, which is the half that breaks a naive fix.
            assert SettingsStore(session, box).get("tmdb.apikey") == "plain-tmdb-key"

    def test_it_is_idempotent_and_leaves_encrypted_values_alone(self, tmp_path):
        from shortlist.server.db.models import Setting
        from shortlist.server.settings_store import SettingsStore

        sessions, box = self._store(tmp_path)
        with sessions() as session:
            SettingsStore(session, box).set("tmdb.apikey", "plain-tmdb-key")
        with sessions() as session:
            before = session.get(Setting, "tmdb.apikey").value["v"]

        with sessions() as session:
            assert SettingsStore(session, box).encrypt_plaintext_secrets() == []
        with sessions() as session:
            assert session.get(Setting, "tmdb.apikey").value["v"] == before, "double-encrypted"

    def test_the_api_no_longer_hands_the_key_out(self, tmp_path):
        """`all_public()` feeds `GET /api/settings`. A key returned in full there lands in the browser,
        in devtools, and in any HAR someone attaches to a bug report."""
        from shortlist.server.settings_store import SettingsStore

        sessions, box = self._store(tmp_path)
        with sessions() as session:
            SettingsStore(session, box).set("tmdb.apikey", "plain-tmdb-key")

        with sessions() as session:
            assert SettingsStore(session, box).all_public()["tmdb.apikey"] == "•••••"
