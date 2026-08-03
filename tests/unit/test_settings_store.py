"""SettingsStore's two failure directions: a row it cannot read, and a secret it cannot encrypt.

Both are cases where the previous code did the quiet, wrong thing — one raised on a shape its sibling
deliberately tolerated, the other stored a Plex token in the clear.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from shortlist.server.db.models import Setting
from shortlist.server.db.session import make_engine, make_session_factory, run_migrations
from shortlist.server.services.secrets import SecretBox
from shortlist.server.settings_store import DEFAULTS, SECRET_KEYS, SettingsStore


@pytest.fixture
def sessions(tmp_path: Path):
    run_migrations(tmp_path)
    engine = make_engine(tmp_path)
    yield make_session_factory(engine)
    engine.dispose()


class TestAnUnreadableRowFallsBackInsteadOfRaising:
    """`get()` has always tolerated a row that isn't shaped `{"v": ...}` — a hand-edited database, a
    half-written migration — because it is read at boot and an exception there is a crash loop.
    `all_public()` did `row.value["v"]` unguarded, so the same row 500'd the entire Settings page,
    recoverable only by hand-editing SQLite."""

    def _plant_a_bad_row(self, session: Session, key: str) -> None:
        session.add(Setting(key=key, value={"wrong": "shape"}))
        session.commit()

    def test_get_returns_the_default(self, sessions):
        with sessions() as session:
            self._plant_a_bad_row(session, "row.size")

            assert SettingsStore(session).get("row.size") == DEFAULTS["row.size"]

    def test_all_public_returns_the_default_rather_than_raising(self, sessions):
        with sessions() as session:
            self._plant_a_bad_row(session, "row.size")

            values = SettingsStore(session).all_public()

        assert values["row.size"] == DEFAULTS["row.size"]

    def test_one_bad_row_does_not_hide_the_good_ones(self, sessions):
        """The Settings page is one response — a single unreadable row must cost only that key."""
        with sessions() as session:
            SettingsStore(session).set("row.size", 20)
            self._plant_a_bad_row(session, "log.level")

            values = SettingsStore(session).all_public()

        assert values["row.size"] == 20  # the row that IS readable still comes through
        assert values["log.level"] == DEFAULTS["log.level"]

    def test_a_bad_secret_row_still_redacts_rather_than_leaking_the_raw_value(self, tmp_path: Path, sessions):
        """A secret whose row is malformed must not fall through to being echoed — `all_public()`
        feeds `GET /api/settings`, which lands in the browser."""
        with sessions() as session:
            session.add(Setting(key="plex.token", value={"v_typo": "raw-token-value"}))
            session.commit()

            values = SettingsStore(session, SecretBox(tmp_path)).all_public()

        # Treated exactly like a key that was never set: reported as unset, never echoed.
        assert not values.get("plex.token")
        assert "raw-token-value" not in str(values)


class TestASecretNeedsASecretBox:
    """Without a box the crypto used to short-circuit silently, so `SettingsStore(session).set(
    "plex.token", ...)` wrote the owner's token to the database in the clear (plex-safety rule 9)."""

    def test_setting_a_secret_without_a_box_raises_instead_of_storing_plaintext(self, sessions):
        with sessions() as session:
            with pytest.raises(RuntimeError, match="secret"):
                SettingsStore(session).set("plex.token", "super-secret-token")

            assert session.get(Setting, "plex.token") is None  # nothing was written

    def test_reading_a_secret_without_a_box_raises_instead_of_handing_back_ciphertext(self, tmp_path: Path, sessions):
        with sessions() as session:
            SettingsStore(session, SecretBox(tmp_path)).set("plex.token", "super-secret-token")

            with pytest.raises(RuntimeError, match="secret"):
                SettingsStore(session).get("plex.token")

    @pytest.mark.parametrize("key", sorted(SECRET_KEYS))
    def test_every_secret_key_is_covered_not_just_plex_token(self, key: str, sessions):
        """`tmdb.apikey` was plaintext at rest for exactly this reason — the guard has to be the set,
        not a hand-listed subset of it."""
        with sessions() as session, pytest.raises(RuntimeError):
            SettingsStore(session).set(key, "value")

    def test_a_non_secret_key_still_works_without_a_box(self, sessions):
        """Half the server reads settings through a box-less store; only SECRET_KEYS may be refused."""
        with sessions() as session:
            store = SettingsStore(session)
            store.set("row.size", 22)

            assert store.get("row.size") == 22

    def test_all_public_still_works_without_a_box(self, tmp_path: Path, sessions):
        """It never decrypts — only the truthiness of a secret decides the redaction — so a box-less
        caller (the notifications and schedule endpoints) must not be locked out of it."""
        with sessions() as session:
            SettingsStore(session, SecretBox(tmp_path)).set("plex.token", "super-secret-token")

            values = SettingsStore(session).all_public()

        assert values["plex.token"] == "•••••"


class TestUnsetIsNotTheSameAsStoringABlank:
    """`unset` removes the row; `set(key, "")` writes one. For the crons the UI can switch off, those
    two states mean opposite things — off vs. run at the built-in default (`scheduler._OFF_ABLE`)."""

    def test_unset_removes_the_row_so_has_row_goes_back_to_false(self, sessions):
        with sessions() as session:
            store = SettingsStore(session)
            store.set("sync.check_cron", "")
            assert store.has_row("sync.check_cron") is True

            assert store.unset("sync.check_cron") is True

            assert store.has_row("sync.check_cron") is False
            # Back to the declared default, exactly as a key that was never written reads.
            assert store.get("sync.check_cron") == DEFAULTS["sync.check_cron"]

    def test_unsetting_a_key_with_no_row_is_a_no_op(self, sessions):
        with sessions() as session:
            store = SettingsStore(session)

            assert store.unset("sync.check_cron") is False
            assert store.has_row("sync.check_cron") is False
