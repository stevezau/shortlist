"""Automatic SQLite backup management for the /config directory.

Takes point-in-time copies of shortlist.db (via SQLite's backup API for crash-safety), rotates old
ones, and hooks into startup (pre-migration) and APScheduler (daily). The backup directory lives at
/config/backups/ so it's inside the user's persisted volume.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from shortlist.server.net_guard import safe_backup_name

DEFAULT_MAX_BACKUPS = 10
BACKUP_SUBDIR = "backups"


def _backup_dir(config_dir: Path) -> Path:
    d = config_dir / BACKUP_SUBDIR
    d.mkdir(exist_ok=True)
    return d


def take_backup(config_dir: Path, *, label: str = "scheduled", max_keep: int = DEFAULT_MAX_BACKUPS) -> Path | None:
    """Take a crash-safe backup of shortlist.db using SQLite's online backup API.

    Returns the path to the new backup, or None if the DB doesn't exist.
    """
    db_path = config_dir / "shortlist.db"
    if not db_path.exists():
        return None

    backup_dir = _backup_dir(config_dir)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    # The label lands in a filename, and `POST /api/system/jobs` takes an unvalidated payload — so
    # `{"label": "../../../../tmp/x"}` wrote a complete copy of the database, encrypted Plex tokens
    # included, outside /config. The restore path has always validated its filename; this one didn't.
    safe_label = re.sub(r"[^a-z0-9_-]", "-", (label or "backup").strip().lower())[:32] or "backup"
    backup_path = backup_dir / f"shortlist_{ts}_{safe_label}.db"

    src = dst = None
    try:
        src = sqlite3.connect(str(db_path))
        dst = sqlite3.connect(str(backup_path))
        src.backup(dst)
        logger.info("backup created: {} ({:.1f} KB)", backup_path.name, backup_path.stat().st_size / 1024)
    except Exception as e:
        logger.error("backup failed: {}", e)
        if backup_path.exists():
            backup_path.unlink()
        return None
    finally:
        # Both handles, always. They used to be closed only on the success path, so a failure
        # mid-`backup()` leaked two SQLite connections and then unlinked a file `dst` still held —
        # and this runs on every boot.
        for conn in (dst, src):
            if conn is not None:
                conn.close()

    _rotate(backup_dir, max_keep)
    return backup_path


def _rotate(backup_dir: Path, max_keep: int) -> None:
    """Keep only the most recent `max_keep` backups, delete the rest."""
    backups = sorted(backup_dir.glob("shortlist_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max_keep:]:
        old.unlink()
        logger.debug("rotated old backup: {}", old.name)


def list_backups(config_dir: Path) -> list[dict]:
    """Return metadata for all existing backups, newest first."""
    backup_dir = config_dir / BACKUP_SUBDIR
    if not backup_dir.exists():
        return []
    backups = sorted(backup_dir.glob("shortlist_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    result = []
    for p in backups:
        stat = p.stat()
        result.append(
            {
                "name": p.name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
            }
        )
    return result


def restore_backup(config_dir: Path, backup_name: str) -> bool:
    """Restore a backup by copying it over the current DB. Returns True on success.

    The caller must stop the app or hold the DB lock before calling this.
    """
    try:
        backup_name = safe_backup_name(backup_name)
    except ValueError:
        # `../../etc/passwd` would otherwise escape the backups directory and be copied over the DB.
        logger.error("refusing a backup name that is not a plain filename: {!r}", backup_name)
        return False
    backup_path = config_dir / BACKUP_SUBDIR / backup_name
    db_path = config_dir / "shortlist.db"
    if not backup_path.exists():
        logger.error("backup not found: {}", backup_name)
        return False

    # Take a pre-restore backup of current state
    take_backup(config_dir, label="pre-restore", max_keep=DEFAULT_MAX_BACKUPS)

    # Remove WAL/SHM files (they belong to the old DB)
    for suffix in (".db-wal", ".db-shm"):
        wal = config_dir / f"shortlist{suffix}"
        if wal.exists():
            wal.unlink()

    shutil.copy2(backup_path, db_path)
    logger.info("restored from backup: {}", backup_name)
    return True
