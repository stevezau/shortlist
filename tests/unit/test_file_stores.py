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

    def test_a_secret_encrypted_with_a_LOST_key_is_never_overwritten(self, tmp_path):
        """The credential-destroying bug. `encrypt_plaintext_secrets` treated ANY decrypt failure as
        "this value is plaintext" — but a wrong key and genuine plaintext both raise a bare
        InvalidToken with an empty message, so a lost/regenerated secret.key made boot RE-ENCRYPT
        every real credential with the new key, destroying the only recoverable copy, and report it
        as "encrypted N setting(s) that were stored in the clear".

        A Fernet token is recognisable by SHAPE even when it cannot be decrypted, which is what lets
        the two cases be told apart.
        """
        from shortlist.server.db.models import Setting
        from shortlist.server.services.secrets import SecretBox
        from shortlist.server.settings_store import SettingsStore

        sessions, box = self._store(tmp_path)
        with sessions() as session:
            SettingsStore(session, box).set("plex.token", "real-plex-token")
        with sessions() as session:
            encrypted = session.get(Setting, "plex.token").value["v"]

        (tmp_path / "secret.key").unlink()  # the key is lost; next boot mints a new one
        new_box = SecretBox(tmp_path)

        with sessions() as session:
            healed = SettingsStore(session, new_box).encrypt_plaintext_secrets()

        assert healed == [], "reported a real credential as plaintext"
        with sessions() as session:
            assert session.get(Setting, "plex.token").value["v"] == encrypted, (
                "OVERWROTE a credential encrypted with the lost key — the original is now unrecoverable"
            )

    def test_undecryptable_secrets_are_reported_so_boot_can_warn(self, tmp_path):
        """Stopping the overwrite is not enough on its own: the owner has to be TOLD, or the app
        silently runs with credentials it cannot read and no explanation."""
        from shortlist.server.services.secrets import SecretBox
        from shortlist.server.settings_store import SettingsStore

        sessions, box = self._store(tmp_path)
        with sessions() as session:
            store = SettingsStore(session, box)
            store.set("plex.token", "real-plex-token")
            store.set("tmdb.apikey", "real-tmdb-key")

        (tmp_path / "secret.key").unlink()
        new_box = SecretBox(tmp_path)

        with sessions() as session:
            assert SettingsStore(session, new_box).undecryptable_secrets() == ["plex.token", "tmdb.apikey"]

    def test_genuine_plaintext_is_still_healed_when_the_key_is_intact(self, tmp_path):
        """The guard must not break the case the function exists for: a value that never was a
        Fernet token still gets encrypted."""
        from shortlist.server.db.models import Setting
        from shortlist.server.settings_store import SettingsStore

        sessions, box = self._store(tmp_path)
        with sessions() as session:
            session.add(Setting(key="tmdb.apikey", value={"v": "0123456789abcdef0123456789abcdef"}))
            session.commit()

        with sessions() as session:
            assert SettingsStore(session, box).encrypt_plaintext_secrets() == ["tmdb.apikey"]
        with sessions() as session:
            assert SettingsStore(session, box).get("tmdb.apikey") == "0123456789abcdef0123456789abcdef"
            assert SettingsStore(session, box).undecryptable_secrets() == []

    def test_the_api_no_longer_hands_the_key_out(self, tmp_path):
        """`all_public()` feeds `GET /api/settings`. A key returned in full there lands in the browser,
        in devtools, and in any HAR someone attaches to a bug report."""
        from shortlist.server.settings_store import SettingsStore

        sessions, box = self._store(tmp_path)
        with sessions() as session:
            SettingsStore(session, box).set("tmdb.apikey", "plain-tmdb-key")

        with sessions() as session:
            assert SettingsStore(session, box).all_public()["tmdb.apikey"] == "•••••"
