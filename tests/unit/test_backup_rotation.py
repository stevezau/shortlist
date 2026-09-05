"""`shortlist/server/services/backup.py`: rotation never costs us the backup it just took.

`take_backup` runs on every boot. `_rotate` walks the backup directory with `glob` and then calls
`stat()` and `unlink()` on what it found — both of which raise if a file disappeared in between (a
concurrent boot, a manual tidy-up, a network filesystem). That exception propagated out of
`take_backup` AFTER the new backup had already been written successfully, so a vanished old file
turned a successful backup into a failed boot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shortlist.server.services import backup as backup_mod


def _make_backups(backup_dir: Path, count: int) -> list[Path]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(count):
        p = backup_dir / f"shortlist_2026090{i}_000000.db"
        p.write_bytes(b"x")
        made.append(p)
    return made


class TestRotateSurvivesAVanishingFile:
    def test_a_file_that_disappears_before_unlink_does_not_raise(self, tmp_path: Path):
        _make_backups(tmp_path, 5)
        real_unlink = Path.unlink

        def vanishing(self, *args, **kwargs):
            real_unlink(self, *args, **kwargs)
            return real_unlink(self, *args, **kwargs)  # second call raises FileNotFoundError

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Path, "unlink", vanishing)
            backup_mod._rotate(tmp_path, max_keep=2)  # must not raise

    def test_it_still_deletes_everything_it_can(self, tmp_path: Path):
        _make_backups(tmp_path, 5)

        backup_mod._rotate(tmp_path, max_keep=2)

        assert len(list(tmp_path.glob("shortlist_*.db"))) == 2, "rotation stopped early"

    def test_the_newest_are_the_ones_kept(self, tmp_path: Path):
        made = _make_backups(tmp_path, 4)
        for i, p in enumerate(made):
            import os

            os.utime(p, (1_000_000 + i * 100, 1_000_000 + i * 100))

        backup_mod._rotate(tmp_path, max_keep=2)

        assert {p.name for p in tmp_path.glob("shortlist_*.db")} == {made[-1].name, made[-2].name}


class TestABackupIsNeverLostToRotation:
    def test_take_backup_returns_the_path_even_when_rotation_fails(self, tmp_path: Path, monkeypatch):
        """The backup is already written and crash-safe by this point. Rotation is housekeeping, and
        housekeeping must never invalidate the thing it is tidying up around — this runs at boot, so
        raising here is a crash-loop on a host that recreates the container automatically."""
        from shortlist.server.db.session import make_engine, run_migrations

        run_migrations(tmp_path)
        make_engine(tmp_path).dispose()

        def boom(*_args, **_kwargs):
            raise OSError("disk went away mid-rotate")

        monkeypatch.setattr(backup_mod, "_rotate", boom)

        result = backup_mod.take_backup(tmp_path, label="boot")

        assert result is not None and result.exists(), "a successful backup was thrown away by rotation"


class TestARestoreNeverOverwritesTheOnlyCopy:
    """`restore_backup` copies the chosen file over the live database and unlinks the WAL.

    The pre-restore backup it takes first is the only way back from a restore chosen by mistake, and
    `take_backup` answers None rather than raising on a full disk, a permission problem or a locked
    database. Continuing past that answer destroys the server's current state with no copy of it
    anywhere — the one case that needed the guarantee was the one case that didn't have it.
    """

    def _install(self, tmp_path: Path) -> Path:
        from shortlist.server.db.session import make_engine, run_migrations

        run_migrations(tmp_path)
        make_engine(tmp_path).dispose()
        chosen = backup_mod.take_backup(tmp_path, label="chosen")
        assert chosen is not None
        return chosen

    def test_a_restore_is_refused_when_the_pre_restore_backup_cannot_be_taken(self, tmp_path: Path, monkeypatch):
        chosen = self._install(tmp_path)
        db_path = tmp_path / "shortlist.db"
        before = db_path.read_bytes()
        monkeypatch.setattr(backup_mod, "take_backup", lambda *_a, **_k: None)

        assert backup_mod.restore_backup(tmp_path, chosen.name) is False
        assert db_path.read_bytes() == before, "the live database was overwritten with no way back"

    def test_a_restore_proceeds_when_the_pre_restore_backup_succeeds(self, tmp_path: Path):
        """The other half of the branch: the guard must not have broken the normal path.

        The live database is changed in a way SQLite still accepts — corrupting it would make
        `take_backup` fail for a second reason and the test would pass on the guard it is trying to
        prove is out of the way.
        """
        import sqlite3

        chosen = self._install(tmp_path)
        with sqlite3.connect(tmp_path / "shortlist.db") as con:
            con.execute("CREATE TABLE only_in_the_live_db (id INTEGER PRIMARY KEY)")

        assert backup_mod.restore_backup(tmp_path, chosen.name) is True

        with sqlite3.connect(tmp_path / "shortlist.db") as con:
            tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "only_in_the_live_db" not in tables, "the chosen backup did not replace the live database"
