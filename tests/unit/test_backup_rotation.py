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
