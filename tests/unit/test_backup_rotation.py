"""`shortlist/server/services/backup.py`: rotation never costs us the backup it just took.

`take_backup` runs on every boot. `_rotate` walks the backup directory with `glob` and then calls
`stat()` and `unlink()` on what it found — both of which raise if a file disappeared in between (a
concurrent boot, a manual tidy-up, a network filesystem). That exception propagated out of
`take_backup` AFTER the new backup had already been written successfully, so a vanished old file
turned a successful backup into a failed boot.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import datetime
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


class TestAWaitingRestoreKeepsItsBackup:
    """A restore waits for the next restart, and backups keep being taken meanwhile (nightly, "Back up
    now"). The rotation used to delete the oldest backup even when it was the one waiting to be restored,
    so the restart found nothing to restore and the owner's restore point was gone (review 2026-09-14)."""

    def _install(self, tmp_path: Path) -> Path:
        from shortlist.server.db.session import make_engine, run_migrations

        run_migrations(tmp_path)
        make_engine(tmp_path).dispose()
        chosen = backup_mod.take_backup(tmp_path, label="chosen")
        assert chosen is not None
        return chosen

    def test_rotation_skips_the_backup_a_restore_is_waiting_for(self, tmp_path: Path):
        import os

        chosen = self._install(tmp_path)
        os.utime(chosen, (1, 1))  # the oldest of all
        _make_backups(tmp_path / "backups", 4)
        assert backup_mod.request_restore(tmp_path, chosen.name, max_keep=3)

        backup_mod._rotate(tmp_path / "backups", max_keep=3)

        assert chosen.exists()
        assert backup_mod.apply_pending_restore(tmp_path)["status"] == "restored"

    def test_a_cancelled_restore_no_longer_protects_its_backup(self, tmp_path: Path):
        import os

        chosen = self._install(tmp_path)
        os.utime(chosen, (1, 1))
        _make_backups(tmp_path / "backups", 4)
        backup_mod.request_restore(tmp_path, chosen.name, max_keep=3)
        backup_mod.cancel_restore(tmp_path)

        backup_mod._rotate(tmp_path / "backups", max_keep=3)

        assert not chosen.exists()

    def test_a_copy_left_by_a_boot_killed_mid_restore_is_removed_on_the_next(self, tmp_path: Path):
        """The request is removed before the copy starts, so a boot killed inside it left a database-sized
        `shortlist.db.restoring` in /config that nothing ever removed."""
        (tmp_path / backup_mod.RESTORE_STAGING).write_bytes(b"half a database")

        assert backup_mod.apply_pending_restore(tmp_path) is None
        assert not (tmp_path / backup_mod.RESTORE_STAGING).exists()


#: What `take_backup(label="manual")` names its file at the frozen clock below.
FROZEN_NAME = "shortlist_20261002_031500_manual.db"


class _FrozenClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 2, 3, 15, 0, tzinfo=tz)


class _ProcessStopped(BaseException):
    """Stands in for the process going away mid-copy: no `except Exception` handler sees it."""


def _live_db(config_dir: Path) -> None:
    """A real database of many pages, so the copy can be watched between its steps."""
    with closing(sqlite3.connect(config_dir / "shortlist.db")) as con:
        con.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY, note TEXT)")
        con.executemany("INSERT INTO probe (note) VALUES (?)", [(f"row {i} " + "x" * 500,) for i in range(200)])
        con.commit()


def _hook_the_copy(monkeypatch: pytest.MonkeyPatch, during_copy: Callable[[], None]) -> None:
    """Call `during_copy` between the steps of `take_backup`'s copy, while pages are still left to copy."""
    real_connect = sqlite3.connect

    class _Source:
        def __init__(self, con: sqlite3.Connection) -> None:
            self._con = con

        def backup(self, target: sqlite3.Connection, **_kwargs) -> None:
            self._con.backup(target, pages=1, progress=lambda _status, left, _total: during_copy() if left else None)

        def close(self) -> None:
            self._con.close()

    def connect(database, *args, **kwargs):
        con = real_connect(database, *args, **kwargs)
        return _Source(con) if Path(database).name == "shortlist.db" else con

    monkeypatch.setattr(sqlite3, "connect", connect)


class TestABackupIsARestorePointOnlyOnceComplete:
    """`take_backup` used to copy straight onto the backup's final name. A process stopped mid-copy (a
    container stop during "Back up now" or the pre-migration backup) left a half-written file under a real
    backup name: listed as a restore point, kept by rotation, and restored with no integrity check."""

    @pytest.mark.parametrize(
        "failure",
        [OSError("disk full"), _ProcessStopped()],
        ids=["an_error_returns_none_as_before", "a_stop_still_propagates_as_before"],
    )
    def test_a_copy_that_fails_midway_leaves_no_backup_and_no_partial(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
    ):
        _live_db(tmp_path)

        def fail() -> None:
            raise failure

        _hook_the_copy(monkeypatch, fail)

        if isinstance(failure, Exception):
            assert backup_mod.take_backup(tmp_path, label="manual") is None
        else:
            with pytest.raises(_ProcessStopped):
                backup_mod.take_backup(tmp_path, label="manual")

        assert os.listdir(tmp_path / "backups") == [], "an unfinished copy was left in the backups folder"
        assert backup_mod.list_backups(tmp_path) == []

    def test_the_backup_name_does_not_exist_until_the_copy_has_finished(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _live_db(tmp_path)
        monkeypatch.setattr(backup_mod, "datetime", _FrozenClock)
        backups = tmp_path / "backups"
        seen: list[tuple[list[str], list[dict]]] = []
        _hook_the_copy(
            monkeypatch, lambda: seen.append((sorted(os.listdir(backups)), backup_mod.list_backups(tmp_path)))
        )

        made = backup_mod.take_backup(tmp_path, label="manual")

        assert made == backups / FROZEN_NAME
        assert seen, "the hook never ran while the copy was in progress"
        for names, listed in seen:
            assert FROZEN_NAME not in names, "a half-written copy sat under the backup's real name"
            assert listed == [], "a half-written copy was listed as a restore point"
            assert names, "the copy in progress belongs in the backups folder, where finishing it is one rename"

    def test_a_stale_partial_is_neither_listed_nor_counted_by_rotation(self, tmp_path: Path):
        backups = tmp_path / "backups"
        kept = _make_backups(backups, 2)
        stale = backups / "shortlist_20261001_020000_scheduled.db.partial"
        stale.write_bytes(b"")
        os.utime(stale, (2_000_000_000, 2_000_000_000))  # the newest file in the folder

        assert {b["name"] for b in backup_mod.list_backups(tmp_path)} == {p.name for p in kept}
        backup_mod._rotate(backups, max_keep=2)
        assert all(p.exists() for p in kept), "a partial took a real backup's place in the keep limit"

    def test_a_stale_partial_cannot_be_chosen_for_a_restore(self, tmp_path: Path):
        _live_db(tmp_path)
        backups = tmp_path / "backups"
        backups.mkdir()
        stale = backups / "shortlist_20261001_020000_scheduled.db.partial"
        stale.write_bytes(b"")
        live_before = (tmp_path / "shortlist.db").read_bytes()

        assert backup_mod.request_restore(tmp_path, stale.name) is False
        assert backup_mod.pending_restore(tmp_path) is None
        assert backup_mod.restore_backup(tmp_path, stale.name) is False
        assert (tmp_path / "shortlist.db").read_bytes() == live_before, "a half-written copy replaced the database"

    def test_the_next_backup_removes_a_stale_partial(self, tmp_path: Path):
        _live_db(tmp_path)
        backups = tmp_path / "backups"
        backups.mkdir()
        stale = backups / "shortlist_20261001_020000_scheduled.db.partial"
        stale.write_bytes(b"")
        (backups / f"{stale.name}-journal").write_bytes(b"x")
        foreign = backups / "notes.txt"
        foreign.write_text("the owner's own file")

        made = backup_mod.take_backup(tmp_path, label="manual")

        assert made is not None
        assert sorted(os.listdir(backups)) == sorted([made.name, foreign.name])

    def test_a_finished_backup_has_the_same_name_and_content_as_before(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _live_db(tmp_path)
        monkeypatch.setattr(backup_mod, "datetime", _FrozenClock)

        made = backup_mod.take_backup(tmp_path, label="Manual")

        assert made == tmp_path / "backups" / FROZEN_NAME
        assert os.listdir(tmp_path / "backups") == [FROZEN_NAME]
        assert [b["name"] for b in backup_mod.list_backups(tmp_path)] == [FROZEN_NAME]
        with closing(sqlite3.connect(made)) as con:
            assert con.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert con.execute("SELECT count(*) FROM probe").fetchone() == (200,)
            assert con.execute("SELECT note FROM probe WHERE id = 1").fetchone()[0] == "row 0 " + "x" * 500

    def test_a_backup_started_during_another_leaves_the_first_one_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The next backup sweeps partials left by a stopped process. One still being written by this
        process is not stale, and sweeping it would fail a backup that was running fine."""
        _live_db(tmp_path)
        first_copying, release_first = threading.Event(), threading.Event()

        def hold_the_first() -> None:
            if threading.current_thread().name == "first":
                first_copying.set()
                release_first.wait(5)

        _hook_the_copy(monkeypatch, hold_the_first)
        made: dict[str, Path | None] = {}

        def backup(label: str) -> None:
            made[label] = backup_mod.take_backup(tmp_path, label=label)

        first = threading.Thread(target=backup, args=("first",), name="first")
        first.start()
        assert first_copying.wait(5)
        second = threading.Thread(target=backup, args=("second",), name="second")
        second.start()
        second.join(timeout=0.5)  # unserialised, the second backup finishes here, in the middle of the first
        release_first.set()
        first.join(5)
        second.join(5)

        assert made["first"] is not None and made["first"].exists(), "the second backup destroyed the first"
        assert made["second"] is not None and made["second"].exists()


class TestRestoreChecksIntegrity:
    @staticmethod
    def _db(path: Path, marker: str) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS alembic_version (version_num TEXT)")
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES (?)", (marker,))
            conn.commit()

    @staticmethod
    def _marker(path: Path) -> str:
        with closing(sqlite3.connect(path)) as conn:
            return conn.execute("SELECT v FROM t").fetchone()[0]

    def test_a_truncated_backup_is_refused_and_the_live_database_is_untouched(self, tmp_path: Path):
        self._db(tmp_path / "shortlist.db", "live")
        backups = tmp_path / backup_mod.BACKUP_SUBDIR
        backups.mkdir()
        self._db(backups / "shortlist_20260901_000000.db", "old")
        good = backups / "shortlist_20260901_000000.db"
        bad = backups / "shortlist_20260902_000000.db"
        bad.write_bytes(good.read_bytes()[:100] + b"\x00" * 50)

        assert backup_mod.restore_backup(tmp_path, bad.name) is False

        assert self._marker(tmp_path / "shortlist.db") == "live"
        assert not (tmp_path / backup_mod.RESTORE_STAGING).exists()
        assert not any("pre-restore" in p.name for p in backups.iterdir())

    def test_a_not_a_database_file_is_refused(self, tmp_path: Path):
        self._db(tmp_path / "shortlist.db", "live")
        backups = tmp_path / backup_mod.BACKUP_SUBDIR
        backups.mkdir()
        junk = backups / "shortlist_20260902_000000.db"
        junk.write_bytes(b"this is not sqlite" * 100)

        assert backup_mod.restore_backup(tmp_path, junk.name) is False
        assert self._marker(tmp_path / "shortlist.db") == "live"

    def test_a_valid_backup_restores(self, tmp_path: Path):
        self._db(tmp_path / "shortlist.db", "live")
        backups = tmp_path / backup_mod.BACKUP_SUBDIR
        backups.mkdir()
        self._db(backups / "shortlist_20260901_000000.db", "old")

        assert backup_mod.restore_backup(tmp_path, "shortlist_20260901_000000.db") is True
        assert self._marker(tmp_path / "shortlist.db") == "old"

    def test_an_empty_backup_is_refused(self, tmp_path: Path):
        self._db(tmp_path / "shortlist.db", "live")
        backups = tmp_path / backup_mod.BACKUP_SUBDIR
        backups.mkdir()
        empty = backups / "shortlist_20260902_000000.db"
        empty.write_bytes(b"")

        assert backup_mod.restore_backup(tmp_path, empty.name) is False
        assert self._marker(tmp_path / "shortlist.db") == "live"
        assert not (tmp_path / backup_mod.RESTORE_STAGING).exists()

    def test_a_database_without_an_alembic_version_table_is_refused(self, tmp_path: Path):
        self._db(tmp_path / "shortlist.db", "live")
        backups = tmp_path / backup_mod.BACKUP_SUBDIR
        backups.mkdir()
        foreign = backups / "shortlist_20260902_000000.db"
        with closing(sqlite3.connect(foreign)) as conn:
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.commit()

        assert backup_mod.restore_backup(tmp_path, foreign.name) is False
        assert self._marker(tmp_path / "shortlist.db") == "live"

    def test_the_sidecars_the_read_only_open_leaves_are_removed(self, tmp_path: Path):
        staged = tmp_path / "staged.db"
        self._db(staged, "old")
        with closing(sqlite3.connect(staged)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        # What a read-only open of a WAL-mode file can leave behind.
        (tmp_path / "staged.db-wal").write_bytes(b"")
        (tmp_path / "staged.db-shm").write_bytes(b"")

        assert backup_mod._passes_integrity_check(staged) is True

        assert not (tmp_path / "staged.db-wal").exists()
        assert not (tmp_path / "staged.db-shm").exists()
